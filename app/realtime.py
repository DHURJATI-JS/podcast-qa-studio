"""Socket.IO server.

* WebSocket transport only (works across several web workers without sticky sessions).
* Connections must come from this site's origin, carry a valid CSRF token in the ``auth``
  payload (blocks cross-site WebSocket hijacking) and a valid Redis session cookie.
* Each socket joins only its owner's private room ``user:<id>``; job events go nowhere else.
"""

from __future__ import annotations

import logging
import time
import uuid

import socketio
from socketio.exceptions import ConnectionRefusedError as SocketRefused

from app.config import Settings, get_settings
from app.db import session_scope
from app.jobs.emitter import ServerEmitter, user_room
from app.redis_client import get_redis
from app.security import csrf, ratelimit
from app.security.sessions import check_login_key, end_session, get_session, session_cookie_name
from app.services.podcasts import NotFound

log = logging.getLogger(__name__)


def _origin_allowed(origin: str | None, environ: dict) -> bool:
    host = environ.get("HTTP_HOST", "")
    scheme = environ.get("wsgi.url_scheme", "http")
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return csrf.origin_allowed(origin, host, scheme)


async def disconnect_user_sockets(sio: socketio.AsyncServer | None, user_id: uuid.UUID | str) -> None:
    """Close every open socket of a user (on any web process, via the Redis manager).

    Used after sign-out, password changes and session revocation so a revoked session cannot
    keep receiving events; tabs whose session is still valid simply reconnect.
    """
    if sio is None:
        return
    redis = get_redis()
    key = f"pq:sock:{user_id}"
    for sid in await redis.zrange(key, 0, -1):
        try:
            await sio.disconnect(sid, namespace="/")
        except Exception:  # noqa: BLE001 - best effort
            log.debug("Could not disconnect socket %s", sid)
    await redis.delete(key)


async def notify_signed_out_devices(app_state: object, user_id: uuid.UUID | str) -> None:
    """Tell every open tab of this account to re-check its session (the server replies with the
    reason if that device was signed out), then close their sockets."""
    emitter = getattr(app_state, "emitter", None)
    if emitter is not None:
        try:
            await emitter.emit("session:check", {}, room=user_room(user_id))
        except Exception:  # noqa: BLE001 - best effort
            log.debug("Could not notify sockets of user %s", user_id)
    await disconnect_user_sockets(getattr(app_state, "sio", None), user_id)


def create_sio(settings: Settings) -> socketio.AsyncServer:
    manager = socketio.AsyncRedisManager(settings.redis_url, channel="pq-socketio")
    sio = socketio.AsyncServer(
        async_mode="asgi",
        client_manager=manager,
        cors_allowed_origins=_origin_allowed,
        transports=["websocket"],
        ping_interval=25,
        ping_timeout=20,
        max_http_buffer_size=64_000,
    )

    @sio.event
    async def connect(sid: str, environ: dict, auth: object = None) -> None:
        cookies = csrf.parse_cookies(environ.get("HTTP_COOKIE", ""))
        token = auth.get("csrf") if isinstance(auth, dict) else None
        if not csrf.check_token(cookies.get(csrf.cookie_name()), token if isinstance(token, str) else None):
            raise SocketRefused("unauthorized")
        redis = get_redis()
        session = await get_session(redis, cookies.get(session_cookie_name()))
        if session is None:
            raise SocketRefused("signed_out")
        signout = await check_login_key(session.user_id, session.sid)  # one login per account
        if signout is not None:
            await end_session(redis, session.sid, signout)
            raise SocketRefused("signed_out")
        key = f"pq:sock:{session.user_id}"
        now = time.time()
        await redis.zremrangebyscore(key, "-inf", now - 12 * 3600)
        if await redis.zcard(key) >= get_settings().max_sockets_per_user:
            raise SocketRefused("too many open tabs")
        await redis.zadd(key, {sid: now})
        await redis.expire(key, 86400)
        await sio.save_session(sid, {"uid": str(session.user_id), "key": session.sid})
        await sio.enter_room(sid, user_room(session.user_id))

    @sio.event
    async def disconnect(sid: str, reason: object = None) -> None:
        try:
            data = await sio.get_session(sid)
        except KeyError:
            return
        if data and data.get("uid"):
            await get_redis().zrem(f"pq:sock:{data['uid']}", sid)

    @sio.on("job:cancel")
    async def job_cancel(sid: str, data: object) -> dict:
        from app.jobs.service import cancel_job

        try:
            session = await sio.get_session(sid)
        except KeyError:
            return {"ok": False}
        uid = uuid.UUID(session["uid"])
        if await check_login_key(uid, session.get("key", "")) is not None:
            await sio.disconnect(sid)  # this device was signed out meanwhile: no changes
            return {"ok": False, "signed_out": True}
        redis = get_redis()
        result = await ratelimit.hit(redis, "socket", str(uid), get_settings().rate("socket_events"))
        if not result.allowed or not isinstance(data, dict):
            return {"ok": False}
        try:
            async with session_scope() as db:
                status = await cancel_job(db=db, redis=redis, emitter=ServerEmitter(sio), user_id=uid, job_id=str(data.get("job_id", "")))
        except NotFound:
            return {"ok": False}
        return {"ok": True, "status": status}

    return sio
