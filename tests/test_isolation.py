"""Cross-account isolation: nobody can see or touch another user's data - pages, API, jobs,
exports, AI memory, cross-podcast matching, sockets."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import session_scope
from app.jobs.emitter import user_room
from app.models import Podcast
from app.realtime import create_sio
from tests.conftest import FakeProvider, add_question, create_podcast, headers, lines, signup


async def test_other_user_gets_404_everywhere(client, other_client, ai):
    alice = await signup(client, "alice")
    podcast_id = await create_podcast(client, alice["csrf"], mode="guest", title="Alice private show")
    question = await add_question(client, alice["csrf"], podcast_id, "What is the secret project you are building?")
    answer = await client.post(
        f"/api/questions/{question['id']}/answers",
        json={"text": "It is a quiet little tool for climate founders.", "feedback": False},
        headers=headers(alice["csrf"], idem=True),
    )
    assert answer.status_code == 201
    export = await client.post(f"/api/podcasts/{podcast_id}/export", json={}, headers=headers(alice["csrf"]))
    assert export.status_code == 201
    export_url = export.json()["export"]["url"]
    ai.use(FakeProvider("gateway", lines("1. [core|120] How did the idea start?")))
    job = await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(alice["csrf"], idem=True))
    assert job.status_code == 202
    job_id = job.json()["job"]["id"]

    bob = await signup(other_client, "bob")
    t = bob["csrf"]
    qid = question["id"]
    checks = [
        ("GET", f"/podcasts/{podcast_id}", None),
        ("GET", f"/podcasts/{podcast_id}/settings", None),
        ("GET", export_url, None),
        ("GET", f"/api/podcasts/{podcast_id}", None),
        ("PATCH", f"/api/podcasts/{podcast_id}", {"target_minutes": 10}),
        ("POST", f"/api/podcasts/{podcast_id}/questions", {"text": "Injected question here?"}),
        ("PATCH", f"/api/questions/{qid}", {"text": "Hijacked question text?"}),
        ("POST", f"/api/questions/{qid}/undo", {}),
        ("POST", f"/api/podcasts/{podcast_id}/questions/reorder", {"ids": [qid]}),
        ("POST", f"/api/podcasts/{podcast_id}/quality-check", {}),
        ("POST", f"/api/podcasts/{podcast_id}/ai/generate", {"count": 2}),
        ("POST", f"/api/podcasts/{podcast_id}/ai/rewrite", {"question_ids": [qid], "instruction": "make it shorter"}),
        ("POST", f"/api/podcasts/{podcast_id}/ai/next", {}),
        ("POST", f"/api/podcasts/{podcast_id}/ai/review-repeats", {}),
        ("POST", f"/api/questions/{qid}/answers", {"text": "Bob answering Alice's question here", "feedback": False}),
        ("GET", f"/api/questions/{qid}/answers", None),
        ("GET", f"/api/jobs/{job_id}", None),
        ("POST", f"/api/jobs/{job_id}/cancel", {}),
        ("POST", f"/api/podcasts/{podcast_id}/export", {}),
        ("DELETE", f"/api/questions/{qid}", None),
    ]
    for method, url, body in checks:
        response = await other_client.request(method, url, json=body, headers=headers(t, idem=True))
        assert response.status_code == 404, f"{method} {url} -> {response.status_code}"
        assert "Alice private show" not in response.text
        assert "secret project" not in response.text

    delete = await other_client.post(f"/podcasts/{podcast_id}/delete", data={"csrf_token": t})
    assert delete.status_code == 404
    dashboard = await other_client.get("/dashboard")
    assert "Alice private show" not in dashboard.text

    # Alice's data is untouched.
    state = (await client.get(f"/api/podcasts/{podcast_id}")).json()
    assert state["podcast"]["target_minutes"] == 30
    assert [q["text"] for q in state["questions"]] == ["What is the secret project you are building?"]
    async with session_scope() as db:
        assert (await db.execute(select(Podcast).where(Podcast.id == uuid.UUID(podcast_id)))).scalar_one()


async def test_malformed_ids_are_404_not_500(client):
    me = await signup(client)
    for url in ("/api/podcasts/not-a-uuid", "/api/jobs/123", "/podcasts/../../etc/passwd", "/api/questions/%00/answers"):
        response = await client.get(url, headers=headers(me["csrf"]))
        assert response.status_code in (404, 422), (url, response.status_code)


async def test_job_events_only_go_to_the_owner_room(client, other_client, ai):
    alice = await signup(client, "alice2")
    await signup(other_client, "bob2")
    podcast_id = await create_podcast(client, alice["csrf"])
    ai.use(FakeProvider("gateway", lines("1. [opener|90] What first pulled you toward climate tech?")))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(alice["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    me = (await client.get("/api/me")).json()
    rooms = {room for _, _, room in ai.emitter.events}
    assert rooms == {user_room(me["id"])}


async def test_ai_memory_and_cross_podcast_matching_never_cross_accounts(client, other_client, ai):
    alice = await signup(client, "alice3")
    bob = await signup(other_client, "bob3")
    text_ = "What first pulled you toward climate technology as a career?"
    bob_podcast = await create_podcast(other_client, bob["csrf"], title="Bob show")
    await add_question(other_client, bob["csrf"], bob_podcast, text_)
    alice_podcast = await create_podcast(client, alice["csrf"], title="Alice show")
    question = await add_question(client, alice["csrf"], alice_podcast, text_)
    assert "asked_before" not in question["flags"], "matched another user's podcast"
    # The same question in Alice's *own* other podcast is flagged.
    alice_second = await create_podcast(client, alice["csrf"], title="Alice second show")
    again = await add_question(client, alice["csrf"], alice_second, text_)
    assert again["flags"]["asked_before"]["podcast_title"] == "Alice show"

    # Answer memory: Bob's answers never reach Alice's feedback prompt.
    guest = await create_podcast(other_client, bob["csrf"], mode="guest", title="Bob guest")
    bq = await add_question(other_client, bob["csrf"], guest, "Tell us about your biggest failure so far?")
    await other_client.post(
        f"/api/questions/{bq['id']}/answers",
        json={"text": "BOBSECRET my biggest failure was a cold storage startup in Pune.", "feedback": False},
        headers=headers(bob["csrf"], idem=True),
    )
    alice_guest = await create_podcast(client, alice["csrf"], mode="guest", title="Alice guest")
    aq = await add_question(client, alice["csrf"], alice_guest, "Tell us about your biggest failure so far?")
    fake = FakeProvider("gateway", ["SCORE: 7\nWORKED:\n- clear\nIMPROVE:\n- add numbers\nSTRONGER ANSWER:\nBetter.\nTIMING: ok\nFOLLOW-UP: What next?\n"])
    ai.use(fake)
    response = await client.post(
        f"/api/questions/{aq['id']}/answers",
        json={"text": "My biggest failure was a cold storage startup that ran out of money.", "feedback": True},
        headers=headers(alice["csrf"], idem=True),
    )
    await ai.run(response.json()["job"]["id"])
    prompt = fake.calls[0][1][0].content
    assert "BOBSECRET" not in prompt


async def test_socket_connect_requires_session_and_csrf(client):
    me = await signup(client, "sockuser")
    sio = create_sio(__import__("app.config", fromlist=["get_settings"]).get_settings())
    connect = sio.handlers["/"]["connect"]
    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    environ = {"HTTP_COOKIE": cookies, "HTTP_HOST": "testserver", "wsgi.url_scheme": "http"}
    import pytest
    from socketio.exceptions import ConnectionRefusedError as Refused

    with pytest.raises(Refused):
        await connect("sid-1", environ, {"csrf": "0" * 64})  # wrong CSRF
    with pytest.raises(Refused):
        await connect("sid-2", {**environ, "HTTP_COOKIE": ""}, {"csrf": me["csrf"]})  # no session
