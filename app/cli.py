"""Operational commands.

    python -m app.cli serve              # run migrations, then start the web server
    python -m app.cli migrate            # run database migrations only
    python -m app.cli reset-link EMAIL   # print a one-time password-reset link (server operators)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone


def migrate(retries: int = 30) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic.ini"))
    for attempt in range(1, retries + 1):
        try:
            command.upgrade(config, "head")
            return
        except Exception as exc:  # noqa: BLE001 - database may still be starting
            if attempt == retries:
                raise
            print(f"Database not ready ({type(exc).__name__}); retrying {attempt}/{retries}...", flush=True)
            time.sleep(2)


def serve() -> None:
    migrate()
    from app.config import get_settings

    workers = os.environ.get("WEB_CONCURRENCY", "2")
    args = [
        "uvicorn", "app.main:asgi_app",
        "--host", "0.0.0.0", "--port", os.environ.get("PORT", "8000"),
        "--workers", workers,
        "--proxy-headers", "--forwarded-allow-ips", os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        "--no-server-header", "--timeout-graceful-shutdown", "20", "--timeout-keep-alive", "75", "--no-access-log",
        "--log-level", get_settings().log_level.lower(),
    ]
    os.execvp(args[0], args)


async def _reset_link(email: str) -> None:
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.models import PasswordResetToken, User

    settings = get_settings()
    init_db(settings.database_url, pool_size=1, max_overflow=0)
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
        if user is None:
            print("No account with that email.")
            return
        token = secrets.token_urlsafe(32)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_minutes),
                request_ip="cli",
            )
        )
        await db.commit()
    print(f"{settings.base_url.rstrip('/')}/reset-password?token={token}")


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "serve":
        serve()
    elif command == "migrate":
        migrate()
    elif command == "reset-link" and len(argv) == 3:
        asyncio.run(_reset_link(argv[2]))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)
