"""Validated request/response contracts exposed in OpenAPI."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models import LectureStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr
    username: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
        ),
    ]
    password: SecretStr = Field(min_length=10, max_length=128)
    password_confirmation: SecretStr = Field(min_length=10, max_length=128)

    @field_validator("password", "password_confirmation")
    @classmethod
    def password_is_utf8(cls, value: SecretStr) -> SecretStr:
        try:
            value.get_secret_value().encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Password must be valid Unicode") from None
        return value

    @model_validator(mode="after")
    def passwords_match(self) -> Self:
        if self.password != self.password_confirmation:
            raise ValueError("Passwords do not match")
        return self


class RegisterConfirmRequest(RegisterRequest):
    code: str = Field(pattern=r"^[0-9]{6}$", min_length=6, max_length=6)


class UserResponse(ORMModel):
    id: UUID
    email: EmailStr
    username: str | None
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    is_admin: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class FlashcardData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)


class StudyNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class LectureListItem(ORMModel):
    id: UUID
    filename: str | None
    duration: float | None
    status: LectureStatus
    created_at: datetime


class LectureResponse(LectureListItem):
    error_message: str | None
    transcription: TranscriptionResponse | None
    summary: SummaryResponse | None


class AdminProcessingItem(BaseModel):
    lecture_id: UUID
    filename: str | None
    username: str | None
    user_email: str
    successful: bool
    finished_at: datetime | None
    uploaded_at: datetime
    error_message: str | None


class AdminProcessingPage(BaseModel):
    items: list[AdminProcessingItem]
    next_before: int | None
    successful_count: int
    failed_count: int


class AdminEventItem(BaseModel):
    id: int
    created_at: datetime
    level: Literal["info", "error"]
    event: str
    username: str | None
    user_email: str | None
    lecture_id: UUID | None
    error_type: str | None
    status_code: int | None


class AdminEventPage(BaseModel):
    items: list[AdminEventItem]
    next_before: int | None


class UploadResponse(BaseModel):
    id: UUID
    status: LectureStatus


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class ChatResponse(BaseModel):
    answer: str
