"""Async SQLAlchemy engine/session management (asyncpg driver)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class _DBState:
    engine: AsyncEngine | None = None
    sessionmaker: async_sessionmaker[AsyncSession] | None = None


db_state = _DBState()


def init_db(url: str, *, pool_size: int = 10, max_overflow: int = 10) -> AsyncEngine:
    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    db_state.engine = engine
    db_state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine


async def close_db() -> None:
    if db_state.engine is not None:
        await db_state.engine.dispose()
    db_state.engine = None
    db_state.sessionmaker = None


def session_scope() -> AsyncSession:
    if db_state.sessionmaker is None:
        raise RuntimeError("Database is not initialised")
    return db_state.sessionmaker()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with session_scope() as session:
        yield session
