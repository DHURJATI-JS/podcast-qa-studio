"""Question & answer domain logic.

* Quality checks that need no AI (yes/no, double-barrelled, leading, too long/short).
* Redundancy detection: same-meaning (pgvector cosine, same embedding model only) **and**
  same-wording (token overlap) - within a podcast and across the user's other podcasts.
  Pairs just below the "similar" threshold are judged by the AI; its verdicts are kept per pair
  (``QuestionReview``) and reapplied for as long as both texts are unchanged.
* Race-safe caps: inserts lock the parent row (``SELECT ... FOR UPDATE``) before counting.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import HASH_MODEL, Embedder
from app.ai.textsim import as_list, cosine, hash_embed, lexical_similarity, review_floor, thresholds
from app.models import Answer, Podcast, Question, QuestionReview, QuestionRevision
from app.security.sanitize import text_hash

_CLOSED_STARTS = (
    "do ", "does ", "did ", "is ", "are ", "was ", "were ", "can ", "could ", "would ", "will ", "have ", "has ",
    "had ", "should ", "shall ", "may ", "might ", "am ", "isn't ", "aren't ", "don't ", "doesn't ", "didn't ",
    "won't ", "wouldn't ", "can't ", "couldn't ",
)
_POLITE_OPEN = re.compile(
    r"^(can|could|would|will)\s+you\s+(please\s+)?(tell|walk|describe|share|explain|talk|take|give|paint|help|unpack|break|run)\b"
)
# "..., and what/how/why ..." - a second question glued onto the first (double-barrelled).
_SECOND_QUESTION = re.compile(
    r",\s*(and|or)\s+(what|how|why|who|when|where|which|do|does|did|is|are|was|were|would|could|can|should|will|have|has)\b"
)
_LEADING = (
    "don't you think", "wouldn't you agree", "isn't it true", "don't you agree", "surely", "obviously",
    "everyone knows", "you must agree", "wouldn't you say", "isn't it fair to say", "you'd agree",
)
FLAG_HELP = {
    "closed": "Yes/no question - start with How, What or Why to get a story.",
    "double": "Asks two things at once - split it into two questions.",
    "leading": "Leading - it hints at the answer you want.",
    "long": "Long - aim for under ~35 words.",
    "short": "Very short - add a little context.",
}


def quality_flags(text: str) -> list[str]:
    stripped = text.strip()
    lower = stripped.lower()
    words = len(re.findall(r"[\w']+", lower))
    flags = []
    if lower.startswith(_CLOSED_STARTS) and not _POLITE_OPEN.match(lower):
        flags.append("closed")
    if stripped.count("?") >= 2 or re.search(r"\?\s*(and|or|also)\b", lower) or _SECOND_QUESTION.search(lower):
        flags.append("double")
    if any(phrase in lower for phrase in _LEADING):
        flags.append("leading")
    if words > 40:
        flags.append("long")
    elif words < 5:
        flags.append("short")
    return flags


@dataclass(frozen=True)
class Match:
    kind: str  # duplicate | similar
    question_id: str | None
    text: str
    similarity: float
    method: str  # meaning | wording


_RANK = {"similar": 1, "duplicate": 2}
Comparable = tuple[object, str, list[float] | None, str | None]


def compare(text: str, vector: list[float] | None, model: str, others: list[Comparable]) -> Match | None:
    dup_t, sim_t = thresholds(model)
    best: Match | None = None
    for other_id, other_text, other_vec, other_model in others:
        lexical = lexical_similarity(text, other_text)
        semantic = cosine(vector, other_vec) if other_model == model else None
        kind, method, score = None, "wording", lexical
        if lexical >= 0.85:
            kind = "duplicate"
        if semantic is not None and semantic >= dup_t:
            kind, score = "duplicate", max(lexical, semantic)
            method = "wording" if lexical >= 0.85 else "meaning"
        if kind is None:
            if semantic is not None and semantic >= sim_t:
                kind, method, score = "similar", "meaning", semantic
            elif lexical >= 0.6:
                kind = "similar"
        if kind and (
            best is None
            or _RANK[kind] > _RANK[best.kind]
            or (_RANK[kind] == _RANK[best.kind] and score > best.similarity)
        ):
            best = Match(kind, str(other_id) if other_id else None, other_text, round(float(score), 3), method)
    return best


def build_flags(text: str, match: Match | None = None, asked_before: dict | None = None) -> dict:
    flags: dict = {"quality": quality_flags(text)}
    if match:
        flags["match"] = {
            "kind": match.kind,
            "question_id": match.question_id,
            "text": match.text[:200],
            "similarity": match.similarity,
            "method": match.method,
        }
    if asked_before:
        flags["asked_before"] = asked_before
    return flags


# --------------------------------------------------------------------------- AI repeat check
Reviews = dict[tuple[uuid.UUID, uuid.UUID], tuple[str, bool]]  # (lower id, higher id) -> (fingerprint, same)


@dataclass(frozen=True)
class ReviewPair:
    later: Question  # flagged if the AI says the two are the same question
    earlier: Question
    similarity: float


def pair_key(a_id: object, a_text: str, b_id: object, b_text: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Order-independent key of a question pair, plus a fingerprint of both texts as judged."""
    (lo_id, lo_text), (hi_id, hi_text) = sorted(
        ((uuid.UUID(str(a_id)), a_text), (uuid.UUID(str(b_id)), b_text)), key=lambda item: str(item[0])
    )
    fingerprint = hashlib.sha256(f"{text_hash(lo_text)}:{text_hash(hi_text)}".encode()).hexdigest()
    return lo_id, hi_id, fingerprint


async def load_reviews(db: AsyncSession, podcast_id: uuid.UUID) -> Reviews:
    rows = await db.execute(
        select(QuestionReview.question_id, QuestionReview.other_id, QuestionReview.fingerprint, QuestionReview.same).where(
            QuestionReview.podcast_id == podcast_id
        )
    )
    return {(row.question_id, row.other_id): (row.fingerprint, row.same) for row in rows}


def review_verdict(reviews: Reviews, a_id: object, a_text: str, b_id: object, b_text: str) -> bool | None:
    """The AI's verdict on exactly these two texts, or None if it hasn't judged them."""
    lo_id, hi_id, fingerprint = pair_key(a_id, a_text, b_id, b_text)
    hit = reviews.get((lo_id, hi_id))
    return hit[1] if hit is not None and hit[0] == fingerprint else None


def reviewed_match(question_id: object, question_text: str, vector: list[float] | None, others: list[Comparable], reviews: Reviews) -> Match | None:
    """The closest other question the AI judged to be this same question in other words."""
    best: Match | None = None
    for other_id, other_text, other_vector, _ in others:
        if review_verdict(reviews, question_id, question_text, other_id, other_text):
            score = cosine(vector, other_vector) or 0.0
            if best is None or score > best.similarity:
                best = Match("similar", str(other_id), other_text, round(float(score), 3), "ai")
    return best


def review_candidates(questions: list[Question], model: str, reviews: Reviews, limit: int) -> tuple[list[ReviewPair], int]:
    """Pairs embeddings can't settle and the AI hasn't judged (for these exact texts): each question
    without a repeat flag, with its closest earlier question in the band just below the "similar"
    threshold. Returns up to ``limit`` pairs (closest first) and how many there are in total."""
    floor, similar = review_floor(model), thresholds(model)[1]
    pairs: list[ReviewPair] = []
    earlier: list[Question] = []
    for question in questions:
        vector = as_list(question.embedding) if question.embedding_model == model else None
        if vector is not None and not (question.flags or {}).get("match"):
            best: ReviewPair | None = None
            for other in earlier:
                if other.embedding_model != model:
                    continue
                score = cosine(vector, as_list(other.embedding))
                if score is None or not floor <= score < similar:
                    continue
                if review_verdict(reviews, question.id, question.text, other.id, other.text) is not None:
                    continue
                if best is None or score > best.similarity:
                    best = ReviewPair(question, other, round(float(score), 3))
            if best is not None:
                pairs.append(best)
        earlier.append(question)
    pairs.sort(key=lambda pair: pair.similarity, reverse=True)
    return pairs[:limit], len(pairs)


async def save_review(db: AsyncSession, podcast_id: uuid.UUID, a: Question, b: Question, *, same: bool, provider: str | None) -> None:
    lo_id, hi_id, fingerprint = pair_key(a.id, a.text, b.id, b.text)
    values = {"fingerprint": fingerprint, "same": same, "provider": provider[:80] if provider else None}
    await db.execute(
        pg_insert(QuestionReview)
        .values(id=uuid.uuid4(), podcast_id=podcast_id, question_id=lo_id, other_id=hi_id, **values)
        .on_conflict_do_update(index_elements=["question_id", "other_id"], set_={**values, "created_at": func.clock_timestamp()})
    )


async def prune_reviews(db: AsyncSession, podcast_id: uuid.UUID, questions: list[Question], cap: int) -> int:
    """Drop verdicts on texts that have since changed, and keep at most ``cap`` (newest first)."""
    by_id = {q.id: q for q in questions}
    rows = (
        await db.execute(
            select(QuestionReview.id, QuestionReview.question_id, QuestionReview.other_id, QuestionReview.fingerprint)
            .where(QuestionReview.podcast_id == podcast_id)
            .order_by(QuestionReview.created_at.desc(), QuestionReview.id.desc())
        )
    ).all()
    doomed: list[uuid.UUID] = []
    kept = 0
    for row in rows:
        a, b = by_id.get(row.question_id), by_id.get(row.other_id)
        current = a is not None and b is not None and pair_key(a.id, a.text, b.id, b.text)[2] == row.fingerprint
        if current and kept < cap:
            kept += 1
        else:
            doomed.append(row.id)
    if doomed:
        await db.execute(delete(QuestionReview).where(QuestionReview.id.in_(doomed)))
    return len(doomed)


def serialize_question(q: Question) -> dict:
    return {
        "id": str(q.id),
        "position": q.position,
        "text": q.text,
        "category": q.category,
        "est_seconds": q.est_seconds,
        "source": q.source,
        "notes": q.notes,
        "flags": q.flags or {},
    }


def serialize_answer(a: Answer) -> dict:
    return {
        "id": str(a.id),
        "question_id": str(a.question_id),
        "text": a.text,
        "word_count": a.word_count,
        "est_seconds": a.est_seconds,
        "rehearsed_seconds": a.rehearsed_seconds,
        "status": a.status,
        "score": a.score,
        "feedback": a.feedback,
        "provider": a.provider,
        "redundant_with": a.redundant_with,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


async def podcast_questions(db: AsyncSession, podcast_id: uuid.UUID) -> list[Question]:
    rows = await db.execute(select(Question).where(Question.podcast_id == podcast_id).order_by(Question.position, Question.created_at))
    return list(rows.scalars())


async def ensure_embeddings(db: AsyncSession, embedder: Embedder, questions: list[Question], model: str) -> str:
    """Embed every question with ``model`` (in one batch). Returns the model actually used."""
    stale = [q for q in questions if q.embedding is None or q.embedding_model != model]
    if not stale:
        return model
    try:
        vectors = await embedder.embed([q.text for q in stale], model)
    except Exception:  # noqa: BLE001 - fall back to the always-available embedder
        if model == HASH_MODEL:
            raise
        model = HASH_MODEL
        stale = [q for q in questions if q.embedding is None or q.embedding_model != model]
        vectors = [hash_embed(q.text) for q in stale]
    for question, vector in zip(stale, vectors):
        question.embedding = vector
        question.embedding_model = model
    await db.flush()
    return model


async def _exact_filtered_vector_search(db: AsyncSession) -> None:
    """With an HNSW index, Postgres may fetch only the ~40 nearest rows of *all* users and then
    apply the owner filter. pgvector 0.8 iterative scans keep searching until enough rows pass
    the filter (transaction-local settings)."""
    await db.execute(
        text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true), set_config('hnsw.ef_search', '100', true)")
    )


async def cross_podcast_match(
    db: AsyncSession, owner_id: uuid.UUID, podcast_id: uuid.UUID, vector: list[float] | None, model: str
) -> dict | None:
    """Closest question in the owner's *other* podcasts (pgvector cosine distance)."""
    if vector is None:
        return None
    _, sim_t = thresholds(model)
    await _exact_filtered_vector_search(db)
    distance = Question.embedding.cosine_distance(vector)
    row = (
        await db.execute(
            select(Question.text, Podcast.title, Podcast.id, distance.label("distance"))
            .join(Podcast, Podcast.id == Question.podcast_id)
            .where(
                Podcast.owner_id == owner_id,
                Question.podcast_id != podcast_id,
                Question.embedding_model == model,
                Question.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    similarity = 1.0 - float(row.distance)
    if similarity < sim_t:
        return None
    return {"podcast_id": str(row.id), "podcast_title": row.title, "text": row.text[:200], "similarity": round(similarity, 3)}


async def insert_question(
    db: AsyncSession,
    *,
    podcast_id: uuid.UUID,
    cap: int,
    text: str,
    category: str,
    est_seconds: int,
    source: str,
    notes: str = "",
    vector: list[float] | None = None,
    model: str | None = None,
    flags: dict | None = None,
    after_position: int | None = None,
) -> Question | None:
    """Insert at the end (or right after ``after_position``); None if the podcast is full. Race-safe."""
    await db.execute(select(Podcast.id).where(Podcast.id == podcast_id).with_for_update())
    count = await db.scalar(select(func.count(Question.id)).where(Question.podcast_id == podcast_id))
    if (count or 0) >= cap:
        return None
    if after_position is not None:
        await db.execute(
            update(Question)
            .where(Question.podcast_id == podcast_id, Question.position > after_position)
            .values(position=Question.position + 1)
        )
        position = after_position + 1
    else:
        last = await db.scalar(select(func.max(Question.position)).where(Question.podcast_id == podcast_id))
        position = (last or 0) + 1
    question = Question(
        podcast_id=podcast_id,
        position=position,
        text=text,
        category=category,
        est_seconds=max(10, min(1800, int(est_seconds))),
        source=source,
        notes=notes,
        flags=flags if flags is not None else build_flags(text),
        text_hash=text_hash(text),
        embedding=vector,
        embedding_model=model if vector is not None else None,
    )
    db.add(question)
    await db.execute(update(Podcast).where(Podcast.id == podcast_id).values(updated_at=func.now()))
    await db.flush()
    return question


async def save_revision(db: AsyncSession, question: Question, *, instruction: str, source: str, cap: int) -> None:
    db.add(
        QuestionRevision(
            question_id=question.id,
            old_text=question.text,
            old_category=question.category,
            old_est_seconds=question.est_seconds,
            instruction=instruction[:500],
            source=source,
        )
    )
    await db.flush()
    stale = (
        await db.execute(
            select(QuestionRevision.id)
            .where(QuestionRevision.question_id == question.id)
            .order_by(QuestionRevision.created_at.desc(), QuestionRevision.id.desc())
            .offset(cap)
        )
    ).scalars().all()
    if stale:
        await db.execute(delete(QuestionRevision).where(QuestionRevision.id.in_(stale)))


async def refresh_question_flags(db: AsyncSession, embedder: Embedder, podcast: Podcast, question: Question) -> None:
    """Re-check one question (after an edit) against the rest of the podcast + other podcasts."""
    questions = await podcast_questions(db, podcast.id)
    model = await ensure_embeddings(db, embedder, questions, await embedder.pick_model())
    others = [(q.id, q.text, as_list(q.embedding), q.embedding_model) for q in questions if q.id != question.id]
    vector = as_list(question.embedding)
    reviews = await load_reviews(db, podcast.id)
    match = compare(question.text, vector, model, others) or reviewed_match(question.id, question.text, vector, others, reviews)
    asked = await cross_podcast_match(db, podcast.owner_id, podcast.id, vector, model)
    question.flags = build_flags(question.text, match, asked)
    await db.flush()


async def recheck_podcast(db: AsyncSession, embedder: Embedder, podcast: Podcast) -> list[Question]:
    """Full quality + redundancy pass. Later questions are flagged against earlier ones."""
    questions = await podcast_questions(db, podcast.id)
    if not questions:
        return questions
    model = await ensure_embeddings(db, embedder, questions, await embedder.pick_model())
    reviews = await load_reviews(db, podcast.id)
    earlier: list[Comparable] = []
    for question in questions:
        vector = as_list(question.embedding)
        match = compare(question.text, vector, model, earlier) or reviewed_match(question.id, question.text, vector, earlier, reviews)
        asked = await cross_podcast_match(db, podcast.owner_id, podcast.id, vector, model)
        question.flags = build_flags(question.text, match, asked)
        earlier.append((question.id, question.text, vector, model))
    await db.flush()
    return questions


async def related_answers(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    vector: list[float],
    model: str,
    exclude_question_id: uuid.UUID,
    limit: int = 3,
) -> list[dict]:
    """The user's earlier answers closest in meaning (pgvector) - used as AI memory."""
    await _exact_filtered_vector_search(db)
    distance = Answer.embedding.cosine_distance(vector)
    rows = (
        await db.execute(
            select(
                Answer.text,
                Answer.podcast_id,
                Answer.question_id,
                Question.text.label("question_text"),
                distance.label("distance"),
            )
            .join(Question, Question.id == Answer.question_id)
            .where(
                Answer.user_id == user_id,
                Answer.question_id != exclude_question_id,
                Answer.embedding_model == model,
                Answer.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(12)
        )
    ).all()
    seen: set[uuid.UUID] = set()
    out = []
    for row in rows:
        if row.question_id in seen:
            continue
        seen.add(row.question_id)
        out.append(
            {
                "question": row.question_text,
                "answer": row.text,
                "podcast_id": row.podcast_id,
                "similarity": 1.0 - float(row.distance),
            }
        )
        if len(out) >= limit:
            break
    return out


async def prune_answers(db: AsyncSession, question_id: uuid.UUID, cap: int) -> None:
    stale = (
        await db.execute(
            select(Answer.id)
            .where(Answer.question_id == question_id)
            .order_by(Answer.created_at.desc(), Answer.id.desc())
            .offset(cap)
        )
    ).scalars().all()
    if stale:
        await db.execute(delete(Answer).where(Answer.id.in_(stale)))
