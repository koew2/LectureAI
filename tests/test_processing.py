from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import AudioFile, Flashcard, LectureStatus, Summary, Transcription, User
from app.schemas import FlashcardData, StudyNotes
from app.services.ai import AIService, AIUnavailable
from app.services.processing import process_lecture
from app.services.transcription import InvalidAudio, Transcript


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
    await process_lecture(file_id, factory, transcriber, ai)
    await process_lecture(file_id, factory, transcriber, ai)
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


async def test_ai_failure_has_no_partial_results_and_can_be_retried(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    file_id = await new_lecture(factory)
    transcriber = Mock()
    transcriber.transcribe.return_value = Transcript(raw_text="Text", duration=1, timestamps=[])
    ai = Mock(spec=AIService)
    ai.generate_notes = AsyncMock(side_effect=AIUnavailable("private upstream details"))
    with pytest.raises(AIUnavailable):
        await process_lecture(file_id, factory, transcriber, ai)
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        assert lecture is not None and lecture.status == LectureStatus.failed
        assert "private" not in (lecture.error_message or "")
        for model in [Transcription, Summary, Flashcard]:
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    ai.generate_notes = AsyncMock(return_value=study_notes())
    await process_lecture(file_id, factory, transcriber, ai)
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        assert lecture is not None and lecture.status == LectureStatus.completed


async def test_bad_audio_fails_without_calling_ai(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    file_id = await new_lecture(factory)
    transcriber = Mock()
    transcriber.transcribe.side_effect = InvalidAudio("No speech was detected in the recording")
    ai = Mock(spec=AIService)
    with pytest.raises(InvalidAudio):
        await process_lecture(file_id, factory, transcriber, ai)
    ai.generate_notes.assert_not_called()


async def test_unknown_job_is_noop(factory: async_sessionmaker[AsyncSession]) -> None:
    transcriber = Mock()
    ai = Mock(spec=AIService)
    await process_lecture(uuid4(), factory, transcriber, ai)
    transcriber.transcribe.assert_not_called()
