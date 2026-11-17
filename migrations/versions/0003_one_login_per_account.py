"""One login per account: store the active device's key on the user.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("login_key_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("login_device", sa.String(length=120), server_default="", nullable=False))
    op.add_column("users", sa.Column("signout_reason", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "signout_reason")
    op.drop_column("users", "login_device")
    op.drop_column("users", "login_key_hash")
