"""AI repeat check: pairs that embeddings can't settle are judged by the AI, and each verdict is
remembered for that exact pair of texts (re-applied without asking again; retired by an edit)."""

from __future__ import annotations

import math
import uuid

from sqlalchemy import func, select, update

from app.ai.embeddings import HASH_MODEL
from app.ai.parsing import parse_verdict
from app.ai.prompts import JUDGE_SYSTEM_PROMPT
from app.ai.textsim import review_floor, thresholds
from app.db import session_scope
from app.models import EMBED_DIM, Question, QuestionReview
from app.security.sanitize import text_hash
from app.services.questions import pair_key, review_candidates
from tests.conftest import FakeProvider, add_question, create_podcast, headers, signup

ORIGIN = "What first pulled you toward cold-chain technology?"
REWORDED = "Why did you get into cold storage in the first place?"
OTHER = "How do farmers pay for the cold rooms?"


def axis(i: int) -> list[float]:
    vector = [0.0] * EMBED_DIM
    vector[i] = 1.0
    return vector


def towards(i: int, j: int, similarity: float) -> list[float]:
    """A unit vector with exactly ``similarity`` cosine to axis(i) (and 0 to every other axis but j)."""
    vector = [0.0] * EMBED_DIM
    vector[i], vector[j] = similarity, math.sqrt(1 - similarity * similarity)
    return vector


async def set_vectors(vectors: dict[str, list[float]]) -> None:
    async with session_scope() as db:
        for question_id, vector in vectors.items():
            await db.execute(update(Question).where(Question.id == uuid.UUID(question_id)).values(embedding=vector, embedding_model=HASH_MODEL))
        await db.commit()


async def check(client, me, podcast_id) -> dict:
    return (await client.post(f"/api/podcasts/{podcast_id}/quality-check", json={}, headers=headers(me["csrf"]))).json()


async def review(client, me, podcast_id, ai) -> dict:
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/review-repeats", json={}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    return (await client.get(f"/api/jobs/{job['id']}")).json()["job"]


class Judge(FakeProvider):
    """A consistent fake model: SAME when the pair mentions any of ``markers``, else DIFFERENT."""

    def __init__(self, *markers: str) -> None:
        super().__init__("gateway")
        self.markers = markers
        self.temperatures: list[float] = []

    async def stream(self, system, messages, *, max_tokens, temperature):  # noqa: ANN001
        self.calls.append((system, messages))
        self.temperatures.append(temperature)
        pair = messages[-1].content.split("<pairs>")[1].split("</pairs>")[0]
        yield "SAME" if any(marker in pair for marker in self.markers) else "DIFFERENT"


def test_verdicts_are_read_from_messy_model_output():
    assert parse_verdict("SAME") is True and parse_verdict("**Different.**") is False
    assert parse_verdict("same question") is True and parse_verdict("Answer: DIFFERENT") is False
    assert parse_verdict("I can't tell") is None and parse_verdict("") is None and parse_verdict("DIFFERENTLY") is None


def test_candidates_are_the_unsettled_band_closest_first_and_capped():
    floor, similar = review_floor(HASH_MODEL), thresholds(HASH_MODEL)[1]
    assert floor == round(similar - 0.12, 3)

    def question(vector, flags=None):
        return Question(id=uuid.uuid4(), text=f"Question {uuid.uuid4().hex[:6]}?", embedding=vector, embedding_model=HASH_MODEL, flags=flags or {})

    base = question(axis(0))
    below = question(towards(0, 1, floor - 0.02))      # too different: not worth asking
    low = question(towards(0, 2, floor + 0.01))
    mid = question(towards(0, 3, (floor + similar) / 2))
    high = question(towards(0, 4, similar - 0.01))
    settled = question(towards(0, 5, similar + 0.02))  # already "similar" by meaning
    flagged = question(towards(0, 6, similar - 0.02), {"match": {"kind": "similar"}})
    questions = [base, below, low, mid, high, settled, flagged]

    pairs, total = review_candidates(questions, HASH_MODEL, {}, limit=2)
    assert total == 3 and [p.later for p in pairs] == [high, mid] and all(p.earlier is base for p in pairs)

    lo, hi, fingerprint = pair_key(high.id, high.text, base.id, base.text)
    pairs, total = review_candidates(questions, HASH_MODEL, {(lo, hi): (fingerprint, False)}, limit=20)
    assert total == 2 and high not in [p.later for p in pairs], "a judged pair is not asked about again"
    pairs, total = review_candidates(questions, HASH_MODEL, {(lo, hi): ("an older wording", False)}, limit=20)
    assert total == 3, "a verdict about different wording doesn't count"


async def test_ai_confirms_a_reworded_repeat_and_the_verdict_is_remembered(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    q1 = await add_question(client, me["csrf"], podcast_id, ORIGIN)
    q2 = await add_question(client, me["csrf"], podcast_id, REWORDED)
    q3 = await add_question(client, me["csrf"], podcast_id, OTHER)
    floor, similar = review_floor(HASH_MODEL), thresholds(HASH_MODEL)[1]
    await set_vectors({q1["id"]: axis(0), q2["id"]: towards(0, 1, similar - 0.03), q3["id"]: towards(0, 2, floor + 0.03)})

    first = await check(client, me, podcast_id)
    assert first["summary"]["to_review"] == 2 and first["summary"]["similar"] == 0

    judge = Judge("cold storage")
    ai.use(judge)
    job = await review(client, me, podcast_id, ai)
    assert job["status"] == "succeeded" and job["result"] == {"checked": 2, "repeats": 1, "unanswered": 0}
    system, messages = judge.calls[0]
    assert system == JUDGE_SYSTEM_PROMPT and "<pairs>" in messages[0].content
    assert REWORDED in messages[0].content and OTHER not in messages[0].content, "one pair per call, closest first"
    assert set(judge.temperatures) == {0.0}, "judged at the temperature it was measured at"
    asked = len(judge.calls)

    flags = {q["id"]: q["flags"] for q in ai.events("job:flags")[-1][0]["questions"]}
    assert flags[q2["id"]]["match"]["method"] == "ai" and flags[q2["id"]]["match"]["question_id"] == q1["id"]
    assert flags[q2["id"]]["match"]["kind"] == "similar"
    assert "match" not in flags[q3["id"]] and "match" not in flags[q1["id"]]

    second = await check(client, me, podcast_id)  # instant check re-applies the verdicts, asks nothing new
    assert second["summary"]["to_review"] == 0 and second["summary"]["similar"] == 1
    assert len(judge.calls) == asked

    edited = await client.patch(f"/api/questions/{q2['id']}", json={"text": "Why did you get into cold storage at all, honestly?"}, headers=headers(me["csrf"]))
    assert edited.status_code == 200 and "match" not in edited.json()["question"]["flags"], "an edit retires the verdict"
    await set_vectors({q2["id"]: towards(0, 1, similar - 0.03)})
    third = await check(client, me, podcast_id)
    assert third["summary"]["to_review"] == 1, "only the edited pair needs a new verdict"


async def test_a_verdict_about_text_edited_meanwhile_is_discarded(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    q1 = await add_question(client, me["csrf"], podcast_id, ORIGIN)
    q2 = await add_question(client, me["csrf"], podcast_id, REWORDED)
    q3 = await add_question(client, me["csrf"], podcast_id, OTHER)
    similar = thresholds(HASH_MODEL)[1]
    await set_vectors({q1["id"]: axis(0), q2["id"]: towards(0, 1, similar - 0.03), q3["id"]: towards(0, 2, similar - 0.05)})

    class EditsWhileThinking(Judge):
        async def stream(self, system, messages, *, max_tokens, temperature):  # noqa: ANN001
            if not self.calls:  # while the first pair (q2 vs q1) is being judged, q2 gets edited
                new_text = "Why did you get into cold storage, really?"
                async with session_scope() as db:
                    await db.execute(update(Question).where(Question.id == uuid.UUID(q2["id"])).values(text=new_text, text_hash=text_hash(new_text)))
                    await db.commit()
            async for chunk in super().stream(system, messages, max_tokens=max_tokens, temperature=temperature):
                yield chunk

    ai.use(EditsWhileThinking("cold"))
    job = await review(client, me, podcast_id, ai)
    assert job["result"]["checked"] == 1, "only the untouched pair is recorded"
    async with session_scope() as db:
        assert await db.scalar(select(func.count(QuestionReview.id)).where(QuestionReview.podcast_id == uuid.UUID(podcast_id))) == 1
    state = {q["id"]: q for q in (await client.get(f"/api/podcasts/{podcast_id}")).json()["questions"]}
    assert "match" not in state[q2["id"]]["flags"]
    assert state[q3["id"]]["flags"]["match"]["method"] == "ai"


async def test_nothing_borderline_means_no_ai_call(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    q1 = await add_question(client, me["csrf"], podcast_id, ORIGIN)
    q2 = await add_question(client, me["csrf"], podcast_id, OTHER)
    await set_vectors({q1["id"]: axis(0), q2["id"]: axis(1)})
    assert (await check(client, me, podcast_id))["summary"]["to_review"] == 0
    judge = Judge("cold")
    ai.use(judge)
    job = await review(client, me, podcast_id, ai)
    assert job["status"] == "succeeded" and job["result"] == {"checked": 0, "repeats": 0}
    assert judge.calls == []


async def test_another_account_cannot_start_a_repeat_check(client, other_client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    intruder = await signup(other_client, "intruder")
    response = await other_client.post(f"/api/podcasts/{podcast_id}/ai/review-repeats", json={}, headers=headers(intruder["csrf"], idem=True))
    assert response.status_code == 404
