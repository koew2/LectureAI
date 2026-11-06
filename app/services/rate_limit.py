"""Shared Redis fixed-window limits, checked before expensive endpoint work."""

import hashlib
from collections.abc import Awaitable, Sequence
from typing import Protocol, cast

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

# Count and set expiry together, so a crash cannot leave a counter without a TTL.
COUNTER_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


class CounterStore(Protocol):
    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]: ...


class RedisCounterStore:
    def __init__(self, client: Redis) -> None:
        self.client = client

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        # Redis's shared command mixin types describe both sync and async clients.
        result = await cast(
            Awaitable[Sequence[int]],
            self.client.eval(COUNTER_SCRIPT, 1, key, str(window_seconds)),
        )
        return int(result[0]), max(1, int(result[1]))


class RateLimiter:
    def __init__(self, store: CounterStore) -> None:
        self.store = store

    async def check(self, category: str, identity: str, limit: int, window_seconds: int) -> None:
        # Redis keys need no raw IP, email or bearer token.
        digest = hashlib.sha256(identity.encode()).hexdigest()
        key = f"eduflow:rate-limit:{category}:{window_seconds}:{digest}"
        try:
            count, retry_after = await self.store.increment(key, window_seconds)
        except RedisError:
            raise HTTPException(status_code=503, detail="Request limiter is unavailable") from None
        if count > limit:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=503, detail="Request limiter is unavailable")
    return limiter
