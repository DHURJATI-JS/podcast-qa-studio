"""Background worker: ``python -m app.worker``.

Runs, in parallel:
* the AI job consumer (Redis Streams, ``WORKER_CONCURRENCY`` jobs at once),
* AI provider + Kokoro health checks (leader-elected, broadcast as ``ai:status``),
* maintenance: re-embedding stale vectors, expiring tokens, failing stuck jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, or_, select, update

from app.ai import registry
from app.ai.embeddings import HASH_MODEL, Embedder
from app.ai.router import AIRouter
from app.config import get_settings
from app.db import close_db, init_db, session_scope
from app.jobs import handlers, queue
from app.jobs.emitter import RedisEmitter
from app.jobs.service import release_job_locks
from app.logging_setup import setup_logging
from app.models import AIJob, Answer, PasswordResetToken, Question
from app.redis_client import close_redis, init_redis
from app.security import ratelimit
from app.services.tts import KokoroClient

log = logging.getLogger("app.worker")


async def _leader(redis, name: str, ttl: int) -> bool:  # noqa: ANN001
    return bool(await redis.set(f"pq:leader:{name}", os.getpid(), nx=True, ex=ttl))


async def heartbeat_loop(redis, name: str, stop: asyncio.Event) -> None:  # noqa: ANN001
    while not stop.is_set():
        try:
            await queue.heartbeat(redis, name)
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat failed")
        await _sleep(stop, 10)


async def health_loop(router: AIRouter, kokoro: KokoroClient, emitter: RedisEmitter, redis, stop: asyncio.Event) -> None:  # noqa: ANN001
    previous = None
    while not stop.is_set():
        try:
            if await _leader(redis, "health", 8):
                statuses = await router.check_all()
                voice_ok = await kokoro.health()
                await redis.set(
                    "pq:voice:health", json.dumps({"status": "online" if voice_ok else "offline", "checked_at": time.time()}), ex=300
                )
                summary = AIRouter.summarize(statuses)
                summary["worker_alive"] = True
                summary["voice"] = {"status": "online" if voice_ok else "offline"}
                fingerprint = json.dumps([(s["name"], s["status"]) for s in statuses] + [voice_ok])
                if fingerprint != previous:
                    previous = fingerprint
                    await emitter.emit("ai:status", summary)  # broadcast: provider status is not user data
        except Exception:  # noqa: BLE001
            log.exception("Health check failed")
        await _sleep(stop, 5)


async def maintenance_loop(embedder: Embedder, redis, stop: asyncio.Event) -> None:  # noqa: ANN001
    settings = get_settings()
    await _sleep(stop, 20)
    while not stop.is_set():
        try:
            if await _leader(redis, "maintenance", 240):
                await _maintenance(embedder, redis, settings)
        except Exception:  # noqa: BLE001
            log.exception("Maintenance failed")
        await _sleep(stop, 300)


async def _maintenance(embedder: Embedder, redis, settings) -> None:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.expires_at < now - timedelta(days=1)))
        await db.execute(delete(AIJob).where(AIJob.created_at < now - timedelta(days=30)))
        stuck_before = now - timedelta(seconds=settings.ai_job_timeout + 300)
        stuck = (
            await db.execute(
                update(AIJob)
                .where(AIJob.status.in_(("queued", "running")), AIJob.created_at < stuck_before)
                .values(status="failed", error=handlers.INTERRUPTED, finished_at=now)
                .returning(AIJob.params)
            )
        ).scalars().all()
        await db.execute(
            update(Answer).where(Answer.status.in_(("queued", "reviewing")), Answer.updated_at < stuck_before).values(status="failed")
        )
        await db.commit()
    for params in stuck:
        await release_job_locks(redis, params)
    # Re-embed vectors made by a fallback model once a better model is available (parallel batches).
    model = await embedder.pick_model()
    if model == HASH_MODEL:
        return
    for table in (Question, Answer):
        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(table).where(or_(table.embedding_model.is_(None), table.embedding_model != model)).limit(64)
                )
            ).scalars().all()
            if not rows:
                continue
            batches = [rows[i : i + 16] for i in range(0, len(rows), 16)]
            results = await asyncio.gather(
                *(embedder.embed([r.text for r in batch], model) for batch in batches), return_exceptions=True
            )
            for batch, vectors in zip(batches, results):
                if isinstance(vectors, BaseException):
                    continue
                for row, vector in zip(batch, vectors):
                    row.embedding = vector
                    row.embedding_model = model
            await db.commit()
            log.info("Re-embedded %d %s rows with %s", len(rows), table.__tablename__, model)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    init_db(settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
    redis = init_redis(settings.redis_url)
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20), headers={"User-Agent": "PodcastQAStudio/1.0"})
    router = AIRouter(registry.build_providers(settings, http), redis, temperature=settings.ai_temperature, cooldown=settings.ai_circuit_cooldown)
    embedder = Embedder(settings, http)
    emitter = RedisEmitter(settings.redis_url)
    ctx = handlers.JobContext(router=router, embedder=embedder, redis=redis, emitter=emitter)
    name = f"{socket.gethostname()}-{os.getpid()}"
    worker = queue.StreamWorker(
        redis,
        name,
        lambda job_id: handlers.run_job(ctx, job_id),
        concurrency=settings.worker_concurrency,
        claim_idle_ms=int((settings.ai_job_timeout + 120) * 1000),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows without Docker
            pass
    log.info("Worker %s starting; AI order: %s", name, ", ".join(settings.provider_order))
    try:
        await asyncio.gather(
            worker.run(stop),
            heartbeat_loop(redis, name, stop),
            health_loop(router, KokoroClient(settings, http), emitter, redis, stop),
            maintenance_loop(embedder, redis, stop),
        )
    finally:
        await redis.zrem(queue.WORKERS_KEY, name)
        await http.aclose()
        await close_redis()
        await close_db()
        log.info("Worker %s stopped", name)


if __name__ == "__main__":
    asyncio.run(main())
