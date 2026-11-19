"""Lecture upload, status and a context-grounded AI tutor."""

import logging
from datetime import UTC, datetime
from pathlib import PureWindowsPath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentUser, SessionDep, SettingsDep, owned_lecture
from app.api.limits import limit_chat, limit_upload
from app.models import AudioFile, LectureProcessingResult, LectureStatus
from app.schemas import ChatRequest, ChatResponse, LectureListItem, LectureResponse, UploadResponse
from app.services.ai import AIService, AIUnavailable
from app.services.mail import notify_lecture_owner
from app.services.storage import save_upload
from app.worker import enqueue_lecture

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lectures", tags=["Lectures"])
QUEUE_ERROR = "Task queue is unavailable. Upload the file again later."


@router.get("", response_model=list[LectureListItem])
async def list_lectures(
    user: CurrentUser,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[AudioFile]:
    """Return the signed-in user's lectures, newest first, without loading their results."""
    statement = (
        select(AudioFile)
        .where(AudioFile.user_id == user.id)
        .order_by(AudioFile.created_at.desc(), AudioFile.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=202,
    dependencies=[Depends(limit_upload)],
)
async def upload_lecture(
    file: UploadFile, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> UploadResponse:
    """Accept mp3/wav/m4a for background validation, transcription and study generation."""
    file_id = uuid4()
    path = await save_upload(file, file_id, settings)
    lecture = AudioFile(
        id=file_id,
        user_id=user.id,
        file_path=str(path),
        filename=PureWindowsPath(file.filename or "").name.replace("\x00", "")[:255],
        status=LectureStatus.pending,
    )
    session.add(lecture)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        await run_in_threadpool(path.unlink, missing_ok=True)
        raise HTTPException(status_code=503, detail="Unable to store lecture") from None
    try:
        await run_in_threadpool(enqueue_lecture, file_id)
    except Exception:
        # A lost broker response can mean the worker already received the job.
        failed = await session.scalar(
            update(AudioFile)
            .where(AudioFile.id == file_id, AudioFile.status == LectureStatus.pending)
            .values(status=LectureStatus.failed, error_message=QUEUE_ERROR)
            .returning(AudioFile)
            .execution_options(populate_existing=True)
        )
        if failed is None:
            current_status = await session.scalar(
                select(AudioFile.status).where(AudioFile.id == file_id)
            )
            if current_status is not None:
                return UploadResponse(id=file_id, status=current_status)
            raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
        session.add(
            LectureProcessingResult(
                lecture_id=file_id,
                successful=False,
                error_message=QUEUE_ERROR,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await notify_lecture_owner(session, settings, failed)
        logger.warning("Queue unavailable for lecture %s", file_id)
        raise HTTPException(
            status_code=503,
            detail={"message": "Task queue is unavailable", "lecture_id": str(file_id)},
        ) from None
    return UploadResponse(id=file_id, status=LectureStatus.pending)


@router.get("/{file_id}", response_model=LectureResponse)
async def get_lecture(file_id: UUID, user: CurrentUser, session: SessionDep) -> AudioFile:
    """Return a lecture's status and results; other users' IDs return 404."""
    return await owned_lecture(file_id, user.id, session)


@router.post("/{file_id}/chat", response_model=ChatResponse, dependencies=[Depends(limit_chat)])
async def chat(
    file_id: UUID,
    body: ChatRequest,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatResponse:
    """Ask one independent question using the completed lecture as context."""
    lecture = await owned_lecture(file_id, user.id, session)
    if lecture.status != LectureStatus.completed or lecture.transcription is None:
        raise HTTPException(status_code=409, detail="Lecture processing is not completed")
    clean_text = lecture.transcription.clean_text
    await session.rollback()
    try:
        answer = await AIService(settings).answer_question(clean_text, body.question)
    except AIUnavailable:
        raise HTTPException(status_code=503, detail="AI provider is unavailable") from None
    return ChatResponse(answer=answer)
