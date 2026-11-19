"""Dependencies enforcing authentication and resource ownership."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.security import decode_token, normalize_email
from app.db import get_session
from app.models import AudioFile, RefreshSession, Summary, User

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
docs_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
DOCS_COOKIE = "eduflow_docs"


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> User:
    try:
        claims = decode_token(token, "access", settings)
    except (jwt.InvalidTokenError, ValidationError, ValueError) as exc:
        raise unauthorized() from exc
    user = await session.get(User, claims.sub)
    if user is None or not user.is_active:
        raise unauthorized()
    request.state.event_user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def is_admin(user: User, settings: Settings) -> bool:
    return settings.admin_email is not None and user.email == normalize_email(settings.admin_email)


def require_admin(user: CurrentUser, settings: SettingsDep) -> None:
    if not is_admin(user, settings):
        raise HTTPException(status_code=403, detail="Administrator access required")


async def require_docs_admin(
    request: Request,
    access_token: Annotated[str | None, Depends(docs_bearer)],
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    if access_token is not None:
        require_admin(await get_current_user(access_token, session, settings, request), settings)
        return

    try:
        claims = decode_token(request.cookies.get(DOCS_COOKIE, ""), "docs", settings)
    except (jwt.InvalidTokenError, ValidationError, ValueError) as exc:
        raise unauthorized() from exc
    user = await session.scalar(
        select(User)
        .join(RefreshSession, RefreshSession.user_id == User.id)
        .where(
            User.id == claims.sub,
            User.is_active.is_(True),
            RefreshSession.id == claims.jti,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > datetime.now(UTC),
        )
    )
    if user is None:
        raise unauthorized()
    request.state.event_user_id = user.id
    require_admin(user, settings)


async def owned_lecture(file_id: UUID, user_id: UUID, session: AsyncSession) -> AudioFile:
    statement = (
        select(AudioFile)
        .where(AudioFile.id == file_id, AudioFile.user_id == user_id)
        .options(
            selectinload(AudioFile.transcription),
            selectinload(AudioFile.summary).selectinload(Summary.flashcards),
        )
    )
    lecture = await session.scalar(statement)
    if lecture is None:
        raise HTTPException(status_code=404, detail="Lecture not found")
    return lecture
