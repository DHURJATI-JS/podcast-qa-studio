"""Sign up, two-step sign in, account chooser, sessions in Redis, lockout, forgot/reset password."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.db import session_scope
from app.models import PasswordResetToken, User
from app.redis_client import get_redis
from app.security.sessions import session_cookie_name
from tests.conftest import FORM_TS, META_CSRF, PASSWORD, csrf, signup


async def login(client, identifier, password=PASSWORD, next_url="/dashboard"):
    token = await csrf(client, "/login?add=1")
    return await client.post("/login", data={"csrf_token": token, "identifier": identifier, "password": password, "next": next_url})


async def test_signup_creates_redis_session_and_fetches_username(client):
    await signup(client, "priya", name="Priya Sharma")
    token = client.cookies.get(session_cookie_name())
    assert token
    sid = hashlib.sha256(token.encode()).hexdigest()
    session = await get_redis().hgetall(f"pq:sess:{sid}")
    assert set(session) >= {"uid", "created", "seen"}
    assert "priya" not in str(session.values()), "only the user id belongs in the session"
    me = (await client.get("/api/me")).json()
    assert me["id"] == session["uid"]
    assert me["username"] == "priya" and me["greeting"] == "Hi, Priya!"
    page = await client.get("/dashboard")
    assert "Hi, Priya!" in page.text


async def test_signup_validation(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "rl_signup_ip", "100/3600")
    await signup(client, "taken")
    client.cookies.clear()
    page = await client.get("/signup")
    token, ts = META_CSRF.search(page.text).group(1), FORM_TS.search(page.text).group(1)
    base = {"csrf_token": token, "form_ts": ts, "display_name": "X", "username": "fresh", "email": "fresh@example.com", "password": PASSWORD, "confirm_password": PASSWORD}
    cases = [
        ({"username": "taken"}, "That username is taken"),
        ({"email": "taken@example.com"}, "already exists"),
        ({"password": "short", "confirm_password": "short"}, "at least 10"),
        ({"password": "password123", "confirm_password": "password123"}, "too common"),
        ({"confirm_password": "Different-Horse-99"}, "don&#39;t match"),
        ({"email": "not-an-email"}, "valid email"),
        ({"username": "A!"}, "lowercase letters"),
        ({"username": "admin"}, "reserved"),
    ]
    for change, message in cases:
        response = await client.post("/signup", data={**base, **change})
        assert response.status_code in (409, 422), change
        assert message in response.text, (change, message)
    honeypot = await client.post("/signup", data={**base, "website": "http://spam"})
    assert honeypot.status_code == 400
    async with session_scope() as db:
        assert (await db.execute(select(User).where(User.username == "fresh"))).first() is None


async def test_login_by_username_or_email_and_safe_redirect(client):
    await signup(client, "zara")
    client.cookies.delete(session_cookie_name())
    response = await login(client, "zara", next_url="//evil.example/steal")
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    client.cookies.delete(session_cookie_name())
    response = await login(client, "ZARA@example.com", next_url="/account")
    assert response.status_code == 303 and response.headers["location"] == "/account"


async def test_wrong_password_is_generic_and_locks_out(client):
    await signup(client, "lockme")
    client.cookies.delete(session_cookie_name())
    unknown = await login(client, "nobody-here", "whatever-password")
    wrong = await login(client, "lockme", "Wrong-Password-123")
    assert unknown.status_code == wrong.status_code == 401
    assert "Wrong email/username or password." in unknown.text and "Wrong email/username or password." in wrong.text
    for _ in range(4):
        await login(client, "lockme", "Wrong-Password-123")
    locked = await login(client, "lockme")  # even the right password is refused while locked
    assert locked.status_code == 429 and "Too many failed attempts" in locked.text


async def test_logout_destroys_session_and_device_chooser_shows_name(client):
    await signup(client, "chooser", name="Chooser Person")
    old = client.cookies.get(session_cookie_name())
    token = await csrf(client, "/dashboard")
    response = await client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    sid = hashlib.sha256(old.encode()).hexdigest()
    assert not await get_redis().exists(f"pq:sess:{sid}")
    assert (await client.get("/api/me")).status_code == 401
    chooser = await client.get("/login")
    assert "Choose an account" in chooser.text and "Chooser Person" in chooser.text and "chooser@example.com" in chooser.text


async def test_password_reset_flow(client):
    await signup(client, "resetme")
    old_session = client.cookies.get(session_cookie_name())
    client.cookies.delete(session_cookie_name())
    page = await client.get("/forgot-password")
    token, ts = META_CSRF.search(page.text).group(1), FORM_TS.search(page.text).group(1)
    generic = "If an account exists for that email"
    for email in ("resetme@example.com", "ghost@example.com"):
        response = await client.post("/forgot-password", data={"csrf_token": token, "form_ts": ts, "email": email})
        assert response.status_code == 200 and generic in response.text
    # Pretend we received the email: create a known token the same way the app does.
    raw = "known-reset-token-for-test-0123456789"
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == "resetme"))).scalar_one()
        stored = (await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))).scalars().all()
        assert len(stored) == 1 and len(stored[0].token_hash) == 64, "tokens are stored hashed"
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        await db.commit()
    form = await client.get(f"/reset-password?token={raw}")
    assert "Choose a new password" in form.text and form.headers["referrer-policy"] == "no-referrer"
    token = META_CSRF.search(form.text).group(1)
    new_password = "Brand-New-Passphrase-77"
    weak = await client.post("/reset-password", data={"csrf_token": token, "token": raw, "password": "short", "confirm_password": "short"})
    assert weak.status_code == 422
    done = await client.post("/reset-password", data={"csrf_token": token, "token": raw, "password": new_password, "confirm_password": new_password})
    assert done.status_code == 303
    reused = await client.post("/reset-password", data={"csrf_token": token, "token": raw, "password": new_password, "confirm_password": new_password})
    assert "invalid, expired or has already been used" in reused.text
    sid = hashlib.sha256(old_session.encode()).hexdigest()
    assert not await get_redis().exists(f"pq:sess:{sid}"), "all sessions are signed out after a reset"
    assert (await login(client, "resetme", PASSWORD)).status_code == 401
    client.cookies.delete(session_cookie_name())
    assert (await login(client, "resetme", new_password)).status_code == 303


async def test_expired_reset_token_is_rejected(client):
    await signup(client, "expiry")
    raw = "expired-token-0123456789-abcdefghij"
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == "expiry"))).scalar_one()
        db.add(PasswordResetToken(user_id=user.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await db.commit()
    response = await client.get(f"/reset-password?token={raw}")
    assert "invalid, expired" in response.text


async def test_session_cap_signs_out_oldest(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_user", 2)
    await signup(client, "manysessions")
    first = client.cookies.get(session_cookie_name())
    for _ in range(2):
        client.cookies.delete(session_cookie_name())
        assert (await login(client, "manysessions")).status_code == 303
    assert not await get_redis().exists(f"pq:sess:{hashlib.sha256(first.encode()).hexdigest()}")


async def test_deactivated_user_session_stops_working(client):
    await signup(client, "deactivate")
    async with session_scope() as db:
        await db.execute(update(User).where(User.username == "deactivate").values(is_active=False))
        await db.commit()
    await get_redis().delete(*[k async for k in get_redis().scan_iter("pq:uprof:*")])
    assert (await client.get("/api/me")).status_code == 401


async def test_account_delete_requires_password_and_confirmation(client):
    me = await signup(client, "deleteme")
    bad = await client.post("/account/delete", data={"csrf_token": me["csrf"], "confirm": "DELETE", "password": "nope-nope-nope"})
    assert bad.status_code == 303 and bad.headers["location"] == "/account"
    good = await client.post("/account/delete", data={"csrf_token": me["csrf"], "confirm": "DELETE", "password": PASSWORD})
    assert good.status_code == 303 and good.headers["location"] == "/"
    async with session_scope() as db:
        assert (await db.execute(select(User).where(User.username == "deleteme"))).first() is None


async def test_login_page_never_echoes_password(client):
    response = await login(client, "someone", "Super-Secret-Typed-Password")
    assert "Super-Secret-Typed-Password" not in response.text
    assert re.search(r'name="identifier"[^>]*value="someone"', response.text)
