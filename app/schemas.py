"""Validated request/response contracts exposed in OpenAPI."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from app.models import LectureStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=10, max_length=128)


class UserResponse(ORMModel):
    id: UUID
    email: EmailStr
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class FlashcardData(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)


class StudyNotes(BaseModel):
    structured_markdown: str = Field(min_length=1, max_length=100000)
    key_points_json: list[str] = Field(min_length=1, max_length=100)
    flashcards: list[FlashcardData] = Field(min_length=1, max_length=50)


class FlashcardResponse(ORMModel):
    id: UUID
    question: str
    answer: str


class TranscriptionResponse(ORMModel):
    raw_text: str
    clean_text: str
    timestamps_json: list[dict[str, str | float]]


class SummaryResponse(ORMModel):
    structured_markdown: str
    key_points_json: list[str]
    flashcards: list[FlashcardResponse]


class LectureResponse(ORMModel):
    id: UUID
    duration: float | None
    status: LectureStatus
    error_message: str | None
    created_at: datetime
    transcription: TranscriptionResponse | None
    summary: SummaryResponse | None


class UploadResponse(BaseModel):
    id: UUID
    status: LectureStatus


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class ChatResponse(BaseModel):
    answer: str
