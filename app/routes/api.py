"""JSON API used by the workspace (fetch + CSRF header + Idempotency-Key)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.ai.embeddings import HASH_MODEL
from app.ai.router import AIRouter
from app.config import get_settings
from app.db import get_db
from app.jobs import queue
from app.jobs.service import cancel_job, start_job
from app.models import QUESTION_CATEGORIES, AIJob, Answer, Podcast, Question, QuestionRevision
from app.redis_client import get_redis
from app.routes.pages import clean_instructions
from app.security import ratelimit
from app.security.sanitize import clean_text, normalize_for_compare, spam_reason, text_hash, word_count
from app.security.sessions import CurrentUser, require_user
from app.services.duration import speaking_seconds
from app.services.exports import create_export
from app.services.podcasts import (
    NotFound,
    get_owned_podcast,
    get_owned_question,
    parse_uuid,
    podcast_plan,
    serialize_job,
    serialize_podcast,
    workspace_state,
)
from app.services.questions import (
    insert_question,
    load_reviews,
    podcast_questions,
    prune_answers,
    recheck_podcast,
    refresh_question_flags,
    review_candidates,
    save_revision,
    serialize_answer,
    serialize_question,
)
from app.services.tts import TTSUnavailable, resolve_voice, voice_options

router = APIRouter(prefix="/api")


class DuplicateSubmit(Exception):
    pass


# --------------------------------------------------------------------------- request bodies
class _Category(BaseModel):
    @field_validator("category", check_fields=False)
    @classmethod
    def _valid_category(cls, value: str | None) -> str | None:
        if value is not None and value not in QUESTION_CATEGORIES:
            raise ValueError("Unknown category")
        return value


class QuestionCreate(_Category):
    text: str = Field(min_length=3, max_length=600)
    category: str = "core"
    est_seconds: int = Field(120, ge=10, le=1800)
    notes: str = Field("", max_length=1000)


class QuestionUpdate(_Category):
    text: str | None = Field(None, min_length=3, max_length=600)
    category: str | None = None
    est_seconds: int | None = Field(None, ge=10, le=1800)
    notes: str | None = Field(None, max_length=1000)


class PodcastPatch(BaseModel):
    target_minutes: int | None = Field(None, ge=5, le=240)
    intro_seconds: int | None = Field(None, ge=0, le=900)
    outro_seconds: int | None = Field(None, ge=0, le=900)
    standing_instructions: list[str] | None = Field(None, max_length=50)


class ReorderBody(BaseModel):
    ids: list[uuid.UUID] = Field(max_length=500)


class GenerateBody(BaseModel):
    count: int = Field(5, ge=1, le=20)
    focus: str = Field("", max_length=400)


class RewriteBody(BaseModel):
    question_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    instruction: str = Field(min_length=2, max_length=400)
    remember: bool = False


class NextBody(BaseModel):
    answer_id: uuid.UUID | None = None


class AnswerBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    rehearsed_seconds: int | None = Field(None, ge=1, le=3600)
    feedback: bool = True


class TTSBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(None, max_length=40)
    speed: float | None = Field(None, ge=0.7, le=1.3)


# --------------------------------------------------------------------------- guards
async def write_guard(request: Request, user: CurrentUser = Depends(require_user)) -> CurrentUser:
    await ratelimit.enforce(get_redis(), "api_write", str(user.id), get_settings().rate("api_write"))
    return user


async def _once(request: Request, user: CurrentUser, scope: str) -> None:
    """Double-submit protection for JSON writes (client sends an Idempotency-Key per action)."""
    if not await ratelimit.claim_once(get_redis(), f"{scope}:{user.id}", request.headers.get("idempotency-key")):
        raise DuplicateSubmit()


def _router(request: Request) -> AIRouter:
    return request.app.state.router


# --------------------------------------------------------------------------- identity & status
@router.get("/me")
async def me(user: CurrentUser = Depends(require_user)):
    """Fetch the signed-in user's name by the user id held in the Redis session (Google-style chip)."""
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "first_name": user.first_name,
        "email": user.email,
        "initial": user.initial,
        "avatar_color": user.avatar_color,
        "greeting": f"Hi, {user.first_name}!",
    }


async def voice_status() -> dict:
    raw = await get_redis().get("pq:voice:health")
    if raw:
        try:
            data = json.loads(raw)
            if time.time() - float(data.get("checked_at", 0)) < 120:
                return {"status": data.get("status", "unknown")}
        except ValueError:
            pass
    return {"status": "unknown"}


@router.get("/ai/status")
async def ai_status(request: Request, user: CurrentUser = Depends(require_user)):
    statuses = await _router(request).statuses()
    summary = AIRouter.summarize(statuses)
    worker = await queue.worker_alive(get_redis())
    summary["worker_alive"] = worker
    summary["any_available"] = summary["any_available"] and worker
    summary["voice"] = await voice_status()
    return summary


# --------------------------------------------------------------------------- podcasts & questions
@router.get("/podcasts/{podcast_id}")
async def get_podcast(podcast_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    return await workspace_state(db, user.id, podcast)


@router.patch("/podcasts/{podcast_id}")
async def patch_podcast(podcast_id: str, body: PodcastPatch, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    podcast = await get_owned_podcast(db, user.id, podcast_id, for_update=True)
    for name in ("target_minutes", "intro_seconds", "outro_seconds"):
        value = getattr(body, name)
        if value is not None:
            setattr(podcast, name, value)
    if body.standing_instructions is not None:
        cleaned = clean_instructions(body.standing_instructions, cap=len(body.standing_instructions))
        if len(cleaned) > settings.max_instructions_per_podcast:
            raise HTTPException(422, f"Mira can remember up to {settings.max_instructions_per_podcast} instructions per podcast.")
        podcast.standing_instructions = cleaned
    await db.commit()
    questions = await podcast_questions(db, podcast.id)
    return {"podcast": serialize_podcast(podcast), "plan": podcast_plan(podcast, questions)}


@router.post("/podcasts/{podcast_id}/questions", status_code=201)
async def add_question(
    request: Request, podcast_id: str, body: QuestionCreate, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)
):
    settings = get_settings()
    await _once(request, user, "addq")
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    question_text = clean_text(body.text, max_len=500)
    if word_count(question_text) < 2:
        raise HTTPException(422, "Write a question with at least two words.")
    question = await insert_question(
        db,
        podcast_id=podcast.id,
        cap=settings.max_questions_per_podcast,
        text=question_text,
        category=body.category,
        est_seconds=body.est_seconds,
        source="user",
        notes=clean_text(body.notes, max_len=1000, multiline=True),
    )
    if question is None:
        raise HTTPException(409, f"This podcast already has the maximum of {settings.max_questions_per_podcast} questions.")
    await refresh_question_flags(db, request.app.state.embedder, podcast, question)
    await db.commit()
    questions = await podcast_questions(db, podcast.id)
    return {"question": serialize_question(question), "plan": podcast_plan(podcast, questions)}


@router.patch("/questions/{question_id}")
async def update_question(
    request: Request, question_id: str, body: QuestionUpdate, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)
):
    settings = get_settings()
    question, podcast = await get_owned_question(db, user.id, question_id, for_update=True)
    text_changed = False
    if body.text is not None:
        new_text = clean_text(body.text, max_len=500)
        if word_count(new_text) < 2:
            raise HTTPException(422, "Write a question with at least two words.")
        if normalize_for_compare(new_text) != normalize_for_compare(question.text):
            await save_revision(db, question, instruction="Edited by you", source="user", cap=settings.max_revisions_per_question)
            question.text = new_text
            question.text_hash = text_hash(new_text)
            question.embedding = None
            question.embedding_model = None
            text_changed = True
    if body.category is not None:
        question.category = body.category
    if body.est_seconds is not None:
        question.est_seconds = body.est_seconds
    if body.notes is not None:
        question.notes = clean_text(body.notes, max_len=1000, multiline=True)
    if text_changed:
        await refresh_question_flags(db, request.app.state.embedder, podcast, question)
    podcast.updated_at = datetime.now(timezone.utc)
    await db.commit()
    questions = await podcast_questions(db, podcast.id)
    return {"question": serialize_question(question), "plan": podcast_plan(podcast, questions)}


async def _renumber(db: AsyncSession, podcast_id: uuid.UUID) -> None:
    await db.execute(
        text(
            "UPDATE questions SET position = ranked.rn FROM ("
            " SELECT id, row_number() OVER (ORDER BY position, created_at) AS rn FROM questions WHERE podcast_id = :pid"
            ") AS ranked WHERE questions.id = ranked.id"
        ),
        {"pid": podcast_id},
    )


@router.delete("/questions/{question_id}")
async def delete_question(question_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    question, podcast = await get_owned_question(db, user.id, question_id, for_update=True)
    await db.delete(question)
    await db.flush()
    await _renumber(db, podcast.id)
    await db.commit()
    questions = await podcast_questions(db, podcast.id)
    return {"questions": [serialize_question(q) for q in questions], "plan": podcast_plan(podcast, questions)}


@router.post("/questions/{question_id}/undo")
async def undo_question(request: Request, question_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    question, podcast = await get_owned_question(db, user.id, question_id, for_update=True)
    revision = (
        await db.execute(
            select(QuestionRevision)
            .where(QuestionRevision.question_id == question.id)
            .order_by(QuestionRevision.created_at.desc(), QuestionRevision.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(409, "There's nothing to undo for this question.")
    question.text = revision.old_text
    question.category = revision.old_category
    question.est_seconds = revision.old_est_seconds
    question.text_hash = text_hash(revision.old_text)
    question.embedding = None
    question.embedding_model = None
    await db.delete(revision)
    await refresh_question_flags(db, request.app.state.embedder, podcast, question)
    await db.commit()
    questions = await podcast_questions(db, podcast.id)
    return {"question": serialize_question(question), "plan": podcast_plan(podcast, questions)}


@router.post("/podcasts/{podcast_id}/questions/reorder")
async def reorder_questions(podcast_id: str, body: ReorderBody, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id, for_update=True)
    questions = await podcast_questions(db, podcast.id)
    if len(body.ids) != len(questions) or set(body.ids) != {q.id for q in questions}:
        raise HTTPException(409, "The question list changed in the meantime. Refresh and try again.")
    by_id = {q.id: q for q in questions}
    for position, qid in enumerate(body.ids, start=1):
        by_id[qid].position = position
    await db.commit()
    ordered = sorted(questions, key=lambda q: q.position)
    return {"questions": [serialize_question(q) for q in ordered], "plan": podcast_plan(podcast, ordered)}


@router.post("/podcasts/{podcast_id}/quality-check")
async def quality_check(request: Request, podcast_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    questions = await recheck_podcast(db, request.app.state.embedder, podcast)
    model = (questions[0].embedding_model if questions else None) or HASH_MODEL
    _, to_review = review_candidates(questions, model, await load_reviews(db, podcast.id), get_settings().max_repeat_review_pairs)
    await db.commit()
    # to_review: pairs too close to call by meaning alone - the client then asks for the AI repeat check
    summary = {"duplicates": 0, "similar": 0, "asked_before": 0, "quality": 0, "to_review": to_review}
    for q in questions:
        match = (q.flags or {}).get("match")
        if match:
            summary["duplicates" if match.get("kind") == "duplicate" else "similar"] += 1
        if (q.flags or {}).get("asked_before"):
            summary["asked_before"] += 1
        if (q.flags or {}).get("quality"):
            summary["quality"] += 1
    return {"questions": [serialize_question(q) for q in questions], "summary": summary, "plan": podcast_plan(podcast, questions)}


# --------------------------------------------------------------------------- AI actions
async def _refuse_if_full(db: AsyncSession, podcast_id: uuid.UUID) -> None:
    cap = get_settings().max_questions_per_podcast
    if (await db.scalar(select(func.count(Question.id)).where(Question.podcast_id == podcast_id)) or 0) >= cap:
        raise HTTPException(409, f"This podcast already has the maximum of {cap} questions. Delete some first.")


@router.post("/podcasts/{podcast_id}/ai/generate", status_code=202)
async def ai_generate(request: Request, podcast_id: str, body: GenerateBody, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    await _refuse_if_full(db, podcast.id)
    job = await start_job(
        db=db,
        redis=get_redis(),
        router=_router(request),
        user_id=user.id,
        podcast_id=podcast.id,
        kind="generate_questions",
        params={"count": min(body.count, get_settings().max_generate_batch), "focus": clean_text(body.focus, max_len=400)},
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return {"job": serialize_job(job)}


@router.post("/podcasts/{podcast_id}/ai/rewrite", status_code=202)
async def ai_rewrite(request: Request, podcast_id: str, body: RewriteBody, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    podcast = await get_owned_podcast(db, user.id, podcast_id, for_update=body.remember)
    instruction = clean_text(body.instruction, max_len=400)
    if word_count(instruction) < 1:
        raise HTTPException(422, "Tell Mira how to change the questions.")
    ids = list(dict.fromkeys(body.question_ids))[: settings.max_rewrite_batch]
    owned = set((await db.execute(select(Question.id).where(Question.podcast_id == podcast.id, Question.id.in_(ids)))).scalars())
    if not owned:
        raise NotFound()
    if body.remember:
        items = list(podcast.standing_instructions or [])
        if instruction.lower() not in {i.lower() for i in items}:
            if len(items) >= settings.max_instructions_per_podcast:
                raise HTTPException(409, f"Mira already remembers {settings.max_instructions_per_podcast} instructions. Remove one first.")
            podcast.standing_instructions = items + [instruction]
        await db.commit()
    job = await start_job(
        db=db,
        redis=get_redis(),
        router=_router(request),
        user_id=user.id,
        podcast_id=podcast.id,
        kind="rewrite_questions",
        params={"question_ids": [str(i) for i in ids if i in owned], "instruction": instruction},
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return {"job": serialize_job(job), "standing_instructions": list(podcast.standing_instructions or [])}


@router.post("/podcasts/{podcast_id}/ai/review-repeats", status_code=202)
async def ai_review_repeats(request: Request, podcast_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    """AI double-check of question pairs whose meaning scores are too close to call."""
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    job = await start_job(
        db=db,
        redis=get_redis(),
        router=_router(request),
        user_id=user.id,
        podcast_id=podcast.id,
        kind="review_repeats",
        params={},
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return {"job": serialize_job(job)}


@router.post("/podcasts/{podcast_id}/ai/next", status_code=202)
async def ai_next_question(request: Request, podcast_id: str, body: NextBody, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    await _refuse_if_full(db, podcast.id)
    params: dict = {}
    if body.answer_id is not None:
        owned = await db.scalar(
            select(Answer.id).where(Answer.id == body.answer_id, Answer.podcast_id == podcast.id, Answer.user_id == user.id)
        )
        if owned is None:
            raise NotFound()
        params["answer_id"] = str(body.answer_id)
    job = await start_job(
        db=db,
        redis=get_redis(),
        router=_router(request),
        user_id=user.id,
        podcast_id=podcast.id,
        kind="next_question",
        params=params,
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return {"job": serialize_job(job)}


@router.post("/questions/{question_id}/answers", status_code=201)
async def submit_answer(request: Request, question_id: str, body: AnswerBody, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    from app.jobs.service import AIUnavailable, JobConflict

    settings = get_settings()
    await _once(request, user, "answer")
    question, podcast = await get_owned_question(db, user.id, question_id, for_update=True)
    answer_text = clean_text(body.text, max_len=5000, multiline=True)
    if not answer_text:
        raise HTTPException(422, "Write your answer first.")
    if body.feedback:
        reason = spam_reason(answer_text)
        if reason:
            raise HTTPException(422, reason)
        latest = (
            await db.execute(select(Answer).where(Answer.question_id == question.id).order_by(Answer.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        if (
            latest is not None
            and latest.status in ("queued", "reviewing", "reviewed")
            and normalize_for_compare(latest.text) == normalize_for_compare(answer_text)
            and latest.created_at > datetime.now(timezone.utc) - timedelta(minutes=30)
        ):
            raise HTTPException(409, "You already asked for feedback on this exact answer. Change something first.")
    words = word_count(answer_text)
    answer = Answer(
        question_id=question.id,
        podcast_id=podcast.id,
        user_id=user.id,
        text=answer_text,
        word_count=words,
        est_seconds=speaking_seconds(words, user.speaking_wpm),
        rehearsed_seconds=body.rehearsed_seconds,
        status="queued" if body.feedback else "saved",
    )
    db.add(answer)
    await db.flush()
    await prune_answers(db, question.id, settings.max_answers_per_question)
    await db.commit()
    # Serialise now: if start_job fails it rolls back, which expires every ORM object in the session.
    answer_data = serialize_answer(answer)
    answer_id = answer.id
    job = None
    job_error = None
    if body.feedback:
        try:
            job = await start_job(
                db=db,
                redis=get_redis(),
                router=_router(request),
                user_id=user.id,
                podcast_id=podcast.id,
                kind="answer_feedback",
                params={"answer_id": str(answer_id), "question_id": answer_data["question_id"]},
                idempotency_key=None,
            )
        except (AIUnavailable, JobConflict, ratelimit.RateLimited) as exc:
            job_error = getattr(exc, "message", None) or str(exc)
            await db.rollback()
            await db.execute(
                update(Answer).where(Answer.id == answer_id).values(status="saved").execution_options(synchronize_session=False)
            )
            await db.commit()
            answer_data["status"] = "saved"
    return {"answer": answer_data, "job": serialize_job(job) if job else None, "job_error": job_error}


@router.get("/questions/{question_id}/answers")
async def list_answers(question_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    question, _ = await get_owned_question(db, user.id, question_id)
    rows = (
        await db.execute(
            select(Answer).where(Answer.question_id == question.id, Answer.user_id == user.id).order_by(Answer.created_at.desc()).limit(get_settings().max_answers_per_question)
        )
    ).scalars()
    return {"answers": [serialize_answer(a) for a in rows]}


# --------------------------------------------------------------------------- jobs
@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(AIJob).where(AIJob.id == parse_uuid(job_id), AIJob.user_id == user.id))).scalar_one_or_none()
    if job is None:
        raise NotFound()
    partial = await get_redis().get(f"pq:job:{job.id}:partial") or ""
    return {"job": serialize_job(job), "partial": partial}


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    status = await cancel_job(db=db, redis=get_redis(), emitter=request.app.state.emitter, user_id=user.id, job_id=job_id)
    return {"status": status}


# --------------------------------------------------------------------------- PDF export
@router.post("/podcasts/{podcast_id}/export", status_code=201)
async def export_pdf(podcast_id: str, user: CurrentUser = Depends(write_guard), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    redis = get_redis()
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    await ratelimit.enforce(redis, "pdf", str(user.id), settings.rate("pdf"), "You're exporting a lot - please wait a few minutes.")
    token = await ratelimit.acquire_lock(redis, f"pdf:{podcast.id}", 150)
    if token is None:
        raise HTTPException(409, "A PDF for this podcast is already being prepared.")
    try:
        export = await create_export(db, user.id, podcast)
        await db.commit()
    finally:
        await ratelimit.release_lock(redis, f"pdf:{podcast.id}", token)
    return {
        "export": {
            "id": str(export.id),
            "filename": export.filename,
            "size_bytes": export.size_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "url": f"/podcasts/{podcast.id}/exports/{export.id}/download",
        }
    }


# --------------------------------------------------------------------------- voice (Kokoro TTS)
@router.get("/tts/voices")
async def tts_voices(user: CurrentUser = Depends(require_user)):
    return {"voices": voice_options(), "default": resolve_voice(user.tts_voice), "status": (await voice_status())["status"]}


@router.post("/tts")
async def tts(request: Request, body: TTSBody, user: CurrentUser = Depends(require_user)):
    settings = get_settings()
    await ratelimit.enforce(get_redis(), "tts", str(user.id), settings.rate("tts"), "Too many voice requests - please slow down.")
    speech = clean_text(body.text, max_len=settings.max_tts_chars)
    if not speech:
        raise HTTPException(422, "Nothing to say.")
    voice = resolve_voice(body.voice or user.tts_voice)
    speed = round(body.speed or user.tts_speed, 2)
    # Per-user key: another account can't probe (via hits or timing) what you had read aloud.
    key = hashlib.sha256(f"{user.id}|{voice}|{speed:.2f}|{speech}".encode()).hexdigest()
    cache = request.app.state.tts_cache
    cached = await cache.get(key)
    headers = {"Cache-Control": "private, max-age=3600"}
    if cached:
        return Response(cached, media_type="audio/mpeg", headers={**headers, "X-TTS-Cache": "hit"})
    try:
        audio = await request.app.state.kokoro.synthesize(speech, voice, speed)
    except TTSUnavailable:
        return JSONResponse(
            {"detail": "Mira's Kokoro voice is offline - using your browser's voice instead.", "fallback": "browser"},
            status_code=503,
        )
    await cache.set(key, audio)
    return Response(audio, media_type="audio/mpeg", headers=headers)
