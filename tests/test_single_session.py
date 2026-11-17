"""One login per account.

A newer sign-in signs every other device out; every request is checked against the key stored in
PostgreSQL; a signed-out device is told why (and when / which device) and nothing it sends changes
any data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select, update
from socketio.exceptions import ConnectionRefusedError as SocketRefused

from app.config import get_settings
from app.db import session_scope
from app.jobs.emitter import RecordingEmitter, user_room
from app.main import asgi_app
from app.models import Podcast, User
from app.realtime import create_sio
from app.redis_client import get_redis
from app.security.sessions import session_cookie_name
from tests.conftest import META_CSRF, PASSWORD, add_question, create_podcast, csrf, headers, signup

CHROME_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


async def login_as(client: httpx.AsyncClient, identifier: str, agent: str | None = None) -> str:
    extra = {"User-Agent": agent} if agent else {}
    page = await client.get("/login?add=1", headers=extra)
    token = META_CSRF.search(page.text).group(1)
    response = await client.post("/login", data={"csrf_token": token, "identifier": identifier, "password": PASSWORD}, headers=extra)
    assert response.status_code == 303, response.text[:300]
    return await csrf(client, "/dashboard")


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver", follow_redirects=False)


async def test_old_device_write_is_refused_with_the_reason_and_changes_nothing(client, other_client, app_ready, monkeypatch):
    recorder = RecordingEmitter()
    monkeypatch.setattr(app_ready.state, "emitter", recorder)
    old = await signup(client, "single1")
    podcast_id = await create_podcast(client, old["csrf"], title="Only mine")
    await add_question(client, old["csrf"], podcast_id, "What first pulled you toward climate tech?")

    await login_as(other_client, "single1", agent=CHROME_ON_WINDOWS)  # the same account, another device
    me = (await other_client.get("/api/me")).json()
    assert ("session:check", {}, user_room(me["id"])) in recorder.events, "old devices are told at once"

    write = await client.post(
        f"/api/podcasts/{podcast_id}/questions", json={"text": "A sneaky late question here?"}, headers=headers(old["csrf"], idem=True)
    )
    assert write.status_code == 401
    body = write.json()
    assert body["code"] == "signed_out" and body["reason"] == "replaced"
    assert "signed in on another device" in body["detail"] and "Chrome on Windows" in body["detail"] and "UTC" in body["detail"]
    state = (await other_client.get(f"/api/podcasts/{podcast_id}")).json()
    assert [q["text"] for q in state["questions"]] == ["What first pulled you toward climate tech?"], "nothing may change"


async def test_old_device_reload_and_form_post_show_why_and_change_nothing(client, other_client):
    old = await signup(client, "single2")
    await create_podcast(client, old["csrf"], title="Before")
    page = await client.get("/podcasts/new")
    idem = page.text.split('name="idem" value="')[1].split('"')[0]
    await login_as(other_client, "single2", agent=CHROME_ON_WINDOWS)

    form = await client.post("/podcasts/new", data={"csrf_token": old["csrf"], "mode": "host", "idem": idem, "title": "After", "theme": "Late"})
    assert form.status_code == 303 and form.headers["location"].startswith("/login")
    async with session_scope() as db:
        assert await db.scalar(select(func.count(Podcast.id)).where(Podcast.title == "After")) == 0
    login_page = await client.get(form.headers["location"])
    assert "signed in on another device (Chrome on Windows" in login_page.text
    assert "Only one device can be signed in at a time" in login_page.text


async def test_every_request_is_checked_against_the_database(client):
    await signup(client, "single3")
    assert (await client.get("/api/me")).status_code == 200
    async with session_scope() as db:  # the key in PostgreSQL changes (e.g. another sign-in)...
        await db.execute(update(User).where(User.username == "single3").values(login_key_hash="0" * 64))
        await db.commit()
    response = await client.get("/api/me")  # ...so the very next request is refused
    assert response.status_code == 401 and response.json()["reason"] == "replaced"


async def test_login_key_matches_the_active_device(client):
    await signup(client, "single4")
    sid = hashlib.sha256(client.cookies.get(session_cookie_name()).encode()).hexdigest()
    async with session_scope() as db:
        assert await db.scalar(select(User.login_key_hash).where(User.username == "single4")) == sid
    token = await csrf(client, "/dashboard")
    await client.post("/logout", data={"csrf_token": token})
    async with session_scope() as db:
        assert await db.scalar(select(User.login_key_hash).where(User.username == "single4")) is None


async def test_password_reset_signs_out_with_its_own_reason(client, other_client):
    await signup(client, "single5")
    raw = "reset-token-for-single-session-test-0123"
    async with session_scope() as db:
        from datetime import datetime, timedelta, timezone

        from app.models import PasswordResetToken

        user = (await db.execute(select(User).where(User.username == "single5"))).scalar_one()
        db.add(PasswordResetToken(user_id=user.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))
        await db.commit()
    form = await other_client.get(f"/reset-password?token={raw}")
    token = META_CSRF.search(form.text).group(1)
    new_password = "Totally-New-Passphrase-77"
    done = await other_client.post("/reset-password", data={"csrf_token": token, "token": raw, "password": new_password, "confirm_password": new_password})
    assert done.status_code == 303
    response = await client.get("/api/me")
    assert response.status_code == 401 and response.json()["reason"] == "password_reset"
    assert "password was reset" in response.json()["detail"]


async def test_expired_session_is_explained(client):
    await signup(client, "single6")
    sid = hashlib.sha256(client.cookies.get(session_cookie_name()).encode()).hexdigest()
    await get_redis().delete(f"pq:sess:{sid}")
    response = await client.get("/dashboard")
    assert response.status_code == 303
    assert "Your session expired" in (await client.get(response.headers["location"])).text


async def test_same_browser_signing_in_again_keeps_working(client):
    me = await signup(client, "single7")
    again = await client.post("/login", data={"csrf_token": me["csrf"], "identifier": "single7", "password": PASSWORD})
    assert again.status_code == 303
    assert (await client.get("/api/me")).status_code == 200


async def test_simultaneous_sign_ins_leave_exactly_one_device(client):
    await signup(client, "single8")
    devices = [new_client() for _ in range(6)]
    try:
        pages = await asyncio.gather(*(d.get("/login?add=1") for d in devices))
        tokens = [META_CSRF.search(p.text).group(1) for p in pages]
        results = await asyncio.gather(
            *(d.post("/login", data={"csrf_token": t, "identifier": "single8", "password": PASSWORD}) for d, t in zip(devices, tokens))
        )
        assert all(r.status_code == 303 for r in results)
        statuses = [(await d.get("/api/me")).status_code for d in devices]
        assert statuses.count(200) == 1, statuses
        assert (await client.get("/api/me")).status_code == 401
        survivor = devices[statuses.index(200)]
        sid = hashlib.sha256(survivor.cookies.get(session_cookie_name()).encode()).hexdigest()
        async with session_scope() as db:
            assert await db.scalar(select(User.login_key_hash).where(User.username == "single8")) == sid
    finally:
        for device in devices:
            await device.aclose()


async def test_signed_out_device_cannot_open_a_socket(client, other_client):
    old = await signup(client, "single9")
    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    await login_as(other_client, "single9")
    sio = create_sio(get_settings())
    connect = sio.handlers["/"]["connect"]
    environ = {"HTTP_COOKIE": cookies, "HTTP_HOST": "testserver", "wsgi.url_scheme": "http"}
    with pytest.raises(SocketRefused):
        await connect("sid-old-device", environ, {"csrf": old["csrf"]})
    note = await get_redis().get(f"pq:ended:{hashlib.sha256(client.cookies.get(session_cookie_name()).encode()).hexdigest()}")
    assert note and json.loads(note)["reason"] == "replaced", "the reason is kept so the device can be told"
