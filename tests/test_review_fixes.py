"""Regression tests for bugs found by the independent code review (each failed before its fix)."""

from __future__ import annotations

import asyncio
import re
import uuid

from sqlalchemy import update

from app.db import session_scope
from app.jobs.service import cancel_job
from app.models import AIJob
from app.redis_client import get_redis
from tests.conftest import FakeProvider, add_question, create_podcast, headers, lines, signup


async def test_invalid_settings_form_rerenders_instead_of_crashing(client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    response = await client.post(
        f"/podcasts/{podcast_id}/settings", data={"csrf_token": me["csrf"], "title": "", "theme": "Still here", "target_minutes": "300"}
    )
    assert response.status_code == 422
    assert "This field is required." in response.text and "5-240 minutes" in response.text
    assert f"/podcasts/{podcast_id}/delete" in response.text


async def test_answer_submit_at_ai_task_cap_saves_instead_of_crashing(client, ai, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_ai_jobs_per_user", 1)
    me = await signup(client)
    host = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", lines("1. [core|60] A perfectly fine question here?")))
    busy = await client.post(f"/api/podcasts/{host}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))
    assert busy.status_code == 202  # uses the only AI slot
    guest = await create_podcast(client, me["csrf"], mode="guest", title="Guest")
    question = await add_question(client, me["csrf"], guest, "Why did you start your company?")
    response = await client.post(
        f"/api/questions/{question['id']}/answers",
        json={"text": "Because farmers near Pune kept losing their crops every summer.", "feedback": True},
        headers=headers(me["csrf"], idem=True),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["job"] is None and "AI tasks running" in body["job_error"]
    assert body["answer"]["status"] == "saved"
    stored = (await client.get(f"/api/questions/{question['id']}/answers")).json()["answers"][0]
    assert stored["status"] == "saved"


async def test_account_password_checks_are_rate_limited(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "rl_account_password", "3/900")
    me = await signup(client)
    attempt = {"csrf_token": me["csrf"], "current_password": "wrong-guess-123", "new_password": "x", "confirm_password": "x"}
    for _ in range(4):
        assert (await client.post("/account/password", data=attempt)).status_code == 303
    assert "Too many password attempts" in (await client.get("/account")).text
    deletion = await client.post("/account/delete", data={"csrf_token": me["csrf"], "confirm": "DELETE", "password": "guess"})
    assert deletion.status_code == 303
    assert "Too many password attempts" in (await client.get("/account")).text


async def test_cancel_tells_the_truth_when_the_worker_already_started(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    ai.use(FakeProvider("gateway", lines("1. [core|60] A perfectly fine question here?")))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))).json()["job"]
    async with session_scope() as db:
        stale = await db.get(AIJob, uuid.UUID(job["id"]))  # loaded as "queued"
        async with session_scope() as worker:  # meanwhile the worker picks it up
            await worker.execute(update(AIJob).where(AIJob.id == stale.id).values(status="running"))
            await worker.commit()
        status = await cancel_job(db=db, redis=get_redis(), emitter=ai.emitter, user_id=stale.user_id, job_id=job["id"])
    assert status == "cancelling"
    assert await get_redis().exists(f"pq:job:{job['id']}:cancel"), "the running job must be told to stop"


async def test_duplicate_podcast_submit_in_flight_goes_to_dashboard(client):
    me = await signup(client)
    page = await client.get("/podcasts/new")
    idem = re.search(r'name="idem" value="([^"]+)"', page.text).group(1)
    user_id = (await client.get("/api/me")).json()["id"]
    await get_redis().set(f"pq:idem:form:{user_id}:{idem}", "1", ex=60)  # first submit still being processed
    response = await client.post(
        "/podcasts/new", data={"csrf_token": me["csrf"], "mode": "host", "idem": idem, "title": "Twice", "theme": "Double click"}
    )
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


async def test_voice_cache_is_per_user(client, other_client, app_ready, monkeypatch):
    alice = await signup(client)
    bob = await signup(other_client)
    calls = []

    async def fake_synthesize(text, voice, speed):
        calls.append(text)
        return b"ID3audio"

    monkeypatch.setattr(app_ready.state.kokoro, "synthesize", fake_synthesize)
    body = {"text": "A very private sentence only Alice wrote", "voice": "af_bella", "speed": 1.0}
    assert (await client.post("/api/tts", json=body, headers=headers(alice["csrf"]))).status_code == 200
    probe = await other_client.post("/api/tts", json=body, headers=headers(bob["csrf"]))
    assert probe.status_code == 200 and probe.headers.get("x-tts-cache") is None, "Bob must not hit Alice's cache"
    assert len(calls) == 2
    again = await client.post("/api/tts", json=body, headers=headers(alice["csrf"]))
    assert again.headers.get("x-tts-cache") == "hit" and len(calls) == 2


async def test_feedback_timeout_marks_the_answer_failed(client, ai, settings, monkeypatch):
    monkeypatch.setattr(settings, "ai_job_timeout", 0.5)

    class Stuck(FakeProvider):
        async def stream(self, system, messages, *, max_tokens, temperature):  # noqa: ANN001
            await asyncio.sleep(10)
            yield "never"

    me = await signup(client)
    guest = await create_podcast(client, me["csrf"], mode="guest")
    question = await add_question(client, me["csrf"], guest, "Why did you start your company?")
    ai.use(Stuck("gateway"))
    response = await client.post(
        f"/api/questions/{question['id']}/answers",
        json={"text": "Because farmers near Pune kept losing their crops every summer.", "feedback": True},
        headers=headers(me["csrf"], idem=True),
    )
    await ai.run(response.json()["job"]["id"])
    assert "took too long" in ai.events("job:error")[0][0]["message"]
    stored = (await client.get(f"/api/questions/{question['id']}/answers")).json()["answers"][0]
    assert stored["status"] == "failed", "a timed-out review must not stay 'reviewing' forever"
