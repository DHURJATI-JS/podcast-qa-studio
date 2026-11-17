"""Podcasts, questions, caps under concurrency, run-time budget, PDF export (stored in PostgreSQL), voice."""

from __future__ import annotations

import asyncio
import re
import uuid

from sqlalchemy import func, select

from app.db import session_scope
from app.models import Export, Podcast, Question
from tests.conftest import add_question, create_podcast, headers, signup


async def test_create_podcast_double_submit_creates_one(client):
    me = await signup(client)
    page = await client.get("/podcasts/new?mode=guest")
    idem = re.search(r'name="idem" value="([^"]+)"', page.text).group(1)
    data = {"csrf_token": me["csrf"], "mode": "guest", "idem": idem, "title": "Twice", "theme": "Clicking twice", "target_minutes": "20"}
    first, second = await asyncio.gather(client.post("/podcasts/new", data=data), client.post("/podcasts/new", data=data))
    assert first.status_code == second.status_code == 303
    async with session_scope() as db:
        assert await db.scalar(select(func.count(Podcast.id)).where(Podcast.title == "Twice")) == 1


async def test_podcast_form_validation(client):
    me = await signup(client)
    page = await client.get("/podcasts/new")
    idem = re.search(r'name="idem" value="([^"]+)"', page.text).group(1)
    response = await client.post(
        "/podcasts/new", data={"csrf_token": me["csrf"], "mode": "host", "idem": idem, "title": "", "theme": "x" * 400, "target_minutes": "999"}
    )
    assert response.status_code == 422
    assert "This field is required." in response.text and "under 300 characters" in response.text and "5-240 minutes" in response.text


async def test_podcast_cap_holds_under_concurrency(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_podcasts_per_user", 3)
    me = await signup(client)

    async def make(i):
        page = await client.get("/podcasts/new")
        idem = re.search(r'name="idem" value="([^"]+)"', page.text).group(1)
        return await client.post("/podcasts/new", data={"csrf_token": me["csrf"], "mode": "host", "idem": idem, "title": f"P{i}", "theme": "Race"})

    results = await asyncio.gather(*(make(i) for i in range(8)))
    assert sum(r.status_code == 303 for r in results) == 3
    assert sum(r.status_code == 409 for r in results) == 5
    async with session_scope() as db:
        assert await db.scalar(select(func.count(Podcast.id))) == 3


async def test_question_cap_holds_under_concurrency(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_questions_per_podcast", 4)
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    results = await asyncio.gather(
        *(
            client.post(f"/api/podcasts/{podcast_id}/questions", json={"text": f"Distinct question number {i} about subject {i * 13}?"}, headers=headers(me["csrf"], idem=True))
            for i in range(12)
        )
    )
    assert sum(r.status_code == 201 for r in results) == 4
    async with session_scope() as db:
        count = await db.scalar(select(func.count(Question.id)).where(Question.podcast_id == uuid.UUID(podcast_id)))
        positions = (await db.execute(select(Question.position).where(Question.podcast_id == uuid.UUID(podcast_id)).order_by(Question.position))).scalars().all()
    assert count == 4 and positions == [1, 2, 3, 4]


async def test_quality_flags_redundancy_and_plan(client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], target_minutes=10)
    await add_question(client, me["csrf"], podcast_id, "What first pulled you toward climate technology?", est_seconds=200)
    closed = await add_question(client, me["csrf"], podcast_id, "Do you like your job?", est_seconds=100)
    double = await add_question(client, me["csrf"], podcast_id, "Why did you start? And what would you change?", est_seconds=100)
    leading = await add_question(client, me["csrf"], podcast_id, "Don't you think climate founders are underrated by investors?")
    repeat = await add_question(client, me["csrf"], podcast_id, "What first pulled you toward climate technology??")
    assert "closed" in closed["flags"]["quality"]
    assert "double" in double["flags"]["quality"]
    assert "leading" in leading["flags"]["quality"]
    assert repeat["flags"]["match"]["kind"] == "duplicate"
    polite = await add_question(client, me["csrf"], podcast_id, "Could you walk us through your first prototype?")
    assert "closed" not in polite["flags"]["quality"]
    check = (await client.post(f"/api/podcasts/{podcast_id}/quality-check", json={}, headers=headers(me["csrf"]))).json()
    assert check["summary"]["duplicates"] >= 1 and check["summary"]["quality"] >= 3
    plan = check["plan"]
    assert plan["target_seconds"] == 600 and plan["intro_seconds"] == 60
    assert plan["planned_seconds"] == 60 + 200 + 100 + 100 + 120 + 120 + 120 + 60 and plan["status"] == "over"


async def test_edit_reorder_delete_and_renumber(client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    a = await add_question(client, me["csrf"], podcast_id, "First question about the origin story?")
    b = await add_question(client, me["csrf"], podcast_id, "Second question about the hardest year?")
    c = await add_question(client, me["csrf"], podcast_id, "Third question about the future of storage?")
    edited = await client.patch(f"/api/questions/{b['id']}", json={"text": "Second question, reworded: the hardest year?", "est_seconds": 300, "category": "story"}, headers=headers(me["csrf"]))
    assert edited.status_code == 200 and edited.json()["question"]["category"] == "story"
    bad = await client.patch(f"/api/questions/{b['id']}", json={"category": "nonsense"}, headers=headers(me["csrf"]))
    assert bad.status_code == 422
    order = await client.post(f"/api/podcasts/{podcast_id}/questions/reorder", json={"ids": [c["id"], a["id"], b["id"]]}, headers=headers(me["csrf"]))
    assert [q["id"] for q in order.json()["questions"]] == [c["id"], a["id"], b["id"]]
    stale = await client.post(f"/api/podcasts/{podcast_id}/questions/reorder", json={"ids": [c["id"], a["id"]]}, headers=headers(me["csrf"]))
    assert stale.status_code == 409
    deleted = await client.delete(f"/api/questions/{a['id']}", headers=headers(me["csrf"]))
    assert [q["position"] for q in deleted.json()["questions"]] == [1, 2]
    undo = await client.post(f"/api/questions/{b['id']}/undo", json={}, headers=headers(me["csrf"]))
    assert undo.json()["question"]["text"] == "Second question about the hardest year?"


async def test_standing_instructions_are_cleaned_and_capped(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_instructions_per_podcast", 3)
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    response = await client.patch(
        f"/api/podcasts/{podcast_id}",
        json={"standing_instructions": ["<b>Avoid politics</b>", "avoid politics", " - Keep it short ", ""]},
        headers=headers(me["csrf"]),
    )
    assert response.json()["podcast"]["standing_instructions"] == ["Avoid politics", "Keep it short"]
    too_many = await client.patch(f"/api/podcasts/{podcast_id}", json={"standing_instructions": ["a b", "c d", "e f", "g h"]}, headers=headers(me["csrf"]))
    assert too_many.status_code == 422


async def test_pdf_export_is_stored_in_postgres_and_downloadable(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_exports_per_podcast", 2)
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], mode="guest", title="Ünïcødé <Show> & Co", talking_points="Point one\nPoint two")
    q = await add_question(client, me["csrf"], podcast_id, "What does 'success' mean to you — really?")
    await client.post(f"/api/questions/{q['id']}/answers", json={"text": "Success means farmers trust us with their harvest.", "feedback": False}, headers=headers(me["csrf"], idem=True))
    urls = []
    for _ in range(3):
        response = await client.post(f"/api/podcasts/{podcast_id}/export", json={}, headers=headers(me["csrf"]))
        assert response.status_code == 201, response.text
        urls.append(response.json()["export"]["url"])
    download = await client.get(urls[-1])
    assert download.status_code == 200 and download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF") and len(download.content) > 1500
    assert "attachment" in download.headers["content-disposition"]
    async with session_scope() as db:
        assert await db.scalar(select(func.count(Export.id))) == 2, "older exports are pruned"
    assert (await client.get(urls[0])).status_code == 404


async def test_tts_falls_back_to_browser_when_kokoro_is_off(client):
    me = await signup(client)
    response = await client.post("/api/tts", json={"text": "Hello there"}, headers=headers(me["csrf"]))
    assert response.status_code == 503 and response.json()["fallback"] == "browser"


async def test_tts_proxies_and_caches_kokoro_audio(client, app_ready, monkeypatch):
    me = await signup(client)
    calls = []

    async def fake_synthesize(text, voice, speed):
        calls.append((text, voice, speed))
        return b"ID3fake-mp3-bytes"

    monkeypatch.setattr(app_ready.state.kokoro, "synthesize", fake_synthesize)
    body = {"text": "<b>Hi</b> I am Mira <script>x()</script>", "voice": "af_bella", "speed": 1.0}
    first = await client.post("/api/tts", json=body, headers=headers(me["csrf"]))
    second = await client.post("/api/tts", json=body, headers=headers(me["csrf"]))
    assert first.status_code == second.status_code == 200 and first.content == b"ID3fake-mp3-bytes"
    assert second.headers.get("x-tts-cache") == "hit" and len(calls) == 1
    assert calls[0] == ("Hi I am Mira", "af_bella", 1.0)
    unknown_voice = await client.post("/api/tts", json={"text": "hello", "voice": "evil_voice"}, headers=headers(me["csrf"]))
    assert unknown_voice.status_code == 200 and calls[-1][1] == "mira_soft"


async def test_pages_render_for_signed_in_user(client):
    me = await signup(client)
    host = await create_podcast(client, me["csrf"], mode="host")
    guest = await create_podcast(client, me["csrf"], mode="guest", title="Guest prep")
    for url in ("/", "/legal", "/dashboard", "/dashboard?q=climate&mode=host", "/account", f"/podcasts/{host}", f"/podcasts/{guest}", f"/podcasts/{host}/settings", "/podcasts/new?mode=guest"):
        response = await client.get(url)
        assert response.status_code == 200, url
    assert "Practice interview" in (await client.get(f"/podcasts/{guest}")).text
    assert "Rewrite selected" in (await client.get(f"/podcasts/{host}")).text
    assert (await client.get("/does-not-exist")).status_code == 404


async def test_anonymous_users_are_redirected_to_login(client):
    response = await client.get("/dashboard")
    assert response.status_code == 303 and response.headers["location"].startswith("/login?next=")
    api = await client.get("/api/me")
    assert api.status_code == 401
    home = await client.get("/")
    assert home.status_code == 200 and "Problems it solves" in home.text and "accept no liability" in home.text
