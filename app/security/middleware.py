"""Pure-ASGI security middleware.

* :class:`Talisman` - an async port of flask-talisman's ideas: per-request CSP **nonce**,
  HSTS, frame/sniff/referrer/permissions policies, optional HTTPS redirect.
* :class:`HostGuardMiddleware` - rejects unexpected Host headers (DNS-rebinding defence).
* :class:`BodyLimitMiddleware` - caps request body size.
* :class:`RequestContextMiddleware` - request id for logs/support.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import time
import logging

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_setup import request_id_var

log = logging.getLogger("app.access")

_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")


class Talisman:
    """Security headers with a fresh CSP nonce per request (``request.state.csp_nonce``)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        force_https: bool = False,
        hsts: bool = False,
        hsts_max_age: int = 31_536_000,
        frame_options: str = "DENY",
        referrer_policy: str = "strict-origin-when-cross-origin",
        permissions_policy: str = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
            "microphone=(), payment=(), usb=(), interest-cohort=()"
        ),
        exempt_https_paths: tuple[str, ...] = ("/healthz", "/readyz"),
    ) -> None:
        self.app = app
        self.force_https = force_https
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age
        self.frame_options = frame_options
        self.referrer_policy = referrer_policy
        self.permissions_policy = permissions_policy
        self.exempt_https_paths = exempt_https_paths

    def csp(self, nonce: str, host: str, scheme: str) -> str:
        ws_scheme = "wss" if scheme == "https" else "ws"
        connect = "'self'"
        if host and _SAFE_HOST.match(host):
            connect = f"'self' {ws_scheme}://{host}"
        directives = [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' 'strict-dynamic'",
            f"style-src 'self' 'nonce-{nonce}'",
            "img-src 'self' data: blob:",
            "media-src 'self' blob:",
            "font-src 'self'",
            f"connect-src {connect}",
            "worker-src 'self' blob:",
            "manifest-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
        ]
        if self.force_https:
            directives.append("upgrade-insecure-requests")
        return "; ".join(directives)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(18)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        headers_in = Headers(scope=scope)
        host = headers_in.get("host", "")
        scheme = scope.get("scheme", "http")
        path = scope.get("path", "")

        if self.force_https and scheme == "http" and not path.startswith(self.exempt_https_paths) and _SAFE_HOST.match(host or "-"):
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{host}{path}" + (f"?{query}" if query else "")
            await send({"type": "http.response.start", "status": 308, "headers": [(b"location", location.encode("latin-1"))]})
            await send({"type": "http.response.body", "body": b""})
            return

        csp = self.csp(nonce, host, scheme)
        # Browsers ignore (and log an error for) COOP on plain-HTTP, non-localhost origins - e.g.
        # http://192.168.x.x on a LAN - so only send it where it has an effect.
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        trustworthy = scheme == "https" or hostname in ("localhost", "127.0.0.1", "[::1]")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["Content-Security-Policy"] = csp
                out.setdefault("X-Content-Type-Options", "nosniff")
                out.setdefault("X-Frame-Options", self.frame_options)
                out.setdefault("Referrer-Policy", self.referrer_policy)
                out.setdefault("Permissions-Policy", self.permissions_policy)
                if trustworthy:
                    out.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                out.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                out.setdefault("X-Permitted-Cross-Domain-Policies", "none")
                if self.hsts:
                    out.setdefault("Strict-Transport-Security", f"max-age={self.hsts_max_age}; includeSubDomains")
                ctype = out.get("content-type", "")
                if "text/html" in ctype or path.startswith("/api/"):
                    out.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


def host_allowed(host_header: str, allowed: frozenset[str]) -> bool:
    host = (host_header or "").strip().lower()
    if not host or not _SAFE_HOST.match(host):
        return False
    if host.startswith("["):
        hostname = host[1 : host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        hostname = host.rsplit(":", 1)[0]
    else:
        hostname = host
    try:
        ipaddress.ip_address(hostname)
        return True  # IP literals can't be DNS-rebinding targets
    except ValueError:
        pass
    if "*" in allowed:
        return True
    for entry in allowed:
        if entry.startswith(".") and (hostname.endswith(entry) or hostname == entry[1:]):
            return True
        if hostname == entry:
            return True
    return False


class HostGuardMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = Headers(scope=scope).get("host", "")
            if not host_allowed(host, self.allowed):
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"Invalid host header"})
                else:
                    await receive()
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                body = json.dumps({"detail": "Request body too large."}).encode()
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else secrets.token_hex(8)
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if status_holder["status"] >= 500 or elapsed > 3000:
                log.warning("%s %s -> %s in %.0fms", scope["method"], scope["path"], status_holder["status"], elapsed)
            request_id_var.reset(token)
