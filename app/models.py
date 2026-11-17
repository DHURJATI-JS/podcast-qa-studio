"""Database models (PostgreSQL + pgvector)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column

EMBED_DIM = 768

PODCAST_MODES = ("host", "guest")
QUESTION_CATEGORIES = (
    "opener",
    "background",
    "core",
    "story",
    "challenge",
    "rapid_fire",
    "audience",
    "closing",
    "follow_up",
)
JOB_KINDS = ("generate_questions", "rewrite_questions", "answer_feedback", "next_question", "review_repeats")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Fetch server-generated values (created_at/updated_at) with RETURNING on INSERT *and*
    # UPDATE, so nothing is lazy-loaded later (lazy loads are not allowed under asyncio).
    __mapper_args__ = {"eager_defaults": True}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


# clock_timestamp() is the real time of the INSERT/UPDATE; now() would be the *transaction start*,
# which can be earlier than the moment a row was actually written (wrong ordering under concurrency).
def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp(), nullable=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    username: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)  # stored lower-case
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)  # stored lower-case
    display_name: Mapped[str] = mapped_column(String(60), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    avatar_color: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0", nullable=False)
    tts_voice: Mapped[str] = mapped_column(String(40), default="mira_soft", server_default="mira_soft", nullable=False)
    tts_speed: Mapped[float] = mapped_column(Float, default=0.95, server_default="0.95", nullable=False)
    speaking_wpm: Mapped[int] = mapped_column(SmallInteger, default=150, server_default="150", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # One login per account: sha256 of the active device's session token (see security/sessions.py).
    login_key_hash: Mapped[str | None] = mapped_column(String(64))
    login_device: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    signout_reason: Mapped[str | None] = mapped_column(String(20))

    __table_args__ = (
        CheckConstraint("avatar_color BETWEEN 0 AND 7", name="avatar_color"),
        CheckConstraint("speaking_wpm BETWEEN 80 AND 260", name="wpm"),
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_ip: Mapped[str] = mapped_column(String(45), default="", server_default="", nullable=False)


class Podcast(Base):
    __tablename__ = "podcasts"

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # host = create questions, guest = prepare
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    theme: Mapped[str] = mapped_column(String(300), nullable=False)
    brief: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    audience: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    tone: Mapped[str] = mapped_column(String(60), default="conversational", server_default="conversational", nullable=False)
    language: Mapped[str] = mapped_column(String(40), default="English", server_default="English", nullable=False)
    show_name: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    host_name: Mapped[str] = mapped_column(String(80), default="", server_default="", nullable=False)
    guest_name: Mapped[str] = mapped_column(String(80), default="", server_default="", nullable=False)
    guest_bio: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    talking_points: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    target_minutes: Mapped[int] = mapped_column(SmallInteger, default=30, server_default="30", nullable=False)
    intro_seconds: Mapped[int] = mapped_column(SmallInteger, default=60, server_default="60", nullable=False)
    outro_seconds: Mapped[int] = mapped_column(SmallInteger, default=60, server_default="60", nullable=False)
    # "Mira remembers": instructions the user gave once that every AI action must respect.
    standing_instructions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint("mode IN ('host','guest')", name="mode"),
        CheckConstraint("target_minutes BETWEEN 5 AND 240", name="target"),
        CheckConstraint("intro_seconds BETWEEN 0 AND 900 AND outro_seconds BETWEEN 0 AND 900", name="bumpers"),
        Index("ix_podcasts_owner_updated", "owner_id", "updated_at"),
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = _pk()
    podcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(20), default="core", server_default="core", nullable=False)
    est_seconds: Mapped[int] = mapped_column(SmallInteger, default=120, server_default="120", nullable=False)
    source: Mapped[str] = mapped_column(String(12), default="user", server_default="user", nullable=False)
    notes: Mapped[str] = mapped_column(String(1000), default="", server_default="", nullable=False)
    flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint("est_seconds BETWEEN 10 AND 1800", name="seconds"),
        Index("ix_questions_podcast_position", "podcast_id", "position"),
        Index("ix_questions_podcast_hash", "podcast_id", "text_hash"),
        Index("ix_questions_embedding_hnsw", "embedding", postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class QuestionRevision(Base):
    __tablename__ = "question_revisions"

    id: Mapped[uuid.UUID] = _pk()
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False)
    old_text: Mapped[str] = mapped_column(String(500), nullable=False)
    old_category: Mapped[str] = mapped_column(String(20), nullable=False)
    old_est_seconds: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    instruction: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    source: Mapped[str] = mapped_column(String(12), default="user", server_default="user", nullable=False)
    created_at: Mapped[datetime] = _created()


class QuestionReview(Base):
    """The AI's verdict on whether two questions of a podcast are the same question in other words.

    One row per pair (``question_id`` < ``other_id``). It applies only while both texts are exactly
    as they were judged (``fingerprint``), so editing either question retires the verdict."""

    __tablename__ = "question_reviews"

    id: Mapped[uuid.UUID] = _pk()
    podcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), index=True, nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    other_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    same: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("question_id", "other_id"),)  # one verdict per pair; the latest wins


class Answer(Base):
    """One practice attempt by the guest for one question."""

    __tablename__ = "answers"

    id: Mapped[uuid.UUID] = _pk()
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False)
    podcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    est_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    rehearsed_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="saved", server_default="saved", nullable=False)
    score: Mapped[int | None] = mapped_column(SmallInteger)
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(String(80))
    redundant_with: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 10", name="score"),
        Index("ix_answers_embedding_hnsw", "embedding", postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class AIJob(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    podcast_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="queued", server_default="queued", nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(1000))
    provider: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="status"
        ),
        Index("ix_ai_jobs_user_status", "user_id", "status"),
    )


class Export(Base):
    """A generated PDF, stored in PostgreSQL."""

    __tablename__ = "exports"

    id: Mapped[uuid.UUID] = _pk()
    podcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()
