"""PDF exports: rendered in a separate process pool (parallel, never blocks the event loop)
and stored in PostgreSQL (``exports.content``)."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Export, Podcast
from app.security.sanitize import slugify
from app.services.duration import plan_summary
from app.services.podcasts import latest_answers
from app.services.questions import podcast_questions

DISCLAIMER = (
    "This document was prepared with AI assistance and is provided \"as is\", without warranty of any kind. "
    "AI suggestions can be inaccurate, incomplete or inappropriate - review every question and answer before "
    "you use it. Nothing here is legal, medical, financial or other professional advice. You are responsible "
    "for your content, for your guests' consent and for complying with the law. The authors and operators of "
    "Podcast Q&A Studio accept no liability for any loss or damage arising from its use."
)

_pool: ProcessPoolExecutor | None = None


def get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=max(1, get_settings().pdf_workers), mp_context=multiprocessing.get_context("spawn")
        )
    return _pool


def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def render_in_pool(payload: dict) -> bytes:
    from app.services.pdf import render_pdf

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(get_pool(), render_pdf, payload), timeout=120)


async def build_payload(db: AsyncSession, podcast: Podcast) -> dict:
    questions = await podcast_questions(db, podcast.id)
    answers = await latest_answers(db, podcast.id) if podcast.mode == "guest" else {}
    items = []
    for number, question in enumerate(questions, start=1):
        answer = answers.get(str(question.id))
        answer_payload = None
        if answer:
            feedback = answer.get("feedback") or {}
            answer_payload = {
                "text": answer["text"],
                "score": answer.get("score"),
                "word_count": answer.get("word_count"),
                "est_seconds": answer.get("est_seconds"),
                "worked": feedback.get("worked") or [],
                "improve": feedback.get("improve") or [],
                "stronger_answer": feedback.get("stronger_answer") or "",
                "timing": feedback.get("timing") or "",
            }
        items.append(
            {
                "number": number,
                "text": question.text,
                "category": question.category,
                "est_seconds": question.est_seconds,
                "answer": answer_payload,
            }
        )
    plan = plan_summary(podcast.target_minutes, podcast.intro_seconds, podcast.outro_seconds, [q.est_seconds for q in questions])
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        "podcast": {
            "mode": podcast.mode,
            "title": podcast.title,
            "theme": podcast.theme,
            "brief": podcast.brief,
            "audience": podcast.audience,
            "tone": podcast.tone,
            "language": podcast.language,
            "show_name": podcast.show_name,
            "host_name": podcast.host_name,
            "guest_name": podcast.guest_name,
            "guest_bio": podcast.guest_bio,
            "talking_points": podcast.talking_points,
            "standing_instructions": list(podcast.standing_instructions or []),
        },
        "plan": plan,
        "questions": items,
        "disclaimer": DISCLAIMER,
        "footer": f"{settings.app_name} - AI-assisted; verify before use. Provided as is, without warranty.",
    }


async def create_export(db: AsyncSession, user_id: uuid.UUID, podcast: Podcast) -> Export:
    payload = await build_payload(db, podcast)
    content = await render_in_pool(payload)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    export = Export(
        podcast_id=podcast.id,
        user_id=user_id,
        filename=f"{slugify(podcast.title)}-{stamp}.pdf",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    db.add(export)
    await db.flush()
    keep = get_settings().max_exports_per_podcast
    stale = (
        await db.execute(
            select(Export.id).where(Export.podcast_id == podcast.id).order_by(Export.created_at.desc(), Export.id.desc()).offset(keep)
        )
    ).scalars().all()
    stale = [s for s in stale if s != export.id]
    if stale:
        await db.execute(delete(Export).where(Export.id.in_(stale)))
    return export
