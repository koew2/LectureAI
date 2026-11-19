from unittest.mock import AsyncMock, Mock

import pytest
from httpx import AsyncClient

from app.main import app


async def test_readiness_handles_postgres_connection_errors(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Mock()
    session.execute = AsyncMock(side_effect=ConnectionRefusedError("private address"))
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(app.state, "session_factory", Mock(return_value=context), raising=False)

    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database is unavailable"}
