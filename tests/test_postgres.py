"""Real PostgreSQL checks run in CI; locally opt in via TEST_DATABASE_URL."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import Base
from tests.conftest import register_and_login

pytestmark = pytest.mark.postgres


@pytest.fixture
async def postgres_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.skip("TEST_DATABASE_URL is not configured")
    # Never drop a user's tables: isolate each test in a randomly named schema.
    schema = f"test_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_refresh_rotation_is_atomic_under_concurrency(
    client: AsyncClient, postgres_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def session_override() -> AsyncIterator[AsyncSession]:
        async with postgres_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    tokens = await register_and_login(client)
    responses = await asyncio.gather(
        *[
            client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 401]


async def test_postgres_advisory_lock_excludes_other_connections(
    postgres_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_factory() as first, postgres_factory() as second:
        key = int.from_bytes(uuid4().bytes[:8], "big", signed=True)
        assert await first.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        assert not await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        assert await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await second.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
