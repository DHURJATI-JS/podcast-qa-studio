"""CSRF protection (pure ASGI, fully async).

Pattern: *signed double-submit cookie* + *same-origin check* (OWASP recommended).

* Every browser gets a random, HttpOnly ``pq_csrf`` cookie (``__Host-`` prefixed over HTTPS).
* Pages receive ``HMAC(SECRET_KEY, cookie)`` as the token (meta tag / hidden input). An attacker
  can neither read the cookie nor compute the HMAC without the server key.
* Every unsafe request (POST/PUT/PATCH/DELETE) must carry the token in the ``X-CSRF-Token``
  header or a ``csrf_token`` form field, and its Origin/Referer must be this site.
* The cookie is rotated on login/logout.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FORM_FIELD = "csrf_token"
HEADER = "x-csrf-token"


def cookie_name(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return "__Host-pq_csrf" if settings.cookie_secure else "pq_csrf"


def token_for(cookie_value: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode("utf-8")
    return hmac.new(key, b"csrf:" + cookie_value.encode("utf-8"), hashlib.sha256).hexdigest()


def valid_cookie(value: str | None) -> bool:
    return bool(value) and 20 <= len(value or "") <= 100 and (value or "").replace("-", "").replace("_", "").isalnum()


def check_token(cookie_value: str | None, token: str | None) -> bool:
    if not valid_cookie(cookie_value) or not token or len(token) != 64:
        return False
    return hmac.compare_digest(token_for(cookie_value or ""), token)


def new_cookie_value() -> str:
    return secrets.token_urlsafe(32)


def cookie_header(value: str, settings: Settings | None = None, *, max_age: int = 60 * 60 * 24 * 30) -> str:
    settings = settings or get_settings()
    parts = [f"{cookie_name(settings)}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if settings.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def parse_cookies(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            key, value = key.strip(), value.strip().strip('"')
            if key and key not in cookies:
                cookies[key] = value
    return cookies


def origin_allowed(origin: str | None, host: str, scheme: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not origin:
        return False
    origin = origin.lower().rstrip("/")
    if origin == "null":
        return False
    return origin in {settings.base_origin, f"{scheme}://{host.lower()}"}


def same_origin_request(headers: Headers, scheme: str, settings: Settings) -> bool:
    origin = headers.get("origin")
    if not origin:
        referer = headers.get("referer")
        if not referer:
            # Non-browser client or stripped headers: the token check below still applies.
            return True
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    return origin_allowed(origin, headers.get("host", ""), scheme, settings)


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, exempt_prefixes: tuple[str, ...] = ("/healthz", "/readyz")) -> None:
        self.app = app
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        headers = Headers(scope=scope)
        name = cookie_name(settings)
        cookie_value = parse_cookies(headers.get("cookie", "")).get(name)
        issue_cookie: str | None = None
        if not valid_cookie(cookie_value):
            cookie_value = new_cookie_value()
            issue_cookie = cookie_value
        state = scope.setdefault("state", {})
        state["csrf_cookie"] = cookie_value
        state["csrf_token"] = token_for(cookie_value or "")

        if scope["method"] not in SAFE_METHODS and not scope["path"].startswith(self.exempt_prefixes):
            if not same_origin_request(headers, scope.get("scheme", "http"), settings):
                await _reject(scope, send, "Cross-site request blocked.")
                return
            token = headers.get(HEADER)
            if token is None and headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                body = await _read_body(receive, settings.max_body_bytes)
                if body is None:
                    await _reject(scope, send, "Request too large.", status=413)
                    return
                token = parse_qs(body.decode("latin-1"), keep_blank_values=True).get(FORM_FIELD, [""])[0]
                receive = _replay(body, receive)
            if issue_cookie is not None or not check_token(cookie_value, token):
                await _reject(scope, send, "Your session expired or the form is stale. Refresh the page and try again.")
                return

        async def send_wrapper(message: Message) -> None:
            if issue_cookie is not None and message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                already = any(v.startswith(f"{name}=") for v in out.getlist("set-cookie"))
                if not already:
                    out.append("set-cookie", cookie_header(issue_cookie, settings))
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, original: Receive) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


async def _reject(scope: Scope, send: Send, message: str, status: int = 403) -> None:
    if scope["path"].startswith("/api/"):
        body = json.dumps({"detail": message, "code": "csrf"}).encode()
        ctype = b"application/json"
    else:
        safe = message.replace("<", "&lt;").replace(">", "&gt;")
        body = (
            "<!doctype html><meta charset='utf-8'><title>Request blocked</title>"
            f"<body><h1>Request blocked</h1><p>{safe}</p><p><a href='/'>Go to the home page</a></p></body>"
        ).encode()
        ctype = b"text/html; charset=utf-8"
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})
