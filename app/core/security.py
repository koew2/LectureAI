"""Argon2 password hashing and JWTs with separate access/refresh token types."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash
from pydantic import BaseModel

from app.core.config import Settings

password_hash = PasswordHash.recommended()
# A fixed-cost verification also runs when the email does not exist.
DUMMY_PASSWORD_HASH = password_hash.hash("not-a-user-password")


class TokenClaims(BaseModel):
    sub: UUID
    jti: UUID
    type: Literal["access", "refresh"]
    exp: int


def create_token(
    user_id: UUID,
    token_type: Literal["access", "refresh"],
    settings: Settings,
    *,
    jti: UUID | None = None,
) -> tuple[str, datetime, UUID]:
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=settings.access_token_minutes)
        if token_type == "access"
        else timedelta(days=settings.refresh_token_days)
    )
    expires_at = now + lifetime
    token_id = jti or uuid4()
    token = jwt.encode(
        {
            "sub": str(user_id),
            "jti": str(token_id),
            "type": token_type,
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return token, expires_at, token_id


def decode_token(
    token: str, expected_type: Literal["access", "refresh"], settings: Settings
) -> TokenClaims:
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require": ["sub", "jti", "type", "exp", "iat", "nbf", "iss", "aud"]},
    )
    claims = TokenClaims.model_validate(payload)
    if claims.type != expected_type:
        raise jwt.InvalidTokenError("Wrong token type")
    return claims
