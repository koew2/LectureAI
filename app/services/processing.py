"""Atomic persistence of study results and explicit background job states."""

import asyncio
import logging
import re
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AudioFile, Flashcard, LectureStatus, Summary, Transcription
from app.services.ai import AIService, AIUnavailable
from app.services.transcription import InvalidAudio, Transcriber

logger = logging.getLogger(__name__)


async def process_lecture(
    file_id: UUID,
    factory: async_sessionmaker[AsyncSession],
    transcriber: Transcriber,
    ai: AIService,
) -> None:
    """Process one lecture; caller must hold the per-lecture advisory lock."""
    async with factory() as session:
        lecture = await session.get(AudioFile, file_id)
        if lecture is None or lecture.status == LectureStatus.completed:
            return
        lecture.status = LectureStatus.processing
        lecture.error_message = None
        await session.commit()
        try:
            transcript = await asyncio.to_thread(transcriber.transcribe, Path(lecture.file_path))
            clean_text = re.sub(r"\s+", " ", transcript.raw_text).strip()
            notes = await ai.generate_notes(clean_text)

            # A re-delivered job replaces any old results in the same transaction.
            old_summary_ids = await session.scalars(
                select(Summary.id).where(Summary.file_id == file_id)
            )
            for summary_id in old_summary_ids:
                await session.execute(delete(Flashcard).where(Flashcard.summary_id == summary_id))
            await session.execute(delete(Summary).where(Summary.file_id == file_id))
            await session.execute(delete(Transcription).where(Transcription.file_id == file_id))
            session.add(
                Transcription(
                    file_id=file_id,
                    raw_text=transcript.raw_text,
                    clean_text=clean_text,
                    timestamps_json=transcript.timestamps,
                )
            )
            summary = Summary(
                file_id=file_id,
                structured_markdown=notes.structured_markdown,
                key_points_json=notes.key_points_json,
                flashcards=[
                    Flashcard(question=c.question, answer=c.answer) for c in notes.flashcards
                ],
            )
            session.add(summary)
            lecture.duration = transcript.duration
            lecture.status = LectureStatus.completed
            await session.commit()
        except Exception as exc:
            await session.rollback()
            failed = await session.get(AudioFile, file_id)
            if failed is not None:
                failed.status = LectureStatus.failed
                if isinstance(exc, InvalidAudio):
                    failed.error_message = str(exc)
                elif isinstance(exc, AIUnavailable):
                    failed.error_message = (
                        "AI processing failed. Check provider configuration or limits."
                    )
                else:
                    failed.error_message = (
                        "Lecture processing failed. Contact the project maintainer."
                    )
                await session.commit()
            # Avoid logging provider payloads, lecture text, credentials or filesystem paths.
            logger.error("Lecture %s failed (%s)", file_id, type(exc).__name__)
            raise
