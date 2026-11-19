"""Read-only lecture history and events for the configured administrator."""

from datetime import UTC
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from app.api.deps import SessionDep, require_admin
from app.models import AudioFile, LectureProcessingResult, OperationalEvent, User
from app.schemas import AdminEventItem, AdminEventPage, AdminProcessingItem, AdminProcessingPage

router = APIRouter(prefix="/admin", tags=["Administration"], dependencies=[Depends(require_admin)])


@router.get("/events", response_model=AdminEventPage)
async def operational_events(
    session: SessionDep,
    response: Response,
    level: Literal["all", "info", "error"] = "all",
    before: Annotated[int | None, Query(gt=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> AdminEventPage:
    event = OperationalEvent
    statement = (
        select(event, User.username, User.email)
        .outerjoin(User, User.id == event.user_id)
        .order_by(event.id.desc())
        .limit(limit + 1)
    )
    if level != "all":
        statement = statement.where(event.level == level)
    if before is not None:
        statement = statement.where(event.id < before)
    rows = (await session.execute(statement)).tuples().all()
    response.headers["Cache-Control"] = "no-store"
    return AdminEventPage(
        items=[
            AdminEventItem(
                id=entry.id,
                created_at=entry.created_at.replace(tzinfo=entry.created_at.tzinfo or UTC),
                level="error" if entry.level == "error" else "info",
                event=entry.event,
                username=username,
                user_email=email,
                lecture_id=entry.lecture_id,
                error_type=entry.error_type,
                status_code=entry.status_code,
            )
            for entry, username, email in rows[:limit]
        ],
        next_before=rows[limit - 1][0].id if len(rows) > limit else None,
    )


@router.get("/processing", response_model=AdminProcessingPage)
async def processing_history(
    session: SessionDep,
    response: Response,
    result: Literal["all", "completed", "failed"] = "all",
    before: Annotated[int | None, Query(gt=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> AdminProcessingPage:
    outcome = LectureProcessingResult
    counts = dict(
        (
            await session.execute(
                select(outcome.successful, func.count()).group_by(outcome.successful)
            )
        )
        .tuples()
        .all()
    )
    statement = (
        select(outcome, AudioFile.filename, AudioFile.created_at, User.username, User.email)
        .join(AudioFile, AudioFile.id == outcome.lecture_id)
        .join(User, User.id == AudioFile.user_id)
        .order_by(outcome.id.desc())
        .limit(limit + 1)
    )
    if result != "all":
        statement = statement.where(outcome.successful.is_(result == "completed"))
    if before is not None:
        statement = statement.where(outcome.id < before)
    rows = (await session.execute(statement)).tuples().all()
    response.headers["Cache-Control"] = "no-store"
    return AdminProcessingPage(
        items=[
            AdminProcessingItem(
                lecture_id=entry.lecture_id,
                filename=filename,
                username=username,
                user_email=email,
                successful=entry.successful,
                finished_at=entry.finished_at,
                uploaded_at=uploaded_at,
                error_message=entry.error_message,
            )
            for entry, filename, uploaded_at, username, email in rows[:limit]
        ],
        next_before=rows[limit - 1][0].id if len(rows) > limit else None,
        successful_count=counts.get(True, 0),
        failed_count=counts.get(False, 0),
    )
