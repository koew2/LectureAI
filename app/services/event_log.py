"""Store application events without copying request data or exception messages."""

import logging
from contextlib import suppress
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OperationalEvent

logger = logging.getLogger(__name__)
EventLevel = Literal["info", "error"]


def add_event(
    session: AsyncSession,
    event: str,
    *,
    user_id: UUID | None = None,
    lecture_id: UUID | None = None,
    error: BaseException | None = None,
    level: EventLevel | None = None,
    status_code: int | None = None,
) -> None:
    """Stage an event in the caller's transaction."""
    session.add(
        OperationalEvent(
            event=event,
            level=level or ("error" if error is not None else "info"),
            user_id=user_id,
            lecture_id=lecture_id,
            error_type=type(error).__name__[:80] if error is not None else None,
            status_code=status_code,
        )
    )


async def record_event(
    session: AsyncSession,
    event: str,
    *,
    user_id: UUID | None = None,
    lecture_id: UUID | None = None,
    error: BaseException | None = None,
    level: EventLevel | None = None,
    status_code: int | None = None,
) -> None:
    """Persist an event best effort when the session has no pending business writes."""
    add_event(
        session,
        event,
        user_id=user_id,
        lecture_id=lecture_id,
        error=error,
        level=level,
        status_code=status_code,
    )
    try:
        await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        with suppress(SQLAlchemyError, OSError):
            await session.rollback()
        logger.warning(
            "Could not store event %s for user %s, lecture %s (%s)",
            event,
            user_id,
            lecture_id,
            type(exc).__name__,
        )
