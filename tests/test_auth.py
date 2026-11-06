from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import create_token, password_hash
from app.models import User


async def test_register_stores_hash_not_password(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "STUDENT@example.com", "password": "StrongPassword123!"},
    )
    assert response.status_code == 201
    assert "password" not in response.text
    assert response.json()["email"] == "student@example.com"
    async with factory() as session:
        user = await session.scalar(select(User))
        assert user is not None
        assert user.hashed_password != "StrongPassword123!"
        assert password_hash.verify("StrongPassword123!", user.hashed_password)
    duplicate = await client.post(
        "/api/v1/auth/register",
        json={"email": "student@example.com", "password": "StrongPassword123!"},
    )
    assert duplicate.status_code == 409


@pytest.mark.parametrize("email,password", [("not-an-email", "xP7!2"), ("a@example.com", "xP7!2")])
async def test_invalid_registration_does_not_echo_password(
    client: AsyncClient, email: str, password: str
) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 422
    assert password not in response.text


async def test_bad_password_and_unknown_user(client: AsyncClient, tokens: dict[str, str]) -> None:
    for username in ["student@example.com", "missing@example.com"]:
        response = await client.post(
            "/api/v1/auth/login", data={"username": username, "password": "wrong"}
        )
        assert response.status_code == 401


async def test_refresh_is_single_use_and_logout_revokes(
    client: AsyncClient, tokens: dict[str, str]
) -> None:
    old = {"refresh_token": tokens["refresh_token"]}
    rotated = await client.post("/api/v1/auth/refresh", json=old)
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != old["refresh_token"]
    assert rotated.headers["cache-control"] == "no-store"
    assert (await client.post("/api/v1/auth/refresh", json=old)).status_code == 401
    current = {"refresh_token": rotated.json()["refresh_token"]}
    assert (await client.post("/api/v1/auth/logout", json=current)).status_code == 204
    assert (await client.post("/api/v1/auth/refresh", json=current)).status_code == 401


async def test_access_and_refresh_cannot_be_interchanged(
    client: AsyncClient, tokens: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401
    response = await client.get(
        f"/api/v1/lectures/{uuid4()}",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert response.status_code == 401


async def test_expired_and_tampered_tokens(client: AsyncClient, settings: Settings) -> None:
    token, _, _ = create_token(uuid4(), "access", settings)
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    for bad_token in [expired, "invalid.jwt.value"]:
        response = await client.get(
            f"/api/v1/lectures/{uuid4()}", headers={"Authorization": f"Bearer {bad_token}"}
        )
        assert response.status_code == 401


async def test_inactive_user_is_denied(
    client: AsyncClient, tokens: dict[str, str], factory: async_sessionmaker[AsyncSession]
) -> None:
    async with factory() as session:
        user = await session.scalar(select(User))
        assert user is not None
        user.is_active = False
        await session.commit()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get(f"/api/v1/lectures/{uuid4()}", headers=headers)).status_code == 401
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401
