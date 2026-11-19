"""ASGI application. Run with `uvicorn app.main:app`."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api import admin, auth, docs, lectures
from app.core.config import get_settings
from app.db import create_session_factory
from app.services.event_log import record_event
from app.services.rate_limit import RateLimiter, RedisCounterStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    factory = create_session_factory(settings)
    app.state.session_factory = factory
    redis_client: Redis | None = None
    if settings.rate_limit_enabled:
        redis_client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        app.state.rate_limiter = RateLimiter(RedisCounterStore(redis_client))
    try:
        yield
    finally:
        app.state.rate_limiter = None
        if redis_client is not None:
            await redis_client.aclose()
        engine = factory.kw["bind"]
        if isinstance(engine, AsyncEngine):
            await engine.dispose()


app = FastAPI(
    title="EduFlow API",
    version="0.1.0",
    description="Audio lectures → transcription, structured notes, flashcards and AI tutor.",
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(lectures.router, prefix="/api/v1")
app.include_router(docs.router)
web_dir = Path(__file__).with_name("web")
app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(web_dir / "index.html")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(web_dir / "admin.html")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Do not echo invalid passwords, JWTs or other request data into error bodies."""
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(Exception)
async def application_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith("/api/v1/"):
        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None:
            try:
                async with factory() as session:
                    await record_event(
                        session,
                        "api_error",
                        user_id=getattr(request.state, "event_user_id", None),
                        error=exc,
                        status_code=500,
                    )
            except (SQLAlchemyError, OSError) as log_error:
                logger.warning("Could not write API error event (%s)", type(log_error).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Liveness: process is accepting requests."""
    return {"status": "ok"}


@app.get("/ready", tags=["Health"])
async def ready(request: Request) -> dict[str, str]:
    """Readiness: the API can connect to PostgreSQL."""
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        raise HTTPException(status_code=503, detail="Database is unavailable") from None
    return {"status": "ready"}
