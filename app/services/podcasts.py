"""Podcast ownership checks and read models.

Every lookup is scoped to the signed-in owner; anything else is a 404 (never a 403), so other
people's podcast ids cannot even be confirmed to exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIJob, Answer, Export, Podcast, Question
from app.services.duration import plan_summary
from app.services.questions import podcast_questions, serialize_answer, serialize_question


class NotFound(Exception):
    pass


def parse_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise NotFound() from exc


async def get_owned_podcast(db: AsyncSession, owner_id: uuid.UUID, podcast_id: str | uuid.UUID, *, for_update: bool = False) -> Podcast:
    stmt = select(Podcast).where(Podcast.id == parse_uuid(podcast_id), Podcast.owner_id == owner_id)
    if for_update:
        stmt = stmt.with_for_update()
    podcast = (await db.execute(stmt)).scalar_one_or_none()
    if podcast is None:
        raise NotFound()
    return podcast


async def get_owned_question(db: AsyncSession, owner_id: uuid.UUID, question_id: str | uuid.UUID, *, for_update: bool = False) -> tuple[Question, Podcast]:
    stmt = (
        select(Question, Podcast)
        .join(Podcast, Podcast.id == Question.podcast_id)
        .where(Question.id == parse_uuid(question_id), Podcast.owner_id == owner_id)
    )
    if for_update:
        stmt = stmt.with_for_update(of=Question)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise NotFound()
    return row[0], row[1]


def serialize_podcast(p: Podcast) -> dict:
    return {
        "id": str(p.id),
        "mode": p.mode,
        "title": p.title,
        "theme": p.theme,
        "brief": p.brief,
        "audience": p.audience,
        "tone": p.tone,
        "language": p.language,
        "show_name": p.show_name,
        "host_name": p.host_name,
        "guest_name": p.guest_name,
        "guest_bio": p.guest_bio,
        "talking_points": p.talking_points,
        "target_minutes": p.target_minutes,
        "intro_seconds": p.intro_seconds,
        "outro_seconds": p.outro_seconds,
        "standing_instructions": list(p.standing_instructions or []),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def podcast_plan(podcast: Podcast, questions: list[Question]) -> dict:
    return plan_summary(podcast.target_minutes, podcast.intro_seconds, podcast.outro_seconds, [q.est_seconds for q in questions])


async def latest_answers(db: AsyncSession, podcast_id: uuid.UUID) -> dict[str, dict]:
    rows = (
        await db.execute(
            select(Answer)
            .where(Answer.podcast_id == podcast_id)
            .distinct(Answer.question_id)
            .order_by(Answer.question_id, Answer.created_at.desc())
        )
    ).scalars()
    counts = dict(
        (await db.execute(select(Answer.question_id, func.count(Answer.id)).where(Answer.podcast_id == podcast_id).group_by(Answer.question_id))).all()
    )
    out = {}
    for answer in rows:
        data = serialize_answer(answer)
        data["attempts"] = int(counts.get(answer.question_id, 1))
        out[str(answer.question_id)] = data
    return out


async def recent_exports(db: AsyncSession, podcast_id: uuid.UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(Export.id, Export.filename, Export.size_bytes, Export.created_at)
            .where(Export.podcast_id == podcast_id)
            .order_by(Export.created_at.desc())
            .limit(10)
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "filename": r.filename,
            "size_bytes": r.size_bytes,
            "created_at": r.created_at.isoformat(),
            "url": f"/podcasts/{podcast_id}/exports/{r.id}/download",
        }
        for r in rows
    ]


async def active_jobs(db: AsyncSession, user_id: uuid.UUID, podcast_id: uuid.UUID) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    rows = (
        await db.execute(
            select(AIJob)
            .where(
                AIJob.user_id == user_id,
                AIJob.podcast_id == podcast_id,
                AIJob.status.in_(("queued", "running")),
                AIJob.created_at > since,
            )
            .order_by(AIJob.created_at)
        )
    ).scalars()
    return [serialize_job(job) for job in rows]


def serialize_job(job: AIJob) -> dict:
    params = {k: v for k, v in (job.params or {}).items() if not k.startswith("_")}
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "provider": job.provider,
        "error": job.error,
        "params": params,
        "result": job.result,
        "podcast_id": str(job.podcast_id) if job.podcast_id else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def workspace_state(db: AsyncSession, user_id: uuid.UUID, podcast: Podcast) -> dict:
    questions = await podcast_questions(db, podcast.id)
    return {
        "podcast": serialize_podcast(podcast),
        "questions": [serialize_question(q) for q in questions],
        "plan": podcast_plan(podcast, questions),
        "answers": await latest_answers(db, podcast.id),
        "exports": await recent_exports(db, podcast.id),
        "jobs": await active_jobs(db, user_id, podcast.id),
    }
