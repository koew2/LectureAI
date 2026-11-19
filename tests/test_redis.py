"""Run real atomic-counter checks when an isolated TEST_REDIS_URL is provided."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.rate_limit import RedisCounterStore


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = os.environ.get("TEST_REDIS_URL", "")
    if not url:
        pytest.skip("TEST_REDIS_URL is not configured")
    client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        yield client
    finally:
        await client.aclose()


async def test_real_redis_atomic_counter_with_concurrent_requests(redis_client: Redis) -> None:
    key = f"eduflow:test:{uuid4().hex}"
    try:
        store = RedisCounterStore(redis_client)
        results = await asyncio.gather(*[store.increment(key, 60) for _ in range(20)])
        assert sorted(count for count, _ in results) == list(range(1, 21))
        assert all(1 <= ttl <= 60 for _, ttl in results)
        assert await redis_client.ttl(key) > 0
    finally:
        await redis_client.delete(key)
