"""AI job execution (worker process).

Each job streams model output to its owner's private Socket.IO room while it is being
generated. Parsed items (questions) are embedded **in parallel** with the ongoing stream and
saved one by one in order, so redundancy checks always see every earlier question.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from sqlalchemy import func, select, update

from app.ai import prompts
from app.ai.embeddings import HASH_MODEL, Embedder
from app.ai.parsing import ParsedQuestion, QuestionStreamParser, first_question, parse_feedback, parse_verdict
from app.ai.providers import Provider
from app.ai.router import AIRouter, NoAIAvailable
from app.ai.textsim import as_list, hash_embed, thresholds
from app.config import get_settings
from app.db import session_scope
from app.jobs.emitter import Emitter, user_room
from app.jobs.service import release_job_locks
from app.models import AIJob, Answer, Podcast, Question, User
from app.security.sanitize import clean_text, normalize_for_compare, text_hash
from app.services import questions as qsvc
from app.services.duration import plan_summary

log = logging.getLogger(__name__)

PARTIAL_LIMIT = 20_000
NEXT_QUESTION_ATTEMPTS = 2  # a follow-up that repeats an existing question gets one automatic retry
INTERRUPTED = "The AI worker restarted while working on this. Please try again."


class JobCancelled(Exception):
    pass


class JobFailed(Exception):
    """A user-facing failure message."""


@dataclass
class JobContext:
    router: AIRouter
    embedder: Embedder
    redis: aioredis.Redis
    emitter: Emitter


@dataclass
class RunningJob:
    id: uuid.UUID
    user_id: uuid.UUID
    podcast_id: uuid.UUID | None
    kind: str
    params: dict
    provider_label: str = ""
    pending: list[str] = field(default_factory=list)
    last_flush: float = 0.0
    last_cancel_check: float = 0.0
    partial_chars: int = 0

    @property
    def room(self) -> str:
        return user_room(self.user_id)

    def base(self) -> dict:
        return {"job_id": str(self.id), "podcast_id": str(self.podcast_id) if self.podcast_id else None, "kind": self.kind}


class Streamer:
    """Batches tokens into ~5 events/second, persists partial output, honours cancel requests."""

    def __init__(self, ctx: JobContext, job: RunningJob) -> None:
        self.ctx = ctx
        self.job = job

    async def emit(self, event: str, **data: object) -> None:
        await self.ctx.emitter.emit(event, {**self.job.base(), **data}, room=self.job.room)

    async def check_cancel(self) -> None:
        if await self.ctx.redis.exists(f"pq:job:{self.job.id}:cancel"):
            raise JobCancelled()

    async def token(self, piece: str) -> None:
        job = self.job
        job.pending.append(piece)
        now = time.monotonic()
        if now - job.last_cancel_check > 0.5:
            job.last_cancel_check = now
            await self.check_cancel()
        if now - job.last_flush >= 0.2 or sum(len(p) for p in job.pending) > 400:
            await self.flush()

    async def flush(self) -> None:
        job = self.job
        if not job.pending:
            return
        text = "".join(job.pending)
        job.pending.clear()
        job.last_flush = time.monotonic()
        await self.emit("job:delta", text=text)
        if job.partial_chars < PARTIAL_LIMIT:
            key = f"pq:job:{job.id}:partial"
            await self.ctx.redis.append(key, text[: PARTIAL_LIMIT - job.partial_chars])
            await self.ctx.redis.expire(key, 3600)
            job.partial_chars += len(text)

    async def on_start(self, provider: Provider) -> None:
        self.job.provider_label = provider.label
        await self.emit("job:update", status="running", provider=provider.label)

    async def on_reset(self, provider: Provider, reason: str) -> None:
        await self.restart(f"{provider.label} stopped responding ({reason}). Switching to the next AI...")

    async def restart(self, message: str) -> None:
        """Throw away the streamed output (the browser clears it and shows `message`) and start again."""
        self.job.pending.clear()
        self.job.partial_chars = 0
        await self.ctx.redis.delete(f"pq:job:{self.job.id}:partial")
        await self.emit("job:reset", reason=message)


Handler = Callable[[JobContext, RunningJob, Streamer], Awaitable[dict]]


async def _owned_podcast(db, job: RunningJob) -> Podcast:  # noqa: ANN001
    podcast = (
        await db.execute(select(Podcast).where(Podcast.id == job.podcast_id, Podcast.owner_id == job.user_id))
    ).scalar_one_or_none()
    if podcast is None:
        raise JobFailed("This podcast no longer exists.")
    return podcast


async def _embed_one(embedder: Embedder, text: str, model: str) -> list[float] | None:
    try:
        return (await embedder.embed([text], model))[0]
    except Exception:  # noqa: BLE001 - wording checks still work without a vector
        return hash_embed(text) if model == HASH_MODEL else None


# --------------------------------------------------------------------------- generate questions
async def handle_generate(ctx: JobContext, job: RunningJob, streamer: Streamer) -> dict:
    settings = get_settings()
    wanted_raw = max(1, min(settings.max_generate_batch, int(job.params.get("count", 5))))
    focus = clean_text(job.params.get("focus", ""), max_len=400)
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        pctx = prompts.PodcastContext.from_model(podcast)
        existing = await qsvc.podcast_questions(db, podcast.id)
        model = await qsvc.ensure_embeddings(db, ctx.embedder, existing, await ctx.embedder.pick_model())
        await db.commit()
        accepted: list[qsvc.Comparable] = [(q.id, q.text, as_list(q.embedding), q.embedding_model) for q in existing]
        seconds = [q.est_seconds for q in existing]
        owner_id, podcast_id = podcast.owner_id, podcast.id
        target, intro, outro = podcast.target_minutes, podcast.intro_seconds, podcast.outro_seconds
    capacity = settings.max_questions_per_podcast - len(existing)
    if capacity <= 0:
        raise JobFailed(f"This podcast already has the maximum of {settings.max_questions_per_podcast} questions. Delete some first.")
    wanted = min(wanted_raw, capacity)
    added: list[dict] = []
    skipped: list[dict] = []

    async def build():  # noqa: ANN202 - re-built for every provider attempt
        plan = plan_summary(target, intro, outro, seconds)
        return prompts.generate_questions(
            pctx, [a[1] for a in accepted], max(1, wanted - len(added)), focus, plan["remaining_seconds"], plan["planned_seconds"]
        )

    queue: asyncio.Queue[tuple[ParsedQuestion, asyncio.Task] | None] = asyncio.Queue()

    async def consume() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            parsed, embedding_task = item
            vector = await embedding_task
            if len(added) >= wanted:
                continue
            match = qsvc.compare(parsed.text, vector, model, accepted)
            if match and match.kind == "duplicate":
                entry = {"text": parsed.text, "reason": f"Too similar to: {match.text[:90]}"}
                skipped.append(entry)
                await streamer.emit("job:skipped", **entry)
                continue
            async with session_scope() as db:
                asked = await qsvc.cross_podcast_match(db, owner_id, podcast_id, vector, model)
                question = await qsvc.insert_question(
                    db,
                    podcast_id=podcast_id,
                    cap=settings.max_questions_per_podcast,
                    text=parsed.text,
                    category=parsed.category,
                    est_seconds=parsed.seconds,
                    source="ai",
                    vector=vector,
                    model=model,
                    flags=qsvc.build_flags(parsed.text, match, asked),
                )
                if question is None:
                    await db.rollback()
                    skipped.append({"text": parsed.text, "reason": "Question limit reached"})
                    continue
                await db.commit()
                data = qsvc.serialize_question(question)
            accepted.append((question.id, question.text, vector, model))
            seconds.append(question.est_seconds)
            added.append(data)
            await streamer.emit("job:question", question=data)

    consumer = asyncio.create_task(consume())
    parser = QuestionStreamParser()

    async def on_token(piece: str) -> None:
        await streamer.token(piece)
        for parsed in parser.feed(piece):
            await queue.put((parsed, asyncio.create_task(_embed_one(ctx.embedder, parsed.text, model))))

    async def on_reset(provider: Provider, reason: str) -> None:
        parser.reset()
        await streamer.on_reset(provider, reason)

    try:
        await ctx.router.run(build, on_start=streamer.on_start, on_token=on_token, on_reset=on_reset)
        for parsed in parser.flush():
            await queue.put((parsed, asyncio.create_task(_embed_one(ctx.embedder, parsed.text, model))))
    finally:
        await queue.put(None)
        await consumer
    if not added:
        if skipped:
            return {
                "added": 0,
                "skipped": skipped[:20],
                "message": "Every suggestion was too similar to questions you already have. Try a different focus.",
            }
        raise JobFailed("The AI reply didn't contain any questions in the expected format. Please try again.")
    return {"added": len(added), "skipped": skipped[:20]}


# --------------------------------------------------------------------------- rewrite questions
async def handle_rewrite(ctx: JobContext, job: RunningJob, streamer: Streamer) -> dict:
    settings = get_settings()
    instruction = clean_text(job.params.get("instruction", ""), max_len=400)
    ids = []
    for raw in job.params.get("question_ids", [])[: settings.max_rewrite_batch]:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        pctx = prompts.PodcastContext.from_model(podcast)
        all_questions = await qsvc.podcast_questions(db, podcast.id)
        rows = [q for q in all_questions if q.id in set(ids)]
        plan = plan_summary(podcast.target_minutes, podcast.intro_seconds, podcast.outro_seconds, [q.est_seconds for q in all_questions])
        podcast_id = podcast.id
    if not rows:
        raise JobFailed("Those questions no longer exist.")
    numbered = {n: q.id for n, q in enumerate(rows, start=1)}
    items = [(n, q.category, q.est_seconds, q.text) for n, q in enumerate(rows, start=1)]
    done: dict[int, dict] = {}
    model = await ctx.embedder.pick_model()

    async def build():  # noqa: ANN202
        todo = [item for item in items if item[0] not in done] or items
        return prompts.rewrite_questions(pctx, todo, instruction, plan["planned_seconds"])

    queue: asyncio.Queue[tuple[ParsedQuestion, asyncio.Task] | None] = asyncio.Queue()

    async def consume() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            parsed, embedding_task = item
            vector = await embedding_task
            if parsed.ref not in numbered or parsed.ref in done:
                continue
            async with session_scope() as db:
                question = (
                    await db.execute(
                        select(Question).where(Question.id == numbered[parsed.ref], Question.podcast_id == podcast_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if question is None:
                    continue
                if normalize_for_compare(question.text) != normalize_for_compare(parsed.text) or question.category != parsed.category:
                    await qsvc.save_revision(db, question, instruction=instruction, source="ai", cap=settings.max_revisions_per_question)
                    question.text = parsed.text
                    question.category = parsed.category
                    question.est_seconds = parsed.seconds
                    question.text_hash = text_hash(parsed.text)
                    question.embedding = vector
                    question.embedding_model = model if vector is not None else None
                    question.flags = qsvc.build_flags(parsed.text)
                    await db.execute(update(Podcast).where(Podcast.id == podcast_id).values(updated_at=func.now()))
                await db.commit()
                data = qsvc.serialize_question(question)
            done[parsed.ref] = data
            await streamer.emit("job:question", question=data, updated=True)

    consumer = asyncio.create_task(consume())
    parser = QuestionStreamParser(require_ref=True)

    async def on_token(piece: str) -> None:
        await streamer.token(piece)
        for parsed in parser.feed(piece):
            await queue.put((parsed, asyncio.create_task(_embed_one(ctx.embedder, parsed.text, model))))

    async def on_reset(provider: Provider, reason: str) -> None:
        parser.reset()
        await streamer.on_reset(provider, reason)

    try:
        await ctx.router.run(build, on_start=streamer.on_start, on_token=on_token, on_reset=on_reset)
        for parsed in parser.flush():
            await queue.put((parsed, asyncio.create_task(_embed_one(ctx.embedder, parsed.text, model))))
    finally:
        await queue.put(None)
        await consumer
    if not done:
        raise JobFailed("The AI reply couldn't be matched to your questions. Please try again.")
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        refreshed = await qsvc.recheck_podcast(db, ctx.embedder, podcast)
        await db.commit()
        await streamer.emit("job:flags", questions=[qsvc.serialize_question(q) for q in refreshed])
    return {"updated": len(done), "requested": len(rows)}


# --------------------------------------------------------------------------- answer feedback
async def handle_feedback(ctx: JobContext, job: RunningJob, streamer: Streamer) -> dict:
    answer_id = uuid.UUID(str(job.params["answer_id"]))
    async with session_scope() as db:
        row = (
            await db.execute(
                select(Answer, Question, Podcast)
                .join(Question, Question.id == Answer.question_id)
                .join(Podcast, Podcast.id == Answer.podcast_id)
                .where(Answer.id == answer_id, Answer.user_id == job.user_id, Podcast.owner_id == job.user_id)
            )
        ).first()
        if row is None:
            raise JobFailed("That answer no longer exists.")
        answer, question, podcast = row
        user = await db.get(User, job.user_id)
        wpm = int(user.speaking_wpm) if user else 150
        pctx = prompts.PodcastContext.from_model(podcast)
        question_text, category, slot = question.text, question.category, question.est_seconds
        answer_text, words, est, rehearsed = answer.text, answer.word_count, answer.est_seconds, answer.rehearsed_seconds
        question_id, podcast_id = question.id, podcast.id
    try:
        model, vectors = await ctx.embedder.embed_best([answer_text])
        vector = vectors[0]
        async with session_scope() as db:
            memory = await qsvc.related_answers(
                db, user_id=job.user_id, vector=vector, model=model, exclude_question_id=question_id, limit=3
            )
            await db.execute(update(Answer).where(Answer.id == answer_id).values(embedding=vector, embedding_model=model, status="reviewing"))
            await db.commit()
        dup_t, _ = thresholds(model)
        redundant = next((m for m in memory if m["podcast_id"] == podcast_id and m["similarity"] >= dup_t), None)
        redundant_info = (
            {"question": redundant["question"][:200], "similarity": round(redundant["similarity"], 3)} if redundant else None
        )
        memory_pairs = [(m["question"], m["answer"]) for m in memory if m["similarity"] >= 0.3]

        async def build():  # noqa: ANN202
            return prompts.answer_feedback(
                pctx,
                question=question_text,
                category=category,
                slot_seconds=slot,
                answer=answer_text,
                words=words,
                est_seconds=est,
                rehearsed_seconds=rehearsed,
                wpm=wpm,
                memory=memory_pairs,
                redundant_with=redundant_info["question"] if redundant_info else None,
            )

        result = await ctx.router.run(build, on_start=streamer.on_start, on_token=streamer.token, on_reset=streamer.on_reset)
        feedback = parse_feedback(result.text)
        async with session_scope() as db:
            answer = await db.get(Answer, answer_id)
            if answer is None:
                raise JobFailed("That answer was deleted.")
            answer.feedback = feedback
            answer.score = feedback.get("score")
            answer.status = "reviewed"
            answer.provider = result.provider.label
            answer.redundant_with = redundant_info
            await db.commit()
            data = qsvc.serialize_answer(answer)
    except BaseException:  # includes the CancelledError raised by the job timeout / shutdown
        try:
            async with session_scope() as db:
                await db.execute(update(Answer).where(Answer.id == answer_id, Answer.status != "reviewed").values(status="failed"))
                await db.commit()
        except Exception:  # noqa: BLE001 - the maintenance sweep is the last line of defence
            log.warning("Could not mark answer %s as failed", answer_id)
        raise
    await streamer.emit("job:feedback", answer=data)
    return {"answer_id": str(answer_id), "score": data["score"]}


# --------------------------------------------------------------------------- next / follow-up question
async def handle_next(ctx: JobContext, job: RunningJob, streamer: Streamer) -> dict:
    settings = get_settings()
    follow_id = job.params.get("answer_id")
    last_question = last_answer = None
    after_position = None
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        pctx = prompts.PodcastContext.from_model(podcast)
        existing = await qsvc.podcast_questions(db, podcast.id)
        if len(existing) >= settings.max_questions_per_podcast:
            raise JobFailed(f"This podcast already has the maximum of {settings.max_questions_per_podcast} questions.")
        if follow_id:
            row = (
                await db.execute(
                    select(Answer, Question)
                    .join(Question, Question.id == Answer.question_id)
                    .where(Answer.id == uuid.UUID(str(follow_id)), Answer.user_id == job.user_id, Answer.podcast_id == podcast.id)
                )
            ).first()
            if row is not None:
                last_answer, last_question, after_position = row[0].text, row[1].text, row[1].position
        model = await qsvc.ensure_embeddings(db, ctx.embedder, existing, await ctx.embedder.pick_model())
        await db.commit()
        others: list[qsvc.Comparable] = [(q.id, q.text, as_list(q.embedding), q.embedding_model) for q in existing]
        podcast_id, owner_id = podcast.id, podcast.owner_id

    rejected: list[str] = []

    async def build():  # noqa: ANN202
        return prompts.next_question(
            pctx, [o[1] for o in others], last_question=last_question, last_answer=last_answer, rejected=rejected
        )

    for attempt in range(1, NEXT_QUESTION_ATTEMPTS + 1):
        result = await ctx.router.run(build, on_start=streamer.on_start, on_token=streamer.token, on_reset=streamer.on_reset)
        parsed = first_question(result.text)
        if parsed is None:
            raise JobFailed("The AI didn't return a usable question. Please try again.")
        vector = await _embed_one(ctx.embedder, parsed.text, model)
        match = qsvc.compare(parsed.text, vector, model, others)
        if not (match and match.kind == "duplicate"):
            break
        if attempt == NEXT_QUESTION_ATTEMPTS:
            raise JobFailed(f'The AI suggested a question you already have ("{match.text[:80]}"). Please try again.')
        rejected.append(parsed.text)  # one more try, telling the AI exactly what to avoid
        await streamer.restart("Mira suggested a question you already have - asking for a different one...")
    async with session_scope() as db:
        asked = await qsvc.cross_podcast_match(db, owner_id, podcast_id, vector, model)
        question = await qsvc.insert_question(
            db,
            podcast_id=podcast_id,
            cap=settings.max_questions_per_podcast,
            text=parsed.text,
            category="follow_up" if last_answer else parsed.category,
            est_seconds=parsed.seconds,
            source="ai_followup" if last_answer else "ai",
            vector=vector,
            model=model,
            flags=qsvc.build_flags(parsed.text, match, asked),
            after_position=after_position,
        )
        if question is None:
            raise JobFailed("Question limit reached.")
        await db.commit()
        data = qsvc.serialize_question(question)
    await streamer.emit("job:question", question=data, ask=True)
    return {"question_id": data["id"], "ask": True}


# --------------------------------------------------------------------------- AI repeat check
REVIEW_BOTH_ORDERS = False


def _short(text_: str, limit: int = 60) -> str:
    return text_ if len(text_) <= limit else text_[: limit - 1].rstrip() + "…"


async def _judge_pair(ctx: JobContext, streamer: Streamer, a: str, b: str) -> tuple[bool | None, str]:
    """One pair, one short AI call. Returns (same?, provider label); same is None if unreadable."""

    async def build():  # noqa: ANN202
        return prompts.same_question(a, b)

    async def quiet(_piece: str) -> None:  # the one-word reply isn't streamed; a summary line is
        return None

    async def reset(_provider: Provider, _reason: str) -> None:
        return None

    # temperature 0: the setting the judge was measured at (held-out accuracy 97% with gemma3:4b)
    result = await ctx.router.run(build, on_start=streamer.on_start, on_token=quiet, on_reset=reset, temperature=0.0)
    return parse_verdict(result.text), result.provider.label


async def handle_review_repeats(ctx: JobContext, job: RunningJob, streamer: Streamer) -> dict:
    """Ask the AI, one pair at a time, about question pairs that embeddings can't settle; each verdict
    is remembered for that exact pair of texts, so unchanged pairs are never asked about twice."""
    settings = get_settings()
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        questions = await qsvc.recheck_podcast(db, ctx.embedder, podcast)
        model = (questions[0].embedding_model if questions else None) or HASH_MODEL
        reviews = await qsvc.load_reviews(db, podcast.id)
        pairs, _ = qsvc.review_candidates(questions, model, reviews, settings.max_repeat_review_pairs)
        judged = [
            (p.later.id, p.earlier.id, qsvc.pair_key(p.later.id, p.later.text, p.earlier.id, p.earlier.text)[2], p.later.text, p.earlier.text)
            for p in pairs
        ]
        if not judged:
            await qsvc.prune_reviews(db, podcast.id, questions, settings.max_reviews_per_podcast)
        await db.commit()
    if not judged:
        return {"checked": 0, "repeats": 0}

    verdicts: dict[int, tuple[bool, str]] = {}  # pair number -> (same?, provider label)
    for number, (*_, later_text, earlier_text) in enumerate(judged, start=1):
        await streamer.check_cancel()
        same, provider = await _judge_pair(ctx, streamer, earlier_text, later_text)  # A = earlier, as measured
        if same and REVIEW_BOTH_ORDERS:  # confirm in the other order: order-dependent "same" answers are dropped
            same, provider = await _judge_pair(ctx, streamer, later_text, earlier_text)
        if same is not None:
            verdicts[number] = (same, provider)
        label = "no answer" if same is None else "same question" if same else "different"
        await streamer.token(f"{number}/{len(judged)} {label}: {_short(later_text)} / {_short(earlier_text)}\n")
    if not verdicts:
        raise JobFailed("Mira's answers couldn't be read. Please try again.")
    checked = repeats = 0
    async with session_scope() as db:
        podcast = await _owned_podcast(db, job)
        current = {q.id: q for q in await qsvc.podcast_questions(db, podcast.id)}
        for number, (later_id, earlier_id, fingerprint, _, _) in enumerate(judged, start=1):
            later, earlier = current.get(later_id), current.get(earlier_id)
            if number not in verdicts or later is None or earlier is None:
                continue
            if qsvc.pair_key(later.id, later.text, earlier.id, earlier.text)[2] != fingerprint:
                continue  # edited while the AI was thinking: the verdict is about the old wording
            same, provider = verdicts[number]
            await qsvc.save_review(db, podcast.id, later, earlier, same=same, provider=provider)
            checked += 1
            repeats += int(same)
        await qsvc.prune_reviews(db, podcast.id, list(current.values()), settings.max_reviews_per_podcast)
        refreshed = await qsvc.recheck_podcast(db, ctx.embedder, podcast)  # applies the new verdicts
        await db.commit()
        await streamer.emit("job:flags", questions=[qsvc.serialize_question(q) for q in refreshed])
    return {"checked": checked, "repeats": repeats, "unanswered": len(judged) - len(verdicts)}


HANDLERS: dict[str, Handler] = {
    "generate_questions": handle_generate,
    "rewrite_questions": handle_rewrite,
    "answer_feedback": handle_feedback,
    "next_question": handle_next,
    "review_repeats": handle_review_repeats,
}


# --------------------------------------------------------------------------- lifecycle
async def run_job(ctx: JobContext, job_id_text: str) -> None:
    try:
        job_id = uuid.UUID(job_id_text)
    except ValueError:
        log.warning("Ignoring malformed job id %r", job_id_text)
        return
    async with session_scope() as db:
        row = (
            await db.execute(
                update(AIJob)
                .where(AIJob.id == job_id, AIJob.status == "queued")
                .values(status="running", started_at=func.now())
                .returning(AIJob.user_id, AIJob.podcast_id, AIJob.kind, AIJob.params)
            )
        ).first()
        if row is None:
            # A message re-delivered after a worker crash: report it instead of re-running.
            stale = (
                await db.execute(
                    update(AIJob)
                    .where(AIJob.id == job_id, AIJob.status == "running")
                    .values(status="failed", error=INTERRUPTED, finished_at=func.now())
                    .returning(AIJob.user_id, AIJob.podcast_id, AIJob.kind, AIJob.params)
                )
            ).first()
            await db.commit()
            if stale is not None:
                job = RunningJob(job_id, stale.user_id, stale.podcast_id, stale.kind, dict(stale.params or {}))
                await release_job_locks(ctx.redis, job.params)
                await ctx.emitter.emit("job:error", {**job.base(), "status": "failed", "message": INTERRUPTED}, room=job.room)
            return
        await db.commit()

    job = RunningJob(job_id, row.user_id, row.podcast_id, row.kind, dict(row.params or {}))
    streamer = Streamer(ctx, job)
    await streamer.emit("job:update", status="running", provider=None)
    status, error, result = "failed", None, None
    try:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            raise JobFailed("Unknown AI task.")
        async with asyncio.timeout(get_settings().ai_job_timeout):
            result = await handler(ctx, job, streamer)
        status = "succeeded"
    except JobCancelled:
        status, error = "cancelled", "Cancelled."
    except (NoAIAvailable, JobFailed) as exc:
        error = str(exc)
    except TimeoutError:
        error = "The AI took too long and was stopped. Try again, or ask for less at once."
    except asyncio.CancelledError:
        error = INTERRUPTED
        raise
    except Exception:  # noqa: BLE001
        log.exception("AI job %s (%s) crashed", job.id, job.kind)
        error = "Something went wrong while working with the AI. Please try again."
    finally:
        try:
            await streamer.flush()
        except Exception:  # noqa: BLE001
            pass
        async with session_scope() as db:
            await db.execute(
                update(AIJob)
                .where(AIJob.id == job.id)
                .values(
                    status=status,
                    error=(error or None) and error[:1000],
                    result=result,
                    provider=job.provider_label or None,
                    finished_at=func.now(),
                )
            )
            await db.commit()
        await release_job_locks(ctx.redis, job.params)
        await ctx.redis.delete(f"pq:job:{job.id}:cancel")
        await ctx.redis.expire(f"pq:job:{job.id}:partial", 300)
    if status == "succeeded":
        await streamer.emit("job:done", status=status, provider=job.provider_label, result=result)
    else:
        await streamer.emit("job:error", status=status, message=error, provider=job.provider_label)
