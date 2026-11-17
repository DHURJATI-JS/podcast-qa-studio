"""AI repeat check: remember the AI's verdict per pair of questions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("podcast_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("other_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("same", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.ForeignKeyConstraint(["podcast_id"], ["podcasts.id"], name=op.f("fk_question_reviews_podcast_id_podcasts"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], name=op.f("fk_question_reviews_question_id_questions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["other_id"], ["questions.id"], name=op.f("fk_question_reviews_other_id_questions"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_reviews")),
        sa.UniqueConstraint("question_id", "other_id", name=op.f("uq_question_reviews_question_id")),
    )
    op.create_index(op.f("ix_question_reviews_podcast_id"), "question_reviews", ["podcast_id"], unique=False)
    op.create_index(op.f("ix_question_reviews_other_id"), "question_reviews", ["other_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_question_reviews_other_id"), table_name="question_reviews")
    op.drop_index(op.f("ix_question_reviews_podcast_id"), table_name="question_reviews")
    op.drop_table("question_reviews")
