"""Isolated SQLite API tests; external AI and Redis are never contacted."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set before importing the app; never use a developer's real database or secrets.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["JWT_SECRET"] = "test-only-secret-at-least-32-bytes-long"

from app.core.config import Settings, get_settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite://",
        jwt_secret="test-only-secret-at-least-32-bytes-long",
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=2048,
        ai_model="test-model",
        openai_api_key="test-only-api-key",
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock()
    monkeypatch.setattr("app.api.lectures.enqueue_lecture", mock)
    return mock


@pytest.fixture
async def client(
    factory: async_sessionmaker[AsyncSession], settings: Settings, queue: Mock
) -> AsyncIterator[AsyncClient]:
    async def session_override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def register_and_login(
    client: AsyncClient, email: str = "student@example.com"
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "StrongPassword123!"}
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "StrongPassword123!"}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def tokens(client: AsyncClient) -> dict[str, str]:
    return await register_and_login(client)


@pytest.fixture
def auth_headers(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
