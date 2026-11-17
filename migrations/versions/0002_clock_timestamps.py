"""Use clock_timestamp() for created_at/updated_at defaults.

now() returns the start time of the transaction, which can be earlier than the moment a row is
written (e.g. when a request waited for a row lock). clock_timestamp() records the real time, so
"latest answer", undo order and pruning stay correct under concurrency.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE password_reset_tokens ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE podcasts ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE podcasts ALTER COLUMN updated_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE questions ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE questions ALTER COLUMN updated_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE question_revisions ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE answers ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE answers ALTER COLUMN updated_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE ai_jobs ALTER COLUMN created_at SET DEFAULT clock_timestamp()")
    op.execute("ALTER TABLE exports ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE password_reset_tokens ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE podcasts ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE podcasts ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE questions ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE questions ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE question_revisions ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE answers ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE answers ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE ai_jobs ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE exports ALTER COLUMN created_at SET DEFAULT now()")
