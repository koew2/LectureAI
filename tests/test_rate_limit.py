from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.main import app
from app.services.rate_limit import CounterStore, RateLimiter, RedisCounterStore


async def test_rate_limit_rejects_with_retry_after_and_hides_identity() -> None:
    store = Mock(spec=CounterStore)
    store.increment = AsyncMock(side_effect=[(1, 60), (2, 59), (3, 58), (1, 60)])
    limiter = RateLimiter(store)
    for _ in range(2):
        await limiter.check("auth", "192.0.2.1", 2, 60)
    with pytest.raises(HTTPException) as error:
        await limiter.check("auth", "192.0.2.1", 2, 60)
    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "58"}
    assert "192.0.2.1" not in store.increment.call_args.args[0]
    # Once the Redis window expires, a new request is allowed.
    await limiter.check("auth", "192.0.2.1", 2, 60)


async def test_redis_failure_fails_closed() -> None:
    store = Mock(spec=CounterStore)
    store.increment = AsyncMock(side_effect=RedisConnectionError("private connection details"))
    with pytest.raises(HTTPException) as error:
        await RateLimiter(store).check("auth", "192.0.2.1", 10, 60)
    assert error.value.status_code == 503
    assert "private" not in error.value.detail


async def test_redis_counter_executes_one_atomic_script() -> None:
    client = Mock()
    client.eval = AsyncMock(return_value=[11, 42])
    assert await RedisCounterStore(client).increment("key", 60) == (11, 42)
    script, key_count, key, seconds = client.eval.call_args.args
    assert "EXPIRE" in script and "INCR" in script
    assert (key_count, key, seconds) == (1, "key", "60")


async def test_auth_limit_uses_peer_ip_and_rejects_before_password_hashing(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.rate_limit_enabled = True
    limiter = Mock(spec=RateLimiter)
    limiter.check = AsyncMock(
        side_effect=HTTPException(status_code=429, detail="Too many requests")
    )
    monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)
    verify = Mock()
    monkeypatch.setattr("app.api.auth.password_hash.verify", verify)
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": "student@example.com", "password": "irrelevant"},
        headers={"X-Forwarded-For": "203.0.113.100"},
    )
    assert response.status_code == 429
    verify.assert_not_called()
    assert limiter.check.call_args.args == ("auth", "127.0.0.1", 10, 60)


async def test_upload_limit_checks_account_before_saving_or_enqueuing(
    client: AsyncClient,
    auth_headers: dict[str, str],
    settings: Settings,
    queue: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.rate_limit_enabled = True
    limiter = Mock(spec=RateLimiter)
    limiter.check = AsyncMock(
        side_effect=HTTPException(status_code=429, detail="Too many requests")
    )
    monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": ("a.mp3", b"data")}
    )
    assert response.status_code == 429
    assert not settings.upload_dir.exists()
    queue.assert_not_called()
    assert limiter.check.call_args.args[0] == "upload"


async def test_chat_limit_rejects_before_provider_call(
    client: AsyncClient,
    auth_headers: dict[str, str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    settings.rate_limit_enabled = True
    limiter = Mock(spec=RateLimiter)
    limiter.check = AsyncMock(
        side_effect=HTTPException(status_code=429, detail="Too many requests")
    )
    monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)
    answer = AsyncMock()
    monkeypatch.setattr("app.api.lectures.AIService.answer_question", answer)
    response = await client.post(
        f"/api/v1/lectures/{uuid4()}/chat", headers=auth_headers, json={"question": "Explain"}
    )
    assert response.status_code == 429
    answer.assert_not_called()
    assert limiter.check.call_args.args[0] == "chat"


async def test_enabled_limiter_missing_from_state_returns_503(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.rate_limit_enabled = True
    monkeypatch.setattr(app.state, "rate_limiter", None, raising=False)
    response = await client.post(
        "/api/v1/auth/login", data={"username": "student@example.com", "password": "anything"}
    )
    assert response.status_code == 503
