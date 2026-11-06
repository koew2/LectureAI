from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import AudioFile, LectureStatus, Transcription
from app.services.ai import AIUnavailable
from tests.conftest import register_and_login


async def test_upload_status_and_ownership(
    client: AsyncClient, auth_headers: dict[str, str], queue: Mock, settings: Settings
) -> None:
    response = await client.post(
        "/api/v1/lectures/upload",
        headers=auth_headers,
        files={"file": ("../../lecture.mp3", b"fake-audio-for-api-test", "audio/mpeg")},
    )
    assert response.status_code == 202
    lecture_id = response.json()["id"]
    queue.assert_called_once_with(UUID(lecture_id))
    files = list(settings.upload_dir.iterdir())
    assert len(files) == 1 and files[0].name == f"{lecture_id}.mp3"
    result = await client.get(f"/api/v1/lectures/{lecture_id}", headers=auth_headers)
    assert result.status_code == 200
    assert result.json()["status"] == "pending"
    assert result.json()["transcription"] is None
    assert "file_path" not in result.json()
    other = await register_and_login(client, "other@example.com")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert (
        await client.get(f"/api/v1/lectures/{lecture_id}", headers=other_headers)
    ).status_code == 404
    response = await client.post(
        f"/api/v1/lectures/{lecture_id}/chat", headers=other_headers, json={"question": "What?"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "filename,content,expected",
    [
        ("malware.exe", b"data", 415),
        ("empty.wav", b"", 400),
        ("big.m4a", b"x" * 2049, 413),
    ],
)
async def test_upload_rejections_clean_up_files(
    client: AsyncClient,
    auth_headers: dict[str, str],
    settings: Settings,
    filename: str,
    content: bytes,
    expected: int,
) -> None:
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": (filename, content)}
    )
    assert response.status_code == expected
    assert not settings.upload_dir.exists() or not list(settings.upload_dir.iterdir())


async def test_upload_requires_auth(client: AsyncClient, queue: Mock) -> None:
    response = await client.post("/api/v1/lectures/upload", files={"file": ("a.mp3", b"x")})
    assert response.status_code == 401
    queue.assert_not_called()


async def test_queue_failure_records_failed_status(
    client: AsyncClient,
    auth_headers: dict[str, str],
    queue: Mock,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    queue.side_effect = ConnectionError("private connection details")
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": ("a.mp3", b"x")}
    )
    assert response.status_code == 503
    assert "private" not in response.text
    async with factory() as session:
        lecture = await session.scalar(select(AudioFile))
        assert lecture is not None and lecture.status == LectureStatus.failed


async def test_chat_requires_completed_lecture(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": ("a.mp3", b"x")}
    )
    lecture_id = response.json()["id"]
    response = await client.post(
        f"/api/v1/lectures/{lecture_id}/chat", headers=auth_headers, json={"question": "Explain"}
    )
    assert response.status_code == 409


async def test_chat_success_and_provider_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": ("a.mp3", b"x")}
    )
    lecture_id = UUID(response.json()["id"])
    async with factory() as session:
        lecture = await session.get(AudioFile, lecture_id)
        assert lecture is not None
        lecture.status = LectureStatus.completed
        session.add(
            Transcription(
                file_id=lecture_id,
                raw_text="Lecture content",
                clean_text="Lecture content",
                timestamps_json=[],
            )
        )
        await session.commit()
    mock = AsyncMock(return_value="Tutor answer")
    monkeypatch.setattr("app.api.lectures.AIService.answer_question", mock)
    response = await client.post(
        f"/api/v1/lectures/{lecture_id}/chat", headers=auth_headers, json={"question": "Explain"}
    )
    assert response.json() == {"answer": "Tutor answer"}
    mock.assert_awaited_once_with("Lecture content", "Explain")
    mock.side_effect = AIUnavailable("secret-provider-data")
    response = await client.post(
        f"/api/v1/lectures/{lecture_id}/chat", headers=auth_headers, json={"question": "Explain"}
    )
    assert response.status_code == 503 and "secret-provider-data" not in response.text


async def test_missing_lecture_and_invalid_id(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    assert (
        await client.get(f"/api/v1/lectures/{uuid4()}", headers=auth_headers)
    ).status_code == 404
    assert (
        await client.get("/api/v1/lectures/not-a-uuid", headers=auth_headers)
    ).status_code == 422


async def test_swagger_documents_security_and_endpoints(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/auth/refresh" in schema["paths"]
    assert "/api/v1/lectures/{file_id}/chat" in schema["paths"]
    assert schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["type"] == "oauth2"
    assert (await client.get("/docs")).status_code == 200
