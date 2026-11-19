"""Registration, OAuth2 login and single-use refresh token rotation."""

import hmac
import logging
from datetime import UTC, datetime, timedelta
from secrets import randbelow
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DOCS_COOKIE, SessionDep, SettingsDep, is_admin, unauthorized
from app.api.limits import limit_auth
from app.core.config import Settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_token,
    decode_token,
    normalize_email,
    password_hash,
)
from app.models import RefreshSession, RegistrationCode, User
from app.schemas import (
    RefreshRequest,
    RegisterConfirmRequest,
    RegisterRequest,
    TokenPair,
    UserResponse,
)
from app.services.event_log import add_event, record_event
from app.services.mail import send_registration_code

router = APIRouter(prefix="/auth", tags=["Authentication"], dependencies=[Depends(limit_auth)])
logger = logging.getLogger(__name__)
CODE_LIFETIME = timedelta(minutes=10)
CODE_RESEND_DELAY = timedelta(minutes=1)
CODE_ATTEMPTS = 5
email_address = TypeAdapter(EmailStr)


def registration_code_digest(email: str, code: str, settings: Settings) -> str:
    key = settings.jwt_secret.get_secret_value().encode()
    return hmac.digest(key, f"registration:{email}:{code}".encode(), "sha256").hex()


def issue_tokens(
    user: User, session: AsyncSession, settings: Settings, response: Response, *, secure: bool
) -> TokenPair:
    access, _, _ = create_token(user.id, "access", settings)
    refresh, expires_at, token_id = create_token(user.id, "refresh", settings)
    session.add(RefreshSession(id=token_id, user_id=user.id, expires_at=expires_at))
    admin = is_admin(user, settings)
    if admin:
        docs_token, _, _ = create_token(user.id, "docs", settings, jti=token_id)
        response.set_cookie(
            DOCS_COOKIE,
            docs_token,
            httponly=True,
            secure=secure,
            samesite="strict",
        )
    else:
        response.delete_cookie(DOCS_COOKIE, httponly=True, secure=secure, samesite="strict")
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
        is_admin=admin,
    )


@router.post("/register", status_code=204)
async def register(body: RegisterRequest, session: SessionDep, settings: SettingsDep) -> Response:
    """Send a six-digit email code after validating the registration form."""
    email = normalize_email(body.email)
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")

    code = f"{randbelow(1_000_000):06d}"
    digest = registration_code_digest(email, code, settings)
    now = datetime.now(UTC)
    await session.execute(delete(RegistrationCode).where(RegistrationCode.expires_at <= now))
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgres_insert
    statement = insert(RegistrationCode).values(
        email=email, digest=digest, expires_at=now + CODE_LIFETIME, attempts_left=CODE_ATTEMPTS
    )
    saved = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[RegistrationCode.email],
            set_={
                "digest": statement.excluded.digest,
                "expires_at": statement.excluded.expires_at,
                "attempts_left": statement.excluded.attempts_left,
            },
            where=RegistrationCode.expires_at <= now + CODE_LIFETIME - CODE_RESEND_DELAY,
        ).returning(RegistrationCode.email)
    )
    if saved is None:
        await session.rollback()
        raise HTTPException(
            status_code=429, detail="Wait one minute before requesting another code"
        )
    await session.commit()
    try:
        await run_in_threadpool(send_registration_code, settings, email, code)
    except (OSError, ValueError) as exc:
        await session.execute(
            delete(RegistrationCode).where(
                RegistrationCode.email == email, RegistrationCode.digest == digest
            )
        )
        add_event(session, "registration_mail_failed", error=exc, status_code=503)
        await session.commit()
        logger.warning("Registration email delivery failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Unable to send registration code") from None
    return Response(status_code=204)


@router.post("/register/confirm", response_model=UserResponse, status_code=201)
async def confirm_registration(
    body: RegisterConfirmRequest, session: SessionDep, settings: SettingsDep
) -> User:
    """Create an account and consume its email code in the same transaction."""
    email = normalize_email(body.email)
    digest = await session.scalar(
        update(RegistrationCode)
        .where(
            RegistrationCode.email == email,
            RegistrationCode.expires_at > datetime.now(UTC),
            RegistrationCode.attempts_left > 0,
        )
        .values(attempts_left=RegistrationCode.attempts_left - 1)
        .returning(RegistrationCode.digest)
    )
    if digest is None or not hmac.compare_digest(
        digest, registration_code_digest(email, body.code, settings)
    ):
        await session.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired registration code")
    hashed = await run_in_threadpool(password_hash.hash, body.password.get_secret_value())
    await session.execute(delete(RegistrationCode).where(RegistrationCode.email == email))
    user = User(email=email, username=body.username, hashed_password=hashed)
    session.add(user)
    try:
        await session.flush()
        add_event(session, "user_registered", user_id=user.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from exc
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> TokenPair:
    """OAuth2 form: pass the email address in the `username` field."""
    try:
        email = normalize_email(email_address.validate_python(form.username))
    except ValidationError:
        user = None
    else:
        user = await session.scalar(select(User).where(User.email == email))
        legacy_email = email.casefold()
        if user is None and legacy_email != email:
            user = await session.scalar(select(User).where(User.email == legacy_email))
    verified = await run_in_threadpool(
        password_hash.verify, form.password, user.hashed_password if user else DUMMY_PASSWORD_HASH
    )
    if user is None or not verified or not user.is_active:
        if user is not None:
            await record_event(
                session, "login_failed", user_id=user.id, level="error", status_code=401
            )
        raise unauthorized()
    pair = issue_tokens(user, session, settings, response, secure=request.url.scheme == "https")
    add_event(session, "login_succeeded", user_id=user.id)
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    request: Request,
    body: RefreshRequest,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
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
    pair = issue_tokens(user, session, settings, response, secure=request.url.scheme == "https")
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return pair


@router.post("/logout", status_code=204)
async def logout(
    request: Request, body: RefreshRequest, session: SessionDep, settings: SettingsDep
) -> Response:
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
    response = Response(status_code=204)
    response.delete_cookie(
        DOCS_COOKIE, httponly=True, secure=request.url.scheme == "https", samesite="strict"
    )
    return response
