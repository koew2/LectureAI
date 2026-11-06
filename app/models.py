"""Persistence models shared by the API, worker and Alembic."""

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    MetaData,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class LectureStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


json_type = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshSession(Base):
    """Only a token's random jti is stored; rotating it prevents refresh replay."""

    __tablename__ = "refresh_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AudioFile(Base):
    __tablename__ = "audio_files"
    __table_args__ = (CheckConstraint("duration >= 0", name="nonnegative_duration"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    status: Mapped[LectureStatus] = mapped_column(
        Enum(LectureStatus, native_enum=False, create_constraint=True, name="lecture_status"),
        default=LectureStatus.pending,
        server_default="pending",
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    transcription: Mapped["Transcription | None"] = relationship(
        back_populates="audio_file", cascade="all, delete-orphan", lazy="raise", uselist=False
    )
    summary: Mapped["Summary | None"] = relationship(
        back_populates="audio_file", cascade="all, delete-orphan", lazy="raise", uselist=False
    )


class Transcription(Base):
    __tablename__ = "transcriptions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(
        ForeignKey("audio_files.id", ondelete="CASCADE"), unique=True
    )
    raw_text: Mapped[str] = mapped_column(Text)
    clean_text: Mapped[str] = mapped_column(Text)
    timestamps_json: Mapped[list[dict[str, str | float]]] = mapped_column(json_type)
    audio_file: Mapped[AudioFile] = relationship(back_populates="transcription", lazy="raise")


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(
        ForeignKey("audio_files.id", ondelete="CASCADE"), unique=True
    )
    structured_markdown: Mapped[str] = mapped_column(Text)
    key_points_json: Mapped[list[str]] = mapped_column(json_type)
    audio_file: Mapped[AudioFile] = relationship(back_populates="summary", lazy="raise")
    flashcards: Mapped[list["Flashcard"]] = relationship(
        cascade="all, delete-orphan", lazy="raise", order_by="Flashcard.id"
    )


class Flashcard(Base):
    __tablename__ = "flashcards"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    summary_id: Mapped[UUID] = mapped_column(ForeignKey("summaries.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
