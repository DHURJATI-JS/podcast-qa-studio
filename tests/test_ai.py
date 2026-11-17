"""AI correctness: fallback chain, "none available" messages, streaming jobs, redundancy skipping,
memory of what the user said, rewrites + undo, feedback parsing, follow-ups, double-submit/spam guards."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ai.providers import Message
from app.ai.router import AIRouter, NoAIAvailable
from app.db import session_scope
from app.jobs import queue
from app.models import AIJob
from app.redis_client import get_redis
from tests.conftest import FakeProvider, add_question, create_podcast, headers, lines, signup

FEEDBACK = (
    "SCORE: 7/10\nWORKED:\n- Clear opening hook\n- Honest tone\nIMPROVE:\n- Add one number\n- Shorten the middle\n- End with a takeaway\n"
    "STRONGER ANSWER:\nI started after watching farmers lose a season of crops to a heatwave.\n"
    "TIMING: About right for the slot.\nFOLLOW-UP: What did the first farmer say when you showed them?\n"
)


async def build(system="sys", text="hi", tokens=50):
    async def _build():
        return system, [Message("user", text)], tokens

    return _build


async def test_router_falls_back_before_and_during_streaming():
    redis = get_redis()
    dead = FakeProvider("gateway", fail_before=True)
    flaky = FakeProvider("local", ["partial ", "answer ", "lost"], fail_after=2)
    good = FakeProvider("anthropic", ["final ", "answer"])
    router = AIRouter([dead, flaky, good], redis)
    resets, tokens = [], []

    async def on_start(p):
        pass

    async def on_token(t):
        tokens.append(t)

    async def on_reset(p, reason):
        resets.append(p.name)
        tokens.clear()

    result = await router.run(await build(), on_start=on_start, on_token=on_token, on_reset=on_reset)
    assert result.provider is good and result.text == "final answer"
    assert resets == ["local"] and "".join(tokens) == "final answer"
    # Failed providers are skipped for a while (circuit breaker) and reported as such.
    statuses = {s["name"]: s["status"] for s in await router.statuses()}
    assert statuses["anthropic"] == "unknown"


async def test_router_reports_every_reason_when_nothing_is_available():
    router = AIRouter(
        [
            FakeProvider("gateway", fail_before=True),
            FakeProvider("local", configured=False),
            FakeProvider("anthropic", configured=False),
            FakeProvider("openai", []),  # returns nothing at all
        ],
        get_redis(),
    )

    async def noop(*_):
        pass

    with pytest.raises(NoAIAvailable) as info:
        await router.run(await build(), on_start=noop, on_token=noop, on_reset=noop)
    message = str(info.value)
    assert message.startswith("No AI is available right now.")
    for fragment in ("Fake gateway: connection refused", "Fake local: not configured", "Fake anthropic: not configured", "Fake openai: returned an empty reply"):
        assert fragment in message


async def test_generate_job_streams_saves_and_skips_repeats(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(
        client, me["csrf"], guest_name="Asha", guest_bio="Founded a solar cold-storage company in Pune", instructions="Avoid politics\nKeep it under 25 words"
    )
    await add_question(client, me["csrf"], podcast_id, "What first pulled you toward climate technology?")
    reply = lines(
        "Here are your questions:",
        "1. [opener|90] What first pulled you toward climate technology?",  # exact repeat -> skipped
        "2. **[story|180] Tell us about the night your first cold room failed.**",
        "3) [Deep Dive | 3] How do you price storage for small farmers?",  # minutes -> 180s, alias category
        "4. [closing|60] What would you tell your younger self?",
    )
    fake = FakeProvider("gateway", reply)
    ai.use(fake)
    response = await client.post(
        f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 3, "focus": "the early days"}, headers=headers(me["csrf"], idem=True)
    )
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    await ai.run(job_id)

    prompt = fake.calls[0][1][0].content
    for remembered in ("Avoid politics", "Keep it under 25 words", "Asha", "solar cold-storage", "the early days", "What first pulled you toward climate technology?"):
        assert remembered in prompt, remembered
    assert "minutes" in prompt and "Length: 30 min target" in prompt

    state = (await client.get(f"/api/podcasts/{podcast_id}")).json()
    texts = [q["text"] for q in state["questions"]]
    assert texts == [
        "What first pulled you toward climate technology?",
        "Tell us about the night your first cold room failed.",
        "How do you price storage for small farmers?",
        "What would you tell your younger self?",
    ]
    assert state["questions"][2]["category"] == "core" and state["questions"][2]["est_seconds"] == 180
    assert len(ai.events("job:question")) == 3
    assert ai.events("job:skipped")[0][0]["text"] == "What first pulled you toward climate technology?"
    assert ai.events("job:delta"), "tokens are streamed"
    done = ai.events("job:done")[0][0]
    assert done["result"]["added"] == 3 and done["provider"] == "Fake gateway"
    async with session_scope() as db:
        job = await db.get(AIJob, __import__("uuid").UUID(job_id))
        assert job.status == "succeeded"
    assert not await get_redis().exists(f"pq:lock:ai:q:{podcast_id}"), "lock released"


async def test_generate_respects_question_cap(client, ai, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_questions_per_podcast", 2)
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", lines("1. [core|120] How did the company begin?", "2. [story|180] Tell us about your hardest week.", "3. [core|120] Where is the storage market heading?", "4. [closing|60] What advice would you give listeners?", "5. [challenge|120] Which critic changed your mind?")))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 5}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    assert len((await client.get(f"/api/podcasts/{podcast_id}")).json()["questions"]) == 2


async def test_garbage_ai_output_fails_gracefully(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", ["I'm sorry, ", "I can't help with that."]))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 2}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    error = ai.events("job:error")[0][0]
    assert "expected format" in error["message"]
    assert not await get_redis().exists(f"pq:lock:ai:q:{podcast_id}")


async def test_no_ai_available_is_reported_clearly(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", configured=False), FakeProvider("local", health="offline"))
    await ai.router.check_all(force=True)
    response = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 2}, headers=headers(me["csrf"], idem=True))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "No AI is available right now" in detail and "Fake gateway: not configured" in detail and "Fake local: offline" in detail
    # Without a live worker the user is told that instead.
    ai.use(FakeProvider("gateway", lines("1. [core|60] Fine question here?")))
    await get_redis().delete(queue.WORKERS_KEY)
    response = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 2}, headers=headers(me["csrf"], idem=True))
    assert response.status_code == 503 and "worker isn't running" in response.json()["detail"]


async def test_double_submit_and_busy_locks(client, ai, settings, monkeypatch):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", lines("1. [core|60] Fine question here?")))
    key = {"X-CSRF-Token": me["csrf"], "Idempotency-Key": "same-key-123456"}
    first = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=key)
    second = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=key)
    assert first.status_code == 202 and second.status_code == 409
    busy = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))
    assert busy.status_code == 409 and busy.json()["code"] == "busy"
    # Cancelling a queued job releases the lock.
    job_id = first.json()["job"]["id"]
    cancel = await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=headers(me["csrf"]))
    assert cancel.json()["status"] == "cancelled"
    again = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))
    assert again.status_code == 202
    # Per-user cap on concurrent AI tasks.
    monkeypatch.setattr(settings, "max_ai_jobs_per_user", 1)
    other = await create_podcast(client, me["csrf"], title="Second")
    capped = await client.post(f"/api/podcasts/{other}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))
    assert capped.status_code == 409 and "AI tasks running" in capped.json()["detail"]


async def test_ai_rate_limit(client, ai, settings, monkeypatch):
    monkeypatch.setattr(settings, "rl_ai_hour", "2/3600")
    me = await signup(client)
    ai.use(FakeProvider("gateway", lines("1. [core|60] Fine question here?")))
    codes = []
    for i in range(3):
        podcast_id = await create_podcast(client, me["csrf"], title=f"Show {i}")
        codes.append((await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))).status_code)
    assert codes == [202, 202, 429]


async def test_rewrite_remembers_instruction_and_undo_restores(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    q1 = await add_question(client, me["csrf"], podcast_id, "Can you describe in detail the full history of your company from the start?")
    q2 = await add_question(client, me["csrf"], podcast_id, "What is the hardest lesson you learned in your first year?")
    fake = FakeProvider("gateway", lines("Q1 [background|90] How did your company begin?", "Q2: [story|120] What was your hardest first-year lesson?"))
    ai.use(fake)
    response = await client.post(
        f"/api/podcasts/{podcast_id}/ai/rewrite",
        json={"question_ids": [q1["id"], q2["id"]], "instruction": "make them shorter", "remember": True},
        headers=headers(me["csrf"], idem=True),
    )
    assert response.status_code == 202
    assert response.json()["standing_instructions"] == ["make them shorter"]
    await ai.run(response.json()["job"]["id"])
    state = (await client.get(f"/api/podcasts/{podcast_id}")).json()
    assert [q["text"] for q in state["questions"]] == ["How did your company begin?", "What was your hardest first-year lesson?"]
    assert "make them shorter" in fake.calls[0][1][0].content
    undo = await client.post(f"/api/questions/{q1['id']}/undo", json={}, headers=headers(me["csrf"]))
    assert undo.json()["question"]["text"] == q1["text"]
    nothing = await client.post(f"/api/questions/{q1['id']}/undo", json={}, headers=headers(me["csrf"]))
    assert nothing.status_code == 409
    # Standing instructions are used by every later AI feature.
    ai.use(fake2 := FakeProvider("gateway", lines("1. [core|60] A completely new angle on storage?")))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    assert "make them shorter" in fake2.calls[0][1][0].content


async def test_feedback_job_scores_and_detects_repeated_story(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest", talking_points="Cold storage cuts crop loss by 30%")
    qa = await add_question(client, me["csrf"], podcast_id, "Why did you start your company?")
    qb = await add_question(client, me["csrf"], podcast_id, "What was your turning point?")
    story = "I started after watching farmers near Pune lose an entire season of tomatoes to a heatwave, and I knew cold storage could fix it."
    first = await client.post(f"/api/questions/{qa['id']}/answers", json={"text": story, "feedback": False}, headers=headers(me["csrf"], idem=True))
    assert first.status_code == 201 and first.json()["job"] is None
    # Embed the first answer by running a feedback job on it.
    fake = FakeProvider("gateway", [FEEDBACK])
    ai.use(fake)
    reviewed = await client.post(f"/api/questions/{qa['id']}/answers", json={"text": story + " Truly.", "feedback": True}, headers=headers(me["csrf"], idem=True))
    await ai.run(reviewed.json()["job"]["id"])
    feedback = ai.events("job:feedback")[0][0]["answer"]
    assert feedback["score"] == 7 and feedback["status"] == "reviewed"
    assert feedback["feedback"]["improve"] == ["Add one number", "Shorten the middle", "End with a takeaway"]
    assert feedback["feedback"]["follow_up"] == "What did the first farmer say when you showed them?"
    assert "Cold storage cuts crop loss by 30%" in fake.calls[0][1][0].content
    # Telling the same story for another question is flagged and the earlier answer is used as memory.
    ai.use(fake2 := FakeProvider("gateway", [FEEDBACK]))
    again = await client.post(f"/api/questions/{qb['id']}/answers", json={"text": story, "feedback": True}, headers=headers(me["csrf"], idem=True))
    await ai.run(again.json()["job"]["id"])
    answer = ai.events("job:feedback")[1][0]["answer"]
    assert answer["redundant_with"]["question"] == "Why did you start your company?"
    prompt = fake2.calls[0][1][0].content
    assert "<memory>" in prompt and "repeats material" in prompt
    # The exact same answer can't be sent for feedback again (spam protection).
    dup = await client.post(f"/api/questions/{qb['id']}/answers", json={"text": story, "feedback": True}, headers=headers(me["csrf"], idem=True))
    assert dup.status_code == 409


async def test_spammy_answers_never_reach_the_ai(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest")
    q = await add_question(client, me["csrf"], podcast_id, "Why did you start your company?")
    fake = FakeProvider("gateway", [FEEDBACK])
    ai.use(fake)
    for junk in ("ok", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "lol " * 40):
        response = await client.post(f"/api/questions/{q['id']}/answers", json={"text": junk, "feedback": True}, headers=headers(me["csrf"], idem=True))
        assert response.status_code == 422
    assert not fake.calls


async def test_follow_up_question_is_inserted_after_the_answered_one(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest")
    q1 = await add_question(client, me["csrf"], podcast_id, "Why did you start your company?")
    await add_question(client, me["csrf"], podcast_id, "Where do you see the industry in ten years?")
    answer = (await client.post(f"/api/questions/{q1['id']}/answers", json={"text": "Because farmers kept losing crops to heat every summer.", "feedback": False}, headers=headers(me["csrf"], idem=True))).json()["answer"]
    ai.use(FakeProvider("gateway", ["[follow_up|120] What did the first farmer say when you showed them the prototype?"]))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/next", json={"answer_id": answer["id"]}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    event = ai.events("job:question")[0][0]
    assert event["ask"] is True and event["question"]["source"] == "ai_followup"
    texts = [q["text"] for q in (await client.get(f"/api/podcasts/{podcast_id}")).json()["questions"]]
    assert texts[1] == "What did the first farmer say when you showed them the prototype?"


class Replies(FakeProvider):
    """Gives a different scripted reply on each call (the last one repeats)."""

    def __init__(self, *replies: list[str]) -> None:
        super().__init__("gateway")
        self.replies = list(replies)

    async def stream(self, system, messages, *, max_tokens, temperature):  # noqa: ANN001
        self.calls.append((system, messages))
        for chunk in self.replies[min(len(self.calls), len(self.replies)) - 1]:
            yield chunk


async def test_follow_up_that_repeats_a_question_is_retried_once_with_the_reason(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest")
    await add_question(client, me["csrf"], podcast_id, "Why did you start your company?")
    fake = Replies(lines("[core|90] Why did you start your company?"), lines("[core|90] Which customer taught you the most, and how?"))
    ai.use(fake)
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/next", json={}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    assert len(fake.calls) == 2, "exactly one retry"
    retry_prompt = fake.calls[1][1][0].content
    assert "<rejected>" in retry_prompt and "Why did you start your company?" in retry_prompt.split("<rejected>")[1]
    assert "already have" in ai.events("job:reset")[0][0]["reason"], "the browser is told why the text was cleared"
    texts = [q["text"] for q in (await client.get(f"/api/podcasts/{podcast_id}")).json()["questions"]]
    assert texts == ["Why did you start your company?", "Which customer taught you the most, and how?"]
    assert (await client.get(f"/api/jobs/{job['id']}")).json()["job"]["status"] == "succeeded"


async def test_follow_up_that_repeats_twice_fails_with_a_clear_message(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest")
    await add_question(client, me["csrf"], podcast_id, "Why did you start your company?")
    fake = Replies(lines("[core|90] Why did you start your company?"))
    ai.use(fake)
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/next", json={}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    assert len(fake.calls) == 2, "no endless retrying"
    error = ai.events("job:error")[0][0]
    assert "already have" in error["message"]
    texts = [q["text"] for q in (await client.get(f"/api/podcasts/{podcast_id}")).json()["questions"]]
    assert texts == ["Why did you start your company?"], "the repeat was not added"


async def test_cancel_running_job(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", ["1. [core|60] First fine question here?\n"] + ["x"] * 50))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 2}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await get_redis().set(f"pq:job:{job['id']}:cancel", "1")
    await ai.run(job["id"])
    assert ai.events("job:error")[0][0]["status"] == "cancelled"
    async with session_scope() as db:
        row = (await db.execute(select(AIJob.status))).scalar_one()
        assert row == "cancelled"


async def test_router_last_resort_tries_providers_marked_offline():
    """A stale "offline" health check must not stop a recovered provider from answering."""
    import json
    import time

    from app.ai.router import HEALTH_KEY

    redis = get_redis()
    recovered = FakeProvider("gateway", ["back ", "online"])
    down = FakeProvider("local", fail_before=True)
    router = AIRouter([recovered, down], redis)
    await redis.hset(HEALTH_KEY, "gateway", json.dumps({"name": "gateway", "status": "offline", "detail": "ConnectError", "checked_at": time.time()}))

    async def noop(*_):
        pass

    result = await router.run(await build(), on_start=noop, on_token=noop, on_reset=noop)
    assert result.provider is recovered and result.text == "back online"
    assert len(down.calls) == 1 and len(recovered.calls) == 1, "healthy providers are tried first"
