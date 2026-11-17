"""Starting and cancelling AI jobs (web side).

All AI spam / double-submit protection is applied here, before anything reaches a model:
1. idempotency key (same click/request processed once),
2. AI availability (worker alive + at least one provider not known to be down),
3. per-user hourly and daily AI quotas,
4. a per-resource lock (one AI task per podcast question list / per answered question),
5. a per-user cap on concurrent AI tasks (row-locked, so it holds under concurrency).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import AIRouter
from app.config import get_settings
from app.jobs import queue
from app.jobs.emitter import Emitter, user_room
from app.models import AIJob, User
from app.security import ratelimit
from app.services.podcasts import NotFound, parse_uuid


class AIUnavailable(Exception):
    pass


class JobConflict(Exception):
    pass


async def ai_ready(redis: aioredis.Redis, router: AIRouter) -> tuple[bool, str, list[dict]]:
    statuses = await router.statuses()
    if not await queue.worker_alive(redis):
        return (
            False,
            "The AI worker isn't running, so AI features are paused. Start the app with the installer "
            "(or run `docker compose up -d worker`) and try again.",
            statuses,
        )
    if not any(s["status"] in ("online", "unknown") for s in statuses):
        details = "; ".join(f"{s['label']}: {s['status_label'].lower()}" for s in statuses)
        return (
            False,
            f"No AI is available right now ({details}). Check that the AI gateway is running, wait for the "
            "local Gemma model to finish downloading, or add a Claude / ChatGPT API key.",
            statuses,
        )
    return True, "", statuses


def lock_names_for(kind: str, podcast_id: uuid.UUID, params: dict) -> list[str]:
    if kind in ("generate_questions", "rewrite_questions", "next_question", "review_repeats"):
        return [f"ai:q:{podcast_id}"]
    if kind == "answer_feedback":
        return [f"ai:fb:{params['question_id']}"]
    return []


async def release_job_locks(redis: aioredis.Redis, params: dict | None) -> None:
    for item in (params or {}).get("_locks", []) or []:
        try:
            name, token = item
        except (TypeError, ValueError):
            continue
        await ratelimit.release_lock(redis, str(name), str(token))


async def start_job(
    *,
    db: AsyncSession,
    redis: aioredis.Redis,
    router: AIRouter,
    user_id: uuid.UUID,
    podcast_id: uuid.UUID,
    kind: str,
    params: dict,
    idempotency_key: str | None,
) -> AIJob:
    settings = get_settings()
    if not await ratelimit.claim_once(redis, f"job:{user_id}", idempotency_key):
        raise JobConflict("That request was already submitted - Mira is already on it.")
    ok, why, _ = await ai_ready(redis, router)
    if not ok:
        raise AIUnavailable(why)
    await ratelimit.enforce(
        redis, "ai_hour", str(user_id), settings.rate("ai_hour"),
        "You've made a lot of AI requests in the last hour. Please take a short break and try again.",
    )
    await ratelimit.enforce(
        redis, "ai_day", str(user_id), settings.rate("ai_day"), "You've reached today's AI limit. It resets within 24 hours."
    )
    held: list[list[str]] = []
    try:
        for name in lock_names_for(kind, podcast_id, params):
            token = await ratelimit.acquire_lock(redis, name, int(settings.ai_job_timeout) + 60)
            if token is None:
                raise JobConflict("Mira is already working on this. Wait for it to finish, or cancel it first.")
            held.append([name, token])
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        since = datetime.now(timezone.utc) - timedelta(seconds=settings.ai_job_timeout + 120)
        active = await db.scalar(
            select(func.count(AIJob.id)).where(
                AIJob.user_id == user_id, AIJob.status.in_(("queued", "running")), AIJob.created_at > since
            )
        )
        if (active or 0) >= settings.max_ai_jobs_per_user:
            raise JobConflict(
                f"You already have {active} AI tasks running (limit {settings.max_ai_jobs_per_user}). Wait for one to finish."
            )
        job = AIJob(user_id=user_id, podcast_id=podcast_id, kind=kind, status="queued", params={**params, "_locks": held})
        db.add(job)
        await db.commit()
    except BaseException:
        await db.rollback()
        for name, token in held:
            await ratelimit.release_lock(redis, name, token)
        raise
    try:
        await queue.enqueue(redis, str(job.id))
    except Exception as exc:  # noqa: BLE001
        await db.execute(update(AIJob).where(AIJob.id == job.id).values(status="failed", error="Could not queue the task.", finished_at=func.now()))
        await db.commit()
        await release_job_locks(redis, job.params)
        raise AIUnavailable("Could not queue the AI task. Please try again.") from exc
    return job


async def cancel_job(*, db: AsyncSession, redis: aioredis.Redis, emitter: Emitter, user_id: uuid.UUID, job_id: str) -> str:
    job = (
        await db.execute(select(AIJob).where(AIJob.id == parse_uuid(job_id), AIJob.user_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        raise NotFound()
    if job.status == "queued":
        changed = (
            await db.execute(
                update(AIJob)
                .where(AIJob.id == job.id, AIJob.status == "queued")
                .values(status="cancelled", error="Cancelled.", finished_at=func.now())
                .returning(AIJob.id)
                # Don't let SQLAlchemy "evaluate" the WHERE against the stale in-memory object.
                .execution_options(synchronize_session=False)
            )
        ).first()
        await db.commit()
        if changed:
            await release_job_locks(redis, job.params)
            await emitter.emit(
                "job:error",
                {"job_id": str(job.id), "podcast_id": str(job.podcast_id), "kind": job.kind, "status": "cancelled", "message": "Cancelled."},
                room=user_room(user_id),
            )
            return "cancelled"
    # Re-read the real status: a worker may have picked the job up after our first SELECT.
    status = await db.scalar(select(AIJob.status).where(AIJob.id == job.id))
    if status in ("queued", "running"):
        await redis.set(f"pq:job:{job.id}:cancel", "1", ex=3600)
        return "cancelling"
    return status or job.status
