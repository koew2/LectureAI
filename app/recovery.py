"""Re-publish unfinished lectures after restoring a DB and audio to a fresh Redis."""

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import AudioFile, LectureStatus
from app.worker import enqueue_lecture


async def unfinished_lectures() -> list[UUID]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine)
        async with factory() as session:
            ids = await session.scalars(
                select(AudioFile.id).where(
                    AudioFile.status.in_([LectureStatus.pending, LectureStatus.processing])
                )
            )
            return list(ids)
    finally:
        await engine.dispose()


def main() -> None:
    ids = asyncio.run(unfinished_lectures())
    for file_id in ids:
        enqueue_lecture(file_id)
    print(f"Published {len(ids)} unfinished lecture task(s).")


if __name__ == "__main__":
    main()
