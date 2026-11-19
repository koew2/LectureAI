from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import create_token, password_hash
from app.models import RegistrationCode, User
from tests.conftest import register_and_login


async def test_register_stores_hash_not_password(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], mail: Mock
) -> None:
    details = {
        "email": "STUDENT@example.com",
        "username": "  Алексей  ",
        "password": "StrongPassword123!",
        "password_confirmation": "StrongPassword123!",
    }
    sent = await client.post("/api/v1/auth/register", json=details)
    assert sent.status_code == 204 and not sent.content
    assert mail.call_args.args[1:] == ("student@example.com", "123456")
    async with factory() as session:
        assert await session.scalar(select(User)) is None
        challenge = await session.get(RegistrationCode, "student@example.com")
        assert challenge is not None and challenge.digest != "123456"
    assert (await client.post("/api/v1/auth/register", json=details)).status_code == 429
    wrong = await client.post("/api/v1/auth/register/confirm", json={**details, "code": "000000"})
    assert wrong.status_code == 400
    response = await client.post(
        "/api/v1/auth/register/confirm", json={**details, "code": "123456"}
    )
    assert response.status_code == 201
    assert "password" not in response.text
    assert response.json()["email"] == "student@example.com"
    assert response.json()["username"] == "Алексей"
    async with factory() as session:
        user = await session.scalar(select(User))
        assert user is not None
        assert user.hashed_password != "StrongPassword123!"
        assert password_hash.verify("StrongPassword123!", user.hashed_password)
    reused = await client.post("/api/v1/auth/register/confirm", json={**details, "code": "123456"})
    assert reused.status_code == 400
    duplicate = await client.post("/api/v1/auth/register", json=details)
    assert duplicate.status_code == 409


@pytest.mark.parametrize("email,password", [("not-an-email", "xP7!2"), ("a@example.com", "xP7!2")])
async def test_invalid_registration_does_not_echo_password(
    client: AsyncClient, email: str, password: str
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "username": "Алексей",
            "password": password,
            "password_confirmation": password,
        },
    )
    assert response.status_code == 422
    assert password not in response.text


async def test_registration_requires_matching_passwords(client: AsyncClient, mail: Mock) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "student@example.com",
            "username": "Алексей",
            "password": "StrongPassword123!",
            "password_confirmation": "DifferentPassword123!",
        },
    )
    assert response.status_code == 422
    assert "DifferentPassword123!" not in response.text
    mail.assert_not_called()


async def test_registration_rejects_unpaired_surrogates(client: AsyncClient, mail: Mock) -> None:
    body = (
        r'{"email":"student@example.com","username":"Student",'
        r'"password":"ExamplePassword\uD800","password_confirmation":"ExamplePassword\uD800"}'
    )
    response = await client.post(
        "/api/v1/auth/register", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "ExamplePassword" not in response.text
    mail.assert_not_called()


async def test_registration_requires_a_name(client: AsyncClient, mail: Mock) -> None:
    details = {
        "email": "student@example.com",
        "password": "StrongPassword123!",
        "password_confirmation": "StrongPassword123!",
    }
    assert (await client.post("/api/v1/auth/register", json=details)).status_code == 422
    for path, body in [
        ("/register", {**details, "username": "  "}),
        ("/register/confirm", {**details, "code": "123456"}),
    ]:
        assert (await client.post(f"/api/v1/auth{path}", json=body)).status_code == 422
    mail.assert_not_called()


async def test_registration_code_attempts_and_delivery_failure(
    client: AsyncClient, mail: Mock
) -> None:
    details = {
        "email": "student@example.com",
        "username": "Алексей",
        "password": "StrongPassword123!",
        "password_confirmation": "StrongPassword123!",
    }
    mail.side_effect = OSError("private transport details")
    failed = await client.post("/api/v1/auth/register", json=details)
    assert failed.status_code == 503 and "private transport details" not in failed.text
    mail.side_effect = None
    assert (await client.post("/api/v1/auth/register", json=details)).status_code == 204
    for code in ["000000"] * 5 + ["123456"]:
        result = await client.post("/api/v1/auth/register/confirm", json={**details, "code": code})
        assert result.status_code == 400


async def test_bad_password_and_unknown_user(client: AsyncClient, tokens: dict[str, str]) -> None:
    for username in ["student@example.com", "missing@example.com"]:
        response = await client.post(
            "/api/v1/auth/login", data={"username": username, "password": "wrong"}
        )
        assert response.status_code == 401


async def test_login_accepts_ascii_form_of_internationalized_email_domain(
    client: AsyncClient,
) -> None:
    await register_and_login(client, "student@bücher.de")
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": "student@xn--bcher-kva.de", "password": "StrongPassword123!"},
    )
    assert response.status_code == 200


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
