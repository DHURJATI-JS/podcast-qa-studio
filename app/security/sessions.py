"""Server-side sessions in Redis + Google-style account display.

After sign-in the browser only holds an opaque random token. Redis maps ``sha256(token)`` to
the **user id** (plus created/last-seen metadata). The username / display name is then *fetched*
by user id (Redis profile cache -> PostgreSQL) for the Google-like account chip, the
"Hi, <name>" menu and the "Choose an account" screen on this device.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import Request
from starlette.responses import Response

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import session_scope
from app.models import User
from app.redis_client import get_redis
from app.security import csrf

PROFILE_TTL = 600
DEVICE_TTL = 180 * 86400
ENDED_TTL = 7 * 86400  # how long a signed-out device can still be told *why*


def session_cookie_name() -> str:
    return "__Host-pq_session" if get_settings().cookie_secure else "pq_session"


def device_cookie_name() -> str:
    return "__Host-pq_device" if get_settings().cookie_secure else "pq_device"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_token(token: str | None) -> bool:
    return bool(token) and 20 <= len(token or "") <= 100


# --------------------------------------------------------------------------- sessions
@dataclass(frozen=True)
class SessionInfo:
    sid: str
    user_id: uuid.UUID
    created: int


async def create_session(redis: aioredis.Redis, user_id: uuid.UUID, *, ip: str, user_agent: str) -> str:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    sid = _digest(token)
    now = int(time.time())
    key, user_key = f"pq:sess:{sid}", f"pq:usess:{user_id}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping={"uid": str(user_id), "created": now, "seen": now, "ip": ip[:45], "ua": user_agent[:160]})
        pipe.expire(key, settings.session_idle_days * 86400)
        pipe.zadd(user_key, {sid: time.time()})  # sub-second precision keeps eviction order exact
        pipe.expire(user_key, settings.session_absolute_days * 86400)
        await pipe.execute()
    # Cap concurrent sessions: the oldest ones are signed out.
    extra = await redis.zcard(user_key) - settings.max_sessions_per_user
    if extra > 0:
        oldest = await redis.zrange(user_key, 0, extra - 1)
        if oldest:
            await redis.delete(*[f"pq:sess:{sid_}" for sid_ in oldest])
            await redis.zrem(user_key, *oldest)
    return token


async def get_session(redis: aioredis.Redis, token: str | None) -> SessionInfo | None:
    if not _valid_token(token):
        return None
    settings = get_settings()
    sid = _digest(token or "")
    key = f"pq:sess:{sid}"
    data = await redis.hgetall(key)
    if not data or "uid" not in data:
        return None
    now = int(time.time())
    created, seen = int(data.get("created", 0)), int(data.get("seen", 0))
    try:
        user_id = uuid.UUID(data["uid"])
    except ValueError:
        await redis.delete(key)
        return None
    if now - created > settings.session_absolute_days * 86400:
        await redis.delete(key)
        await redis.zrem(f"pq:usess:{user_id}", sid)
        return None
    if now - seen > 300:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.hset(key, "seen", now)
            pipe.expire(key, settings.session_idle_days * 86400)
            await pipe.execute()
    return SessionInfo(sid=sid, user_id=user_id, created=created)


async def destroy_session(redis: aioredis.Redis, token: str | None) -> None:
    if not _valid_token(token):
        return
    sid = _digest(token or "")
    uid = await redis.hget(f"pq:sess:{sid}", "uid")
    await redis.delete(f"pq:sess:{sid}")
    if uid:
        await redis.zrem(f"pq:usess:{uid}", sid)


async def destroy_user_sessions(redis: aioredis.Redis, user_id: uuid.UUID, keep_sid: str | None = None) -> int:
    user_key = f"pq:usess:{user_id}"
    sids = [sid for sid in await redis.zrange(user_key, 0, -1) if sid != keep_sid]
    if sids:
        await redis.delete(*[f"pq:sess:{sid}" for sid in sids])
        await redis.zrem(user_key, *sids)
    return len(sids)


async def list_sessions(redis: aioredis.Redis, user_id: uuid.UUID, current_sid: str = "") -> list[dict]:
    out = []
    for sid in await redis.zrevrange(f"pq:usess:{user_id}", 0, -1):
        data = await redis.hgetall(f"pq:sess:{sid}")
        if not data:
            await redis.zrem(f"pq:usess:{user_id}", sid)
            continue
        out.append(
            {
                "current": sid == current_sid,
                "created": int(data.get("created", 0)),
                "seen": int(data.get("seen", 0)),
                "ip": data.get("ip", ""),
                "agent": data.get("ua", ""),
            }
        )
    return out


# --------------------------------------------------------------------------- profiles
@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    username: str
    display_name: str
    email: str
    avatar_color: int
    tts_voice: str
    tts_speed: float
    speaking_wpm: int
    session_id: str = ""

    @property
    def initial(self) -> str:
        return (self.display_name.strip() or self.username or "?")[:1].upper()

    @property
    def first_name(self) -> str:
        return self.display_name.split()[0] if self.display_name.strip() else self.username


def profile_from_user(user: User) -> dict:
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "avatar_color": int(user.avatar_color),
        "tts_voice": user.tts_voice,
        "tts_speed": float(user.tts_speed),
        "speaking_wpm": int(user.speaking_wpm),
    }


def user_from_profile(profile: dict, session_id: str = "") -> CurrentUser:
    return CurrentUser(
        id=uuid.UUID(profile["id"]),
        username=profile["username"],
        display_name=profile["display_name"],
        email=profile["email"],
        avatar_color=int(profile.get("avatar_color", 0)) % 8,
        tts_voice=profile.get("tts_voice", "mira_soft"),
        tts_speed=float(profile.get("tts_speed", 0.95)),
        speaking_wpm=int(profile.get("speaking_wpm", 150)),
        session_id=session_id,
    )


async def load_profiles(redis: aioredis.Redis, user_ids: list[str]) -> dict[str, dict]:
    """Fetch profiles by user id: Redis cache first, PostgreSQL for misses."""
    ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not ids:
        return {}
    cached = await redis.mget([f"pq:uprof:{uid}" for uid in ids])
    found: dict[str, dict] = {}
    missing: list[uuid.UUID] = []
    for uid, raw in zip(ids, cached):
        if raw:
            try:
                found[uid] = json.loads(raw)
                continue
            except ValueError:
                pass
        try:
            missing.append(uuid.UUID(uid))
        except ValueError:
            continue
    if missing:
        from sqlalchemy import select

        async with session_scope() as db:
            rows = (await db.execute(select(User).where(User.id.in_(missing), User.is_active.is_(True)))).scalars().all()
        async with redis.pipeline(transaction=False) as pipe:
            for user in rows:
                profile = profile_from_user(user)
                found[profile["id"]] = profile
                pipe.set(f"pq:uprof:{user.id}", json.dumps(profile), ex=PROFILE_TTL)
            await pipe.execute()
    return found


async def load_profile(redis: aioredis.Redis, user_id: uuid.UUID | str) -> dict | None:
    return (await load_profiles(redis, [str(user_id)])).get(str(user_id))


async def invalidate_profile(redis: aioredis.Redis, user_id: uuid.UUID | str) -> None:
    await redis.delete(f"pq:uprof:{user_id}")


# --------------------------------------------------------------------------- device accounts (account chooser)
async def remember_on_device(redis: aioredis.Redis, device_token: str, user_id: uuid.UUID) -> None:
    if not _valid_token(device_token):
        return
    cap = get_settings().max_device_accounts
    key = f"pq:dev:{_digest(device_token)}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, {str(user_id): time.time()})
        pipe.zremrangebyrank(key, 0, -(cap + 1))
        pipe.expire(key, DEVICE_TTL)
        await pipe.execute()


async def forget_on_device(redis: aioredis.Redis, device_token: str | None, user_id: str) -> None:
    if _valid_token(device_token):
        await redis.zrem(f"pq:dev:{_digest(device_token or '')}", user_id)


async def device_accounts(redis: aioredis.Redis, device_token: str | None) -> list[dict]:
    if not _valid_token(device_token):
        return []
    key = f"pq:dev:{_digest(device_token or '')}"
    ids = await redis.zrevrange(key, 0, get_settings().max_device_accounts - 1)
    profiles = await load_profiles(redis, ids)
    accounts = []
    for uid in ids:
        profile = profiles.get(uid)
        if profile is None:
            await redis.zrem(key, uid)
            continue
        user = user_from_profile(profile)
        accounts.append(
            {
                "id": uid,
                "display_name": user.display_name,
                "email": user.email,
                "initial": user.initial,
                "avatar_color": user.avatar_color,
            }
        )
    return accounts


# --------------------------------------------------------------------------- cookies
def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        session_cookie_name(),
        token,
        max_age=settings.session_absolute_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(session_cookie_name(), path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


def ensure_device_token(request: Request, response: Response) -> str:
    settings = get_settings()
    token = request.cookies.get(device_cookie_name())
    if not _valid_token(token):
        token = secrets.token_urlsafe(32)
        response.set_cookie(
            device_cookie_name(), token, max_age=DEVICE_TTL, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/"
        )
    return token or ""


def rotate_csrf(response: Response) -> None:
    response.headers.append("set-cookie", csrf.cookie_header(csrf.new_cookie_value()))


# --------------------------------------------------------------------------- one login per account
# At sign-in the new session's key (sha256 of its random token) is written to users.login_key_hash.
# Every authenticated request - page loads, form posts, API calls, socket connects - compares the
# device's key with the database. A mismatch means the account signed in somewhere else (or the
# password was reset): the device is signed out *before* the request can change anything, and is
# told why.

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
    ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"),
)


def describe_device(user_agent: str) -> str:
    agent = user_agent or ""
    browser = next((name for token, name in _BROWSERS if token in agent), "a browser")
    system = next((name for token, name in _SYSTEMS if token in agent), "")
    return f"{browser} on {system}" if system else browser


def signout_message(info: dict) -> str:
    reason = info.get("reason")
    if reason == "replaced":
        when = ""
        try:
            moment = datetime.fromisoformat(info["at"]).astimezone(timezone.utc)
            when = moment.strftime(", %d %b %Y at %H:%M UTC")
        except (KeyError, TypeError, ValueError):
            pass
        device = info.get("device") or "another device"
        return (
            f"You were signed out on this device because your account signed in on another device "
            f"({device}{when}). Only one device can be signed in at a time - if that wasn't you, "
            "sign in and change your password now."
        )
    if reason == "password_reset":
        return "You were signed out because your password was reset. Sign in with your new password."
    if reason == "deactivated":
        return "You were signed out because this account is no longer active."
    return "Your session expired. Please sign in again."


async def end_session(redis: aioredis.Redis, sid: str, info: dict | None = None) -> None:
    """Destroy one session; optionally leave a short-lived note saying why (shown to that device)."""
    uid = await redis.hget(f"pq:sess:{sid}", "uid")
    await redis.delete(f"pq:sess:{sid}")
    if uid:
        await redis.zrem(f"pq:usess:{uid}", sid)
    if info:
        await redis.set(f"pq:ended:{sid}", json.dumps(info), ex=ENDED_TTL)


async def end_user_sessions(redis: aioredis.Redis, user_id: uuid.UUID, info: dict, keep_sid: str | None = None) -> int:
    sids = [sid for sid in await redis.zrange(f"pq:usess:{user_id}", 0, -1) if sid != keep_sid]
    for sid in sids:
        await end_session(redis, sid, info)
    return len(sids)


async def start_exclusive_session(redis: aioredis.Redis, user_id: uuid.UUID, *, ip: str, user_agent: str) -> tuple[str, int]:
    """Sign in, signing every other device of this account out. Returns (token, devices signed out).

    Serialised per account with a Redis lock, so two simultaneous sign-ins can't both survive
    (or knock each other out): the last one wins and the earlier one is told why.
    """
    lock_name = f"pq:lock:login:{user_id}"
    lock_token = secrets.token_hex(8)
    for _ in range(50):
        if await redis.set(lock_name, lock_token, nx=True, ex=15):
            break
        await asyncio.sleep(0.1)
    try:
        token = await create_session(redis, user_id, ip=ip, user_agent=user_agent)
        sid = _digest(token)
        device = describe_device(user_agent)
        async with session_scope() as db:
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(login_key_hash=sid, login_device=device[:120], last_login_at=func.now(), signout_reason=None)
            )
            await db.commit()
            last_login = await db.scalar(select(User.last_login_at).where(User.id == user_id))
        info = {"reason": "replaced", "device": device, "at": last_login.isoformat() if last_login else None}
        replaced = await end_user_sessions(redis, user_id, info, keep_sid=sid)
        return token, replaced
    finally:
        if await redis.get(lock_name) == lock_token:
            await redis.delete(lock_name)


async def release_login_key(user_id: uuid.UUID, sid: str) -> None:
    """On sign-out, the account no longer has an active device (only if it was this one)."""
    async with session_scope() as db:
        await db.execute(update(User).where(User.id == user_id, User.login_key_hash == sid).values(login_key_hash=None))
        await db.commit()


async def check_login_key(user_id: uuid.UUID, sid: str) -> dict | None:
    """Compare a device's key with the account's current key in PostgreSQL.

    Returns None when the device may continue, otherwise a dict describing why it is signed out.
    """
    async with session_scope() as db:
        row = (
            await db.execute(
                select(User.login_key_hash, User.is_active, User.login_device, User.last_login_at, User.signout_reason).where(
                    User.id == user_id
                )
            )
        ).first()
    if row is None or not row.is_active:
        return {"reason": "deactivated"}
    if row.login_key_hash and hmac.compare_digest(row.login_key_hash, sid):
        return None
    if row.login_key_hash:
        return {"reason": "replaced", "device": row.login_device, "at": row.last_login_at.isoformat() if row.last_login_at else None}
    return {"reason": row.signout_reason or "expired"}


# --------------------------------------------------------------------------- request helpers
class LoginRequired(Exception):
    """Raised by :func:`require_user`; turned into a redirect (pages) or 401 (API)."""


class SignedOut(LoginRequired):
    """This device was signed out (another sign-in, password reset, expiry). Carries the reason."""

    def __init__(self, info: dict) -> None:
        super().__init__(info.get("reason", "expired"))
        self.info = info


async def current_user(request: Request) -> CurrentUser | None:
    state = request.state
    if getattr(state, "user_loaded", False):
        return state.user
    state.user_loaded = True
    state.user = None
    token = request.cookies.get(session_cookie_name())
    if not token:
        return None
    redis = get_redis()
    session = await get_session(redis, token)
    if session is None:
        if _valid_token(token):
            note = await redis.get(f"pq:ended:{_digest(token)}")
            state.signout = json.loads(note) if note else {"reason": "expired"}
        return None
    signout = await check_login_key(session.user_id, session.sid)  # database check, every request
    if signout is not None:
        await end_session(redis, session.sid, signout)
        state.signout = signout
        return None
    profile = await load_profile(redis, session.user_id)
    if profile is None:
        await end_session(redis, session.sid)
        return None
    state.user = user_from_profile(profile, session.sid)
    return state.user


async def require_user(request: Request) -> CurrentUser:
    user = await current_user(request)
    if user is None:
        info = getattr(request.state, "signout", None)
        if info:
            raise SignedOut(info)
        raise LoginRequired()
    return user


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"
