from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import AudioFile, LectureProcessingResult, LectureStatus, Transcription
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
    assert result.json()["filename"] == "lecture.mp3"
    assert result.json()["transcription"] is None
    assert "file_path" not in result.json()
    other = await register_and_login(client, "other@example.com")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    library = await client.get("/api/v1/lectures?limit=1", headers=auth_headers)
    assert library.status_code == 200
    items = library.json()
    assert [(item["id"], item["filename"]) for item in items] == [(lecture_id, "lecture.mp3")]
    assert "file_path" not in items[0]
    assert (await client.get("/api/v1/lectures", headers=other_headers)).json() == []
    assert (await client.get("/api/v1/lectures?offset=1", headers=auth_headers)).json() == []
    assert (await client.get("/api/v1/lectures")).status_code == 401
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


async def test_lost_queue_response_does_not_overwrite_worker_result(
    client: AsyncClient,
    auth_headers: dict[str, str],
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def worker_finished_before_publish_error(_enqueue: Mock, file_id: UUID) -> None:
        async with factory() as session:
            lecture = await session.get(AudioFile, file_id)
            assert lecture is not None
            lecture.status = LectureStatus.completed
            session.add(LectureProcessingResult(lecture_id=file_id, successful=True))
            await session.commit()
        raise ConnectionError("Broker response lost")

    monkeypatch.setattr("app.api.lectures.run_in_threadpool", worker_finished_before_publish_error)
    notify = AsyncMock()
    monkeypatch.setattr("app.api.lectures.notify_lecture_owner", notify)
    response = await client.post(
        "/api/v1/lectures/upload", headers=auth_headers, files={"file": ("a.wav", b"x")}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "completed"
    notify.assert_not_awaited()
    async with factory() as session:
        results = (await session.scalars(select(LectureProcessingResult))).all()
        assert len(results) == 1 and results[0].successful


async def test_lecture_list_validates_offset(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/lectures?offset=2147483648", headers=auth_headers)
    assert response.status_code == 422


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


async def test_frontend_serves_the_app_and_assets(client: AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200 and 'lang="ru"' in page.text
    for asset in (
        "styles.css",
        "app.js",
        "study-illustration.jpg",
        "inter-cyrillic.woff2",
        "inter-latin.woff2",
    ):
        response = await client.get(f"/assets/{asset}")
        assert response.status_code == 200 and response.content
