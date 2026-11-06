"""Lecture upload, status and a context-grounded AI tutor."""

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentUser, SessionDep, SettingsDep, owned_lecture
from app.api.limits import limit_chat, limit_upload
from app.models import AudioFile, LectureStatus
from app.schemas import ChatRequest, ChatResponse, LectureResponse, UploadResponse
from app.services.ai import AIService, AIUnavailable
from app.services.storage import save_upload
from app.worker import enqueue_lecture

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lectures", tags=["Lectures"])


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
        id=file_id, user_id=user.id, file_path=str(path), status=LectureStatus.pending
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
        # No successful upload is reported unless the broker accepted the task.
        # The original file is retained for diagnosis; no partial results are created.
        lecture.status = LectureStatus.failed
        lecture.error_message = "Task queue is unavailable. Upload the file again later."
        await session.commit()
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
    try:
        answer = await AIService(settings).answer_question(
            lecture.transcription.clean_text, body.question
        )
    except AIUnavailable:
        raise HTTPException(status_code=503, detail="AI provider is unavailable") from None
    return ChatResponse(answer=answer)
