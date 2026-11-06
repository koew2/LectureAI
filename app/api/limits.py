"""IP limits for authentication and account limits for costly authenticated actions."""

from fastapi import Request

from app.api.deps import CurrentUser, SettingsDep
from app.services.rate_limit import get_rate_limiter


async def limit_auth(request: Request, settings: SettingsDep) -> None:
    if settings.rate_limit_enabled:
        # Uvicorn only trusts the configured proxy. Never read user-supplied XFF here.
        address = request.client.host if request.client else "unknown"
        await get_rate_limiter(request).check(
            "auth", address, settings.auth_requests_per_minute, 60
        )


async def limit_chat(request: Request, user: CurrentUser, settings: SettingsDep) -> None:
    if settings.rate_limit_enabled:
        await get_rate_limiter(request).check(
            "chat", str(user.id), settings.chat_requests_per_minute, 60
        )


async def limit_upload(request: Request, user: CurrentUser, settings: SettingsDep) -> None:
    if settings.rate_limit_enabled:
        await get_rate_limiter(request).check(
            "upload", str(user.id), settings.uploads_per_hour, 3600
        )
