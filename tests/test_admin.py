from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import app
from app.models import AudioFile, LectureProcessingResult, LectureStatus, User
from tests.conftest import register_and_login


async def test_admin_processing_history_permissions_and_outcomes(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    settings.admin_email = "admin@example.com"
    administrator = await register_and_login(client, "admin@example.com")
    member = await register_and_login(client)
    assert administrator["is_admin"] is True and member["is_admin"] is False
    admin_headers = {"Authorization": f"Bearer {administrator['access_token']}"}
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}

    async with factory() as session:
        owner = await session.scalar(select(User).where(User.email == "student@example.com"))
        assert owner is not None
        lecture = AudioFile(user_id=owner.id, file_path="/private/audio.wav", filename="audio.wav")
        pending = AudioFile(user_id=owner.id, file_path="/private/pending.wav")
        session.add_all([lecture, pending])
        await session.flush()
        session.add(
            LectureProcessingResult(lecture_id=lecture.id, successful=False, error_message="Error")
        )
        await session.flush()
        lecture.status = LectureStatus.completed
        session.add(LectureProcessingResult(lecture_id=lecture.id, successful=True))
        await session.commit()

    path = "/api/v1/admin/processing"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=member_headers)).status_code == 403
    invalid_cursor = await client.get(path, headers=admin_headers, params={"before": 2147483648})
    assert invalid_cursor.status_code == 422
    response = await client.get(path, headers=admin_headers, params={"limit": 1})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "/private/" not in response.text
    page = response.json()
    assert page["successful_count"] == page["failed_count"] == 1
    assert page["items"][0]["successful"] is True
    assert page["items"][0]["username"] == "Тестовый пользователь"
    next_page = await client.get(
        path, headers=admin_headers, params={"before": page["next_before"]}
    )
    failed = await client.get(path, headers=admin_headers, params={"result": "failed"})
    assert next_page.json()["items"] == failed.json()["items"]
    assert failed.json()["items"][0]["successful"] is False
    assert failed.json()["items"][0]["lecture_id"] == page["items"][0]["lecture_id"]
    settings.admin_email = None
    assert (await client.get(path, headers=admin_headers)).status_code == 403


async def test_events_keep_actor_and_codes_without_sensitive_details(
    client: AsyncClient, settings: Settings, queue: Mock, mail: Mock
) -> None:
    settings.admin_email = "admin@example.com"
    administrator = await register_and_login(client, "admin@example.com")
    member = await register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {administrator['access_token']}"}
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}
    path = "/api/v1/admin/events"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=member_headers)).status_code == 403

    invalid_login = await client.post(
        "/api/v1/auth/login",
        data={"username": "student@example.com", "password": "private-password"},
    )
    assert invalid_login.status_code == 401
    bad_upload = await client.post(
        "/api/v1/lectures/upload", headers=member_headers, files={"file": ("text.txt", b"x")}
    )
    assert bad_upload.status_code == 415
    queue.side_effect = ConnectionError("redis://private-queue-address")
    queued = await client.post(
        "/api/v1/lectures/upload", headers=member_headers, files={"file": ("lecture.wav", b"x")}
    )
    assert queued.status_code == 503
    lecture_id = queued.json()["detail"]["lecture_id"]
    mail.side_effect = OSError("private-mail-payload")
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "not-in-journal@example.com",
            "username": "Visitor",
            "password": "secretPassword!",
            "password_confirmation": "secretPassword!",
        },
    )
    assert registration.status_code == 503

    result = await client.get(path, headers=admin_headers, params={"level": "error", "limit": 2})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    for secret in (
        "private-password",
        "private-queue-address",
        "private-mail-payload",
        "not-in-journal",
    ):
        assert secret not in result.text
    page = result.json()
    mail_event, queue_event = page["items"]
    assert mail_event["event"] == "registration_mail_failed" and mail_event["username"] is None
    assert queue_event["event"] == "queue_failed" and queue_event["lecture_id"] == lecture_id
    assert queue_event["error_type"] == "ConnectionError" and queue_event["status_code"] == 503
    assert queue_event["username"] == "Тестовый пользователь"
    assert queue_event["user_email"] == "student@example.com"
    assert queue_event["created_at"].endswith("Z")
    next_page = await client.get(
        path, headers=admin_headers, params={"level": "error", "before": page["next_before"]}
    )
    assert [(entry["event"], entry["status_code"]) for entry in next_page.json()["items"]] == [
        ("upload_rejected", 415),
        ("login_failed", 401),
    ]
    info = await client.get(path, headers=admin_headers, params={"level": "info"})
    assert {entry["event"] for entry in info.json()["items"]} == {
        "user_registered",
        "login_succeeded",
        "lecture_uploaded",
    }


async def test_unhandled_api_error_records_safe_event_for_user(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.admin_email = "admin@example.com"
    administrator = await register_and_login(client, "admin@example.com")
    member = await register_and_login(client)
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}
    admin_headers = {"Authorization": f"Bearer {administrator['access_token']}"}
    monkeypatch.setattr(app.state, "session_factory", factory, raising=False)
    monkeypatch.setattr(
        "app.api.lectures.owned_lecture", AsyncMock(side_effect=RuntimeError("private message"))
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as internal_client:
        failed = await internal_client.get(
            "/api/v1/lectures/13a04faa-8ce3-4114-b3cc-c646555449f0", headers=member_headers
        )
    assert failed.status_code == 500 and "private message" not in failed.text
    logs = await client.get(
        "/api/v1/admin/events", headers=admin_headers, params={"level": "error"}
    )
    assert "private message" not in logs.text
    [entry] = logs.json()["items"]
    assert (entry["event"], entry["error_type"], entry["status_code"]) == (
        "api_error",
        "RuntimeError",
        500,
    )
    assert entry["user_email"] == "student@example.com"
