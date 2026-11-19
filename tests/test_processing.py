from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import (
    AudioFile,
    Flashcard,
    LectureProcessingResult,
    LectureStatus,
    OperationalEvent,
    Summary,
    Transcription,
    User,
)
from app.schemas import FlashcardData, StudyNotes
from app.services.ai import AIService, AIUnavailable
from app.services.processing import process_lecture
from app.services.transcription import FasterWhisperTranscriber, InvalidAudio, Transcript
from app.worker import LectureTaskFailed, process_lecture_task


async def new_lecture(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        user = User(email="worker@example.com", hashed_password="not-used")
        session.add(user)
        await session.flush()
        lecture = AudioFile(user_id=user.id, file_path="/test/audio.wav")
        session.add(lecture)
        await session.commit()
        return lecture.id


def study_notes() -> StudyNotes:
    return StudyNotes(
        structured_markdown="# Topic\nA useful fact.",
        key_points_json=["A useful fact"],
        flashcards=[FlashcardData(question="What?", answer="A useful fact")],
    )


async def test_pipeline_persists_all_results_and_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = await new_lecture(factory)
    transcriber = Mock()
    transcriber.transcribe.return_value = Transcript(
        raw_text="A  useful\n fact",
        duration=10.5,
        timestamps=[{"start": 0.0, "end": 10.5, "text": "A useful fact"}],
    )
    ai = Mock(spec=AIService)
    ai.generate_notes = AsyncMock(return_value=study_notes())
    settings.smtp_host = "smtp.example.com"
    settings.smtp_from = "eduflow@example.com"
    send = Mock(side_effect=OSError("SMTP unavailable"))
    monkeypatch.setattr("app.services.mail._send_email", send)
    await process_lecture(file_id, factory, transcriber, ai, settings)
    await process_lecture(file_id, factory, transcriber, ai, settings)
    send.assert_called_once()
    assert send.call_args.args[1:3] == ("worker@example.com", "LectureAI — лекция готова")
    transcriber.transcribe.assert_called_once_with(Path("/test/audio.wav"))
    ai.generate_notes.assert_awaited_once_with("A useful fact")
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        assert lecture is not None
        assert lecture.status == LectureStatus.completed and lecture.duration == 10.5
        transcript = await session.scalar(select(Transcription))
        assert transcript is not None and transcript.clean_text == "A useful fact"
        summary = await session.scalar(select(Summary).options(selectinload(Summary.flashcards)))
        assert summary is not None and len(summary.flashcards) == 1
        assert summary.flashcards[0].answer == "A useful fact"
        assert await session.scalar(select(func.count()).select_from(Transcription)) == 1
        outcomes = (await session.scalars(select(LectureProcessingResult))).all()
        assert len(outcomes) == 1 and outcomes[0].successful
        events = (
            await session.scalars(select(OperationalEvent).order_by(OperationalEvent.id))
        ).all()
        assert [(item.event, item.level, item.error_type) for item in events] == [
            ("lecture_completed", "info", None),
            ("lecture_mail_failed", "error", "OSError"),
        ]
        assert all(
            item.lecture_id == file_id and item.user_id == lecture.user_id for item in events
        )


async def test_ai_failure_has_no_partial_results_and_can_be_retried(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = await new_lecture(factory)
    transcriber = Mock()
    transcriber.transcribe.return_value = Transcript(raw_text="Text", duration=1, timestamps=[])
    ai = Mock(spec=AIService)
    ai.generate_notes = AsyncMock(side_effect=AIUnavailable("private upstream details"))
    settings.smtp_host = "smtp.example.com"
    settings.smtp_from = "eduflow@example.com"
    send = Mock()
    monkeypatch.setattr("app.services.mail._send_email", send)
    with pytest.raises(AIUnavailable):
        await process_lecture(file_id, factory, transcriber, ai, settings)
    send.assert_called_once()
    assert send.call_args.args[1:3] == (
        "worker@example.com",
        "LectureAI — не удалось обработать лекцию",
    )
    assert "private upstream details" not in send.call_args.args[3]
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        assert lecture is not None and lecture.status == LectureStatus.failed
        assert "private" not in (lecture.error_message or "")
        for model in [Transcription, Summary, Flashcard]:
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    ai.generate_notes = AsyncMock(return_value=study_notes())
    await process_lecture(file_id, factory, transcriber, ai, settings)
    assert send.call_count == 2
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        assert lecture is not None and lecture.status == LectureStatus.completed
        attempts = (
            await session.scalars(
                select(LectureProcessingResult).order_by(LectureProcessingResult.id)
            )
        ).all()
        assert [item.successful for item in attempts] == [False, True]
        assert "private" not in (attempts[0].error_message or "")
        events = (
            await session.scalars(select(OperationalEvent).order_by(OperationalEvent.id))
        ).all()
        assert [(item.event, item.error_type) for item in events] == [
            ("lecture_failed", "AIUnavailable"),
            ("lecture_completed", None),
        ]
        assert all(
            item.lecture_id == file_id and item.user_id == lecture.user_id for item in events
        )


async def test_bad_audio_fails_without_calling_ai(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    file_id = await new_lecture(factory)
    transcriber = Mock()
    transcriber.transcribe.side_effect = InvalidAudio("No speech was detected in the recording")
    ai = Mock(spec=AIService)
    with pytest.raises(InvalidAudio):
        await process_lecture(file_id, factory, transcriber, ai, settings)
    ai.generate_notes.assert_not_called()


async def test_unknown_job_is_noop(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    transcriber = Mock()
    ai = Mock(spec=AIService)
    await process_lecture(uuid4(), factory, transcriber, ai, settings)
    transcriber.transcribe.assert_not_called()


def test_audio_probe_only_opens_supported_local_formats(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Mock()
    probe.return_value.stdout = (
        '{"format":{"duration":"2.5","format_name":"mov,mp4,m4a,3gp,3g2,mj2"},'
        '"streams":[{"codec_type":"audio"}]}'
    )
    monkeypatch.setattr("app.services.transcription.subprocess.run", probe)
    assert FasterWhisperTranscriber(settings)._duration(Path("lecture.m4a")) == 2.5
    command = probe.call_args.args[0]
    assert command[command.index("-format_whitelist") + 1] == "mp3,wav,mov"
    assert command[command.index("-protocol_whitelist") + 1] == "file"


def test_worker_does_not_expose_exception_details_to_celery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = AsyncMock(side_effect=RuntimeError("private provider detail"))
    monkeypatch.setattr("app.worker.run_lecture", run)
    file_id = uuid4()
    with pytest.raises(LectureTaskFailed) as exc:
        process_lecture_task(str(file_id))
    run.assert_awaited_once_with(file_id)
    assert "private provider detail" not in str(exc.value)
    assert exc.value.__suppress_context__
