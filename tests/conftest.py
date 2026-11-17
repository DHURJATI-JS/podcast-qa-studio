"""Test fixtures.

Tests run in the isolated ``docker-compose.test.yml`` stack against the throwaway
``podcastqa_test`` database (in RAM) and Redis DB 15 - never the real database.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://podcastqa:test-only-password@db:5432/podcastqa_test")
os.environ.setdefault("REDIS_URL", "redis://redis:6379/15")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("EMBEDDING_ORDER", "hash")
os.environ.setdefault("TTS_ENABLED", "false")
os.environ.setdefault("GATEWAY_ENABLED", "false")
os.environ.setdefault("LOCAL_LLM_ENABLED", "false")

if not os.environ["DATABASE_URL"].rstrip("/").endswith("_test"):
    raise RuntimeError("Refusing to run tests: DATABASE_URL must point at a *_test database, never the main one.")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.ai.embeddings import Embedder  # noqa: E402
from app.ai.providers import Health, Provider, ProviderError  # noqa: E402
from app.ai.router import AIRouter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.jobs import handlers, queue  # noqa: E402
from app.jobs.emitter import RecordingEmitter  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.main import asgi_app  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

TABLES = "answers, question_revisions, question_reviews, exports, ai_jobs, questions, podcasts, password_reset_tokens, users"
PASSWORD = "Correct-Horse-Battery-42"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def database():
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.commit()

        def upgrade(sync_conn):  # noqa: ANN001
            config = Config("alembic.ini")
            config.attributes["connection"] = sync_conn
            config.attributes["configure_logger"] = False
            command.upgrade(config, "head")

        await conn.run_sync(upgrade)
    await engine.dispose()
    yield


@pytest_asyncio.fixture(scope="session")
async def app_ready(database):
    async with LifespanManager(fastapi_app):
        yield fastapi_app


@pytest_asyncio.fixture(autouse=True)
async def clean(app_ready):
    from app.db import session_scope

    async with session_scope() as db:
        await db.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await db.commit()
    await get_redis().flushdb()
    yield


@pytest_asyncio.fixture
async def client(app_ready) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


@pytest_asyncio.fixture
async def other_client(app_ready) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


# --------------------------------------------------------------------------- helpers
META_CSRF = re.compile(r'<meta name="csrf-token" content="([0-9a-f]{64})">')
FORM_TS = re.compile(r'name="form_ts" value="([^"]+)"')


async def csrf(client: httpx.AsyncClient, path: str = "/login") -> str:
    response = await client.get(path)
    match = META_CSRF.search(response.text)
    assert match, f"no CSRF meta tag on {path} (status {response.status_code})"
    return match.group(1)


async def signup(client: httpx.AsyncClient, username: str | None = None, *, email: str | None = None, name: str = "Test User") -> dict:
    username = username or f"user{uuid.uuid4().hex[:8]}"
    email = email or f"{username}@example.com"
    page = await client.get("/signup")
    token = META_CSRF.search(page.text).group(1)
    form_ts = FORM_TS.search(page.text).group(1)
    response = await client.post(
        "/signup",
        data={
            "csrf_token": token,
            "form_ts": form_ts,
            "website": "",
            "display_name": name,
            "username": username,
            "email": email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    assert response.status_code == 303, response.text[:500]
    return {"username": username, "email": email, "csrf": await csrf(client, "/dashboard")}


def headers(token: str, idem: bool = False) -> dict:
    out = {"X-CSRF-Token": token}
    if idem:
        out["Idempotency-Key"] = uuid.uuid4().hex
    return out


async def create_podcast(client: httpx.AsyncClient, token: str, *, mode: str = "host", title: str = "Climate founders", **extra) -> str:
    page = await client.get(f"/podcasts/new?mode={mode}")
    idem = re.search(r'name="idem" value="([^"]+)"', page.text).group(1)
    data = {
        "csrf_token": token,
        "mode": mode,
        "idem": idem,
        "title": title,
        "theme": extra.pop("theme", "Building climate startups that last"),
        "target_minutes": str(extra.pop("target_minutes", 30)),
        "intro_seconds": "60",
        "outro_seconds": "60",
    }
    data.update({k: str(v) for k, v in extra.items()})
    response = await client.post("/podcasts/new", data=data)
    assert response.status_code == 303, response.text[:500]
    return response.headers["location"].split("/podcasts/")[1].split("?")[0]


async def add_question(client: httpx.AsyncClient, token: str, podcast_id: str, text_: str, **extra) -> dict:
    response = await client.post(
        f"/api/podcasts/{podcast_id}/questions",
        json={"text": text_, "category": extra.get("category", "core"), "est_seconds": extra.get("est_seconds", 120)},
        headers=headers(token, idem=True),
    )
    assert response.status_code == 201, response.text
    return response.json()["question"]


# --------------------------------------------------------------------------- fake AI
class FakeProvider(Provider):
    """Scripted provider: yields the given chunks, or raises ProviderError at a chosen point."""

    def __init__(self, name: str, chunks: list[str] | None = None, *, fail_before: bool = False, fail_after: int | None = None, configured: bool = True, health: str = "online"):
        self.name = name
        self.label = f"Fake {name}"
        self.model = "fake"
        self.chunks = chunks or []
        self.fail_before = fail_before
        self.fail_after = fail_after
        self._configured = configured
        self._health = health
        self.calls: list[tuple[str, list]] = []
        self.health_interval = 20

    def configured(self) -> bool:
        return self._configured

    async def health(self) -> Health:
        return Health(self._health, "")

    async def stream(self, system, messages, *, max_tokens, temperature):  # noqa: ANN001
        self.calls.append((system, messages))
        if self.fail_before:
            raise ProviderError("connection refused")
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i >= self.fail_after:
                raise ProviderError("stream dropped")
            yield chunk


@pytest_asyncio.fixture
async def ai(app_ready, monkeypatch):
    """Install fake providers in the web app and give tests a way to run queued jobs inline."""

    class AI:
        def __init__(self) -> None:
            self.emitter = RecordingEmitter()
            self.router: AIRouter = app_ready.state.router
            self.embedder: Embedder = app_ready.state.embedder

        def use(self, *providers: Provider) -> None:
            monkeypatch.setattr(self.router, "providers", list(providers))

        @property
        def ctx(self) -> handlers.JobContext:
            return handlers.JobContext(router=self.router, embedder=self.embedder, redis=get_redis(), emitter=self.emitter)

        async def run(self, job_id: str) -> None:
            await handlers.run_job(self.ctx, job_id)

        def events(self, name: str) -> list[tuple[dict, str | None]]:
            return [(data, room) for event, data, room in self.emitter.events if event == name]

    helper = AI()
    await queue.heartbeat(get_redis(), "test-worker")
    return helper


def lines(*questions: str) -> list[str]:
    """Split a model reply into small chunks, like a real stream."""
    text_ = "\n".join(questions) + "\n"
    return [text_[i : i + 7] for i in range(0, len(text_), 7)]


@pytest.fixture
def settings():
    return get_settings()
