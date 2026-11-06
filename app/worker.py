"""Celery entrypoint and a PostgreSQL lock preventing duplicate processing."""

import asyncio
from uuid import UUID

from celery import Celery
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.ai import AIService
from app.services.processing import process_lecture
from app.services.transcription import FasterWhisperTranscriber

# Configuration is loaded lazily so importing routes does not connect to Redis or PostgreSQL.
celery_app = Celery("eduflow")
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=5,
    broker_transport_options={"visibility_timeout": 43200, "socket_timeout": 5},
    task_publish_retry=False,
)


@celery_app.on_after_configure.connect  # type: ignore[untyped-decorator]
def configure_broker(sender: Celery, **kwargs: object) -> None:
    sender.conf.broker_url = get_settings().redis_url


def enqueue_lecture(file_id: UUID) -> None:
    """Publish only the identifier, never credentials or raw lecture text."""
    celery_app.conf.broker_url = get_settings().redis_url
    celery_app.send_task("eduflow.process_lecture", args=[str(file_id)], task_id=str(file_id))


async def run_lecture(file_id: UUID) -> None:
    settings = get_settings()
    # Celery invokes asyncio.run for each task: no pooled connections cross event loops.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lock_id = int.from_bytes(file_id.bytes[:8], byteorder="big", signed=True)
    try:
        async with engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}
            )
            await connection.commit()
            if not acquired:
                return
            try:
                await process_lecture(
                    file_id, factory, FasterWhisperTranscriber(settings), AIService(settings)
                )
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
                await connection.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="eduflow.process_lecture")  # type: ignore[untyped-decorator]
def process_lecture_task(file_id: str) -> None:
    """Worker jobs can be re-delivered safely; completed lectures are skipped."""
    asyncio.run(run_lecture(UUID(file_id)))
