"""A small, durable job queue on Redis Streams (consumer groups).

* Parallel: each worker runs up to ``WORKER_CONCURRENCY`` jobs at once, and any number of
  worker processes/containers can share the stream (``docker compose up --scale worker=3``).
* Durable: messages stay pending until acknowledged; messages abandoned by a crashed worker are
  reclaimed with ``XAUTOCLAIM`` (after the job timeout) and reported as interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)

STREAM = "pq:jobs"
GROUP = "pq-workers"
WORKERS_KEY = "pq:workers"


async def enqueue(redis: aioredis.Redis, job_id: str) -> None:
    await redis.xadd(STREAM, {"job_id": job_id}, maxlen=10_000, approximate=True)


async def ensure_group(redis: aioredis.Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def heartbeat(redis: aioredis.Redis, name: str) -> None:
    now = time.time()
    await redis.zadd(WORKERS_KEY, {name: now})
    await redis.zremrangebyscore(WORKERS_KEY, "-inf", now - 300)


async def worker_alive(redis: aioredis.Redis, within: int = 30) -> bool:
    return (await redis.zcount(WORKERS_KEY, time.time() - within, "+inf")) > 0


Handler = Callable[[str], Awaitable[None]]


class StreamWorker:
    def __init__(self, redis: aioredis.Redis, name: str, handler: Handler, *, concurrency: int, claim_idle_ms: int) -> None:
        self.redis = redis
        self.name = name
        self.handler = handler
        self.concurrency = max(1, concurrency)
        self.claim_idle_ms = claim_idle_ms
        self.tasks: set[asyncio.Task] = set()
        self._last_claim = 0.0

    def _spawn(self, message_id: str, fields: dict) -> None:
        task = asyncio.create_task(self._process(message_id, fields))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _process(self, message_id: str, fields: dict) -> None:
        try:
            await self.handler(str(fields.get("job_id", "")))
        except Exception:  # noqa: BLE001 - a failing job must never kill the worker
            log.exception("Job handler crashed for message %s", message_id)
        finally:
            try:
                await self.redis.xack(STREAM, GROUP, message_id)
                await self.redis.xdel(STREAM, message_id)
            except Exception:  # noqa: BLE001
                log.exception("Could not acknowledge message %s", message_id)

    async def _reclaim(self) -> None:
        free = self.concurrency - len(self.tasks)
        if free <= 0:
            return
        result = await self.redis.xautoclaim(
            STREAM, GROUP, self.name, min_idle_time=self.claim_idle_ms, start_id="0-0", count=free
        )
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        for message_id, fields in entries:
            if fields:
                log.warning("Reclaimed abandoned job message %s", message_id)
                self._spawn(message_id, fields)
            else:
                await self.redis.xack(STREAM, GROUP, message_id)

    async def run(self, stop: asyncio.Event) -> None:
        await ensure_group(self.redis)
        log.info("Worker %s consuming %s (concurrency %d)", self.name, STREAM, self.concurrency)
        while not stop.is_set():
            try:
                if time.monotonic() - self._last_claim > 30:
                    self._last_claim = time.monotonic()
                    await self._reclaim()
                free = self.concurrency - len(self.tasks)
                if free <= 0:
                    await asyncio.wait(self.tasks, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                    continue
                response = await self.redis.xreadgroup(GROUP, self.name, {STREAM: ">"}, count=free, block=2000)
                for _stream, entries in response or []:
                    for message_id, fields in entries:
                        self._spawn(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep consuming after transient Redis errors
                log.exception("Worker loop error; retrying in 2s")
                await asyncio.sleep(2)
        if self.tasks:
            log.info("Waiting for %d running jobs to finish", len(self.tasks))
            _, pending = await asyncio.wait(self.tasks, timeout=30)
            for task in pending:
                task.cancel()  # run_job marks them failed + releases locks in its finally block
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
