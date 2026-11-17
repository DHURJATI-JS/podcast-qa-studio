"""Jinja2 templates (autoescaped) and small page helpers (flash messages, anti-bot form tokens)."""

from __future__ import annotations

import time
from typing import Any

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.responses import Response

from app import __version__
from app.assets import STATIC_DIR, asset_url
from app.config import get_settings
from app.services.duration import fmt_duration

FLASH_COOKIE = "pq_flash"
AVATAR_CLASSES = [
    "bg-violet-600", "bg-rose-600", "bg-amber-600", "bg-emerald-600",
    "bg-sky-600", "bg-fuchsia-600", "bg-teal-600", "bg-indigo-600",
]
CATEGORY_LABELS = {
    "opener": "Opener", "background": "Background", "core": "Core", "story": "Story", "challenge": "Challenge",
    "rapid_fire": "Rapid fire", "audience": "For listeners", "closing": "Closing", "follow_up": "Follow-up",
}

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(STATIC_DIR.parent / "templates")),
    autoescape=jinja2.select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals.update(asset=asset_url, avatar_classes=AVATAR_CLASSES, app_version=__version__, category_labels=CATEGORY_LABELS)
_env.filters["duration"] = fmt_duration
templates = Jinja2Templates(env=_env)


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt=salt)


def flash(response: Response, message: str, kind: str = "info") -> None:
    settings = get_settings()
    response.set_cookie(
        FLASH_COOKIE,
        _serializer("flash").dumps({"m": message[:300], "k": kind}),
        max_age=120,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _pop_flash(request: Request) -> dict | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer("flash").loads(raw, max_age=300)
    except (BadSignature, ValueError, TypeError):
        return None
    kind = str(data.get("k", "info"))
    return {"message": str(data.get("m", "")), "kind": kind if kind in ("info", "success", "error") else "info"}


def form_token() -> str:
    return _serializer("form-ts").dumps(int(time.time()))


def form_token_age(token: str) -> float | None:
    try:
        issued = int(_serializer("form-ts").loads(token, max_age=86400))
    except (BadSignature, ValueError, TypeError):
        return None
    return time.time() - issued


def render(request: Request, name: str, context: dict[str, Any] | None = None, *, status_code: int = 200) -> Response:
    settings = get_settings()
    ctx: dict[str, Any] = {
        "user": getattr(request.state, "user", None),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "app_name": settings.app_name,
        "flash": _pop_flash(request),
        "form_token": form_token(),
    }
    signout = getattr(request.state, "signout", None)
    if signout and not ctx.get("flash"):
        from app.security.sessions import signout_message

        ctx["flash"] = {"message": signout_message(signout), "kind": "error"}
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if request.cookies.get(FLASH_COOKIE):
        response.delete_cookie(FLASH_COOKIE, path="/")
    if signout:
        from app.security.sessions import clear_session_cookie

        clear_session_cookie(response)
    return response
