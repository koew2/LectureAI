from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
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
