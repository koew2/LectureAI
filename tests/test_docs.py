from httpx import URL, AsyncClient

from app.api.deps import DOCS_COOKIE
from app.core.config import Settings
from tests.conftest import register_and_login

DOCS_PATHS = ("/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect")


async def test_documentation_is_only_served_to_admins(
    client: AsyncClient, settings: Settings
) -> None:
    settings.admin_email = "admin@example.com"
    for path in DOCS_PATHS:
        assert (await client.get(path)).status_code == 401

    member = await register_and_login(client)
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}
    assert DOCS_COOKIE not in client.cookies
    for path in DOCS_PATHS:
        assert (await client.get(path, headers=member_headers)).status_code == 403

    admin = await register_and_login(client, settings.admin_email)
    for path in DOCS_PATHS:
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert "/openapi.json" in (await client.get("/docs")).text
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/auth/refresh" in schema["paths"]
    assert "/api/v1/lectures/{file_id}/chat" in schema["paths"]
    assert schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["type"] == "oauth2"
    assert all(path not in schema["paths"] for path in DOCS_PATHS)
    assert (await client.get("/api/v1/admin/processing")).status_code == 401

    settings.admin_email = None
    assert (await client.get("/docs")).status_code == 403
    settings.admin_email = "admin@example.com"
    client.cookies.clear()
    admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}
    assert (await client.get("/openapi.json", headers=admin_headers)).status_code == 200


async def test_docs_cookie_rotates_and_is_revoked_on_logout(
    client: AsyncClient, settings: Settings
) -> None:
    client.base_url = URL("https://test")
    settings.admin_email = "admin@example.com"
    admin = await register_and_login(client, settings.admin_email)
    old_cookie = client.cookies[DOCS_COOKIE]
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": admin["refresh_token"]}
    )
    assert refresh.status_code == 200
    attributes = refresh.headers["set-cookie"].lower()
    assert all(
        attribute in attributes for attribute in ("; httponly", "; secure", "; samesite=strict")
    )
    current_cookie = client.cookies[DOCS_COOKIE]
    assert current_cookie != old_cookie
    assert (await client.get("/openapi.json")).status_code == 200
    assert (
        await client.get("/openapi.json", headers={"Cookie": f"{DOCS_COOKIE}={old_cookie}"})
    ).status_code == 401

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": refresh.json()["refresh_token"]}
    )
    assert logout.status_code == 204 and DOCS_COOKIE not in client.cookies
    assert (await client.get("/docs")).status_code == 401
    assert (
        await client.get("/openapi.json", headers={"Cookie": f"{DOCS_COOKIE}={current_cookie}"})
    ).status_code == 401
