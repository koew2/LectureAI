"""Dependencies enforcing authentication and resource ownership."""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db import get_session
from app.models import AudioFile, Summary, User

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], session: SessionDep, settings: SettingsDep
) -> User:
    try:
        claims = decode_token(token, "access", settings)
    except (jwt.InvalidTokenError, ValidationError, ValueError) as exc:
        raise unauthorized() from exc
    user = await session.get(User, claims.sub)
    if user is None or not user.is_active:
        raise unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


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
