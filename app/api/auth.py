"""Registration, OAuth2 login and single-use refresh token rotation."""

from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep, SettingsDep, unauthorized
from app.api.limits import limit_auth
from app.core.config import Settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_token,
    decode_token,
    password_hash,
)
from app.models import RefreshSession, User
from app.schemas import RefreshRequest, RegisterRequest, TokenPair, UserResponse

router = APIRouter(prefix="/auth", tags=["Authentication"], dependencies=[Depends(limit_auth)])


def issue_tokens(user: User, session: AsyncSession, settings: Settings) -> TokenPair:
    access, _, _ = create_token(user.id, "access", settings)
    refresh, expires_at, token_id = create_token(user.id, "refresh", settings)
    session.add(RefreshSession(id=token_id, user_id=user.id, expires_at=expires_at))
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(body: RegisterRequest, session: SessionDep) -> User:
    """Create a user. Passwords are hashed with Argon2, never stored in plaintext."""
    hashed = await run_in_threadpool(password_hash.hash, body.password.get_secret_value())
    user = User(email=str(body.email).casefold(), hashed_password=hashed)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from exc
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> TokenPair:
    """OAuth2 form: pass the email address in the `username` field."""
    user = await session.scalar(select(User).where(User.email == form.username.casefold()))
    verified = await run_in_threadpool(
        password_hash.verify, form.password, user.hashed_password if user else DUMMY_PASSWORD_HASH
    )
    if user is None or not verified or not user.is_active:
        raise unauthorized()
    pair = issue_tokens(user, session, settings)
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, session: SessionDep, settings: SettingsDep, response: Response
) -> TokenPair:
    """Atomically revoke the old refresh token and issue a new pair."""
    try:
        claims = decode_token(body.refresh_token, "refresh", settings)
    except (jwt.InvalidTokenError, ValidationError, ValueError) as exc:
        raise unauthorized() from exc
    now = datetime.now(UTC)
    result = await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.id == claims.jti,
            RefreshSession.user_id == claims.sub,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(RefreshSession.user_id)
    )
    user_id = result.scalar_one_or_none()
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        await session.rollback()
        raise unauthorized()
    pair = issue_tokens(user, session, settings)
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return pair


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest, session: SessionDep, settings: SettingsDep) -> Response:
    """Revoke this refresh session; an issued access token lives until its expiry."""
    try:
        claims = decode_token(body.refresh_token, "refresh", settings)
    except (jwt.InvalidTokenError, ValidationError, ValueError) as exc:
        raise unauthorized() from exc
    await session.execute(
        update(RefreshSession)
        .where(RefreshSession.id == claims.jti, RefreshSession.user_id == claims.sub)
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    return Response(status_code=204)
