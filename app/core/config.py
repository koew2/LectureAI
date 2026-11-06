"""Validated configuration; credentials are supplied by the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: SecretStr
    jwt_issuer: str = "eduflow"
    jwt_audience: str = "eduflow-api"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_days: int = Field(default=7, ge=1, le=90)
    upload_dir: Path = Path("uploads")
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    max_audio_seconds: int = Field(default=14400, ge=1)
    rate_limit_enabled: bool = False
    auth_requests_per_minute: int = Field(default=10, ge=1, le=10000)
    chat_requests_per_minute: int = Field(default=20, ge=1, le=10000)
    uploads_per_hour: int = Field(default=10, ge=1, le=10000)
    ai_provider: Literal["openai", "deepseek"] = "openai"
    ai_model: str = ""
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    ai_timeout_seconds: float = Field(default=120, gt=0)
    ai_chunk_chars: int = Field(default=12000, ge=1000, le=50000)
    ai_max_chunks: int = Field(default=80, ge=1, le=500)
    ai_max_output_tokens: int = Field(default=6000, ge=256, le=32000)
    whisper_model: str = "small"
    whisper_device: Literal["cpu", "cuda", "auto"] = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = ""

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("Use postgresql+asyncpg:// (sqlite+aiosqlite:// only for tests)")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
