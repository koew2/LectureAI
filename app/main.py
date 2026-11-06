"""ASGI application. Run with `uvicorn app.main:app`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api import auth, lectures
from app.core.config import get_settings
from app.db import create_session_factory
from app.services.rate_limit import RateLimiter, RedisCounterStore


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
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(lectures.router, prefix="/api/v1")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Do not echo invalid passwords, JWTs or other request data into error bodies."""
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


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
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database is unavailable") from None
    return {"status": "ready"}
