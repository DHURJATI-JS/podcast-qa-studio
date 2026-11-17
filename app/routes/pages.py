"""Server-rendered pages: home, legal, dashboard, podcast create/edit/workspace, account."""

from __future__ import annotations

import secrets
import uuid
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from starlette.responses import Response

from app.config import get_settings
from app.db import get_db
from app.models import PODCAST_MODES, Answer, Export, Podcast, Question, User
from app.redis_client import get_redis
from app.security import ratelimit
from app.security.passwords import hash_password, password_problem, verify_password
from app.security.sanitize import clean_text
from app.security.sessions import (
    CurrentUser,
    clear_session_cookie,
    current_user,
    describe_device,
    destroy_user_sessions,
    device_cookie_name,
    forget_on_device,
    invalidate_profile,
    list_sessions,
    require_user,
    rotate_csrf,
)
from app.realtime import disconnect_user_sockets
from app.services.duration import fmt_duration, suggest_count
from app.services.podcasts import get_owned_podcast, parse_uuid, workspace_state, NotFound
from app.services.questions import FLAG_HELP
from app.services.tts import VOICES, resolve_voice, voice_options
from app.templating import CATEGORY_LABELS, flash, render

router = APIRouter()
PAGE_SIZE = 24
TONES = ["conversational", "friendly & warm", "professional", "humorous", "investigative", "inspirational", "casual", "academic"]
LANGUAGES = ["English", "Hindi", "Hinglish", "Spanish", "French", "German", "Portuguese", "Italian", "Japanese", "Arabic"]
PODCAST_FIELDS: dict[str, tuple[int, bool, bool]] = {
    # name: (max length, multiline, required)
    "title": (120, False, True),
    "theme": (300, False, True),
    "show_name": (120, False, False),
    "host_name": (80, False, False),
    "guest_name": (80, False, False),
    "audience": (200, False, False),
    "tone": (60, False, False),
    "language": (40, False, False),
    "brief": (2000, True, False),
    "guest_bio": (2000, True, False),
    "talking_points": (2000, True, False),
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _int(value: object, default: int, low: int, high: int) -> int | None:
    try:
        number = int(str(value).strip() or default)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def clean_instructions(lines: list[str], cap: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        item = clean_text(line.lstrip("-*• ").strip(), max_len=300)
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out[:cap]


def parse_podcast_form(form) -> tuple[dict, dict]:  # noqa: ANN001
    settings = get_settings()
    values: dict = {}
    errors: dict[str, str] = {}
    for name, (limit, multiline, required) in PODCAST_FIELDS.items():
        raw = str(form.get(name, "") or "")
        if len(raw.strip()) > limit:
            errors[name] = f"Keep this under {limit} characters."
        value = clean_text(raw, max_len=limit, multiline=multiline)
        if required and not value:
            errors[name] = "This field is required."
        values[name] = value
    values["language"] = values["language"] or "English"
    values["tone"] = values["tone"] or "conversational"
    for name, default, low, high, label in (
        ("target_minutes", 30, 5, 240, "Target length must be 5-240 minutes."),
        ("intro_seconds", 60, 0, 900, "Intro must be 0-900 seconds."),
        ("outro_seconds", 60, 0, 900, "Outro must be 0-900 seconds."),
    ):
        number = _int(form.get(name, default), default, low, high)
        if number is None:
            errors[name] = label
            number = default
        values[name] = number
    raw_lines = [line for line in str(form.get("instructions", "") or "").splitlines() if line.strip()]
    if len(raw_lines) > settings.max_instructions_per_podcast:
        errors["instructions"] = f"Up to {settings.max_instructions_per_podcast} instructions (one per line)."
    values["standing_instructions"] = clean_instructions(raw_lines, settings.max_instructions_per_podcast)
    return values, errors


async def optional_user(request: Request) -> CurrentUser | None:
    return await current_user(request)


# --------------------------------------------------------------------------- public pages
@router.get("/")
async def home(request: Request, user: CurrentUser | None = Depends(optional_user)):
    settings = get_settings()
    return render(request, "home.html", {"page": "home", "limits": settings, "voices": voice_options()})


@router.get("/legal")
async def legal(request: Request, user: CurrentUser | None = Depends(optional_user)):
    return render(request, "legal.html", {"page": "legal"})


# --------------------------------------------------------------------------- dashboard
@router.get("/dashboard")
async def dashboard(
    request: Request,
    q: str = "",
    mode: str = "",
    page: int = 1,
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    query = clean_text(q, max_len=100)
    mode = mode if mode in PODCAST_MODES else ""
    page = max(1, min(int(page), 1000))
    stmt = select(Podcast).where(Podcast.owner_id == user.id)
    if query:
        pattern = f"%{_escape_like(query)}%"
        stmt = stmt.where(or_(Podcast.title.ilike(pattern, escape="\\"), Podcast.theme.ilike(pattern, escape="\\")))
    if mode:
        stmt = stmt.where(Podcast.mode == mode)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    podcasts = (await db.execute(stmt.order_by(Podcast.updated_at.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    ids = [p.id for p in podcasts]
    q_stats = {}
    answered = {}
    if ids:
        q_stats = {
            row.podcast_id: (row.count, row.seconds or 0)
            for row in (
                await db.execute(
                    select(Question.podcast_id, func.count(Question.id).label("count"), func.sum(Question.est_seconds).label("seconds"))
                    .where(Question.podcast_id.in_(ids))
                    .group_by(Question.podcast_id)
                )
            ).all()
        }
        answered = dict(
            (
                await db.execute(
                    select(Answer.podcast_id, func.count(distinct(Answer.question_id))).where(Answer.podcast_id.in_(ids)).group_by(Answer.podcast_id)
                )
            ).all()
        )
    items = []
    for p in podcasts:
        count, seconds = q_stats.get(p.id, (0, 0))
        planned = p.intro_seconds + int(seconds) + p.outro_seconds
        items.append(
            {
                "podcast": p,
                "questions": count,
                "answered": answered.get(p.id, 0),
                "planned": fmt_duration(planned),
                "target": fmt_duration(p.target_minutes * 60),
                "over": planned > p.target_minutes * 60 * 1.05,
            }
        )
    total_all = await db.scalar(select(func.count(Podcast.id)).where(Podcast.owner_id == user.id)) or 0
    return render(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            "items": items,
            "q": query,
            "mode": mode,
            "page_no": page,
            "pages": max(1, -(-total // PAGE_SIZE)),
            "total": total,
            "total_all": total_all,
            "cap": get_settings().max_podcasts_per_user,
        },
    )


# --------------------------------------------------------------------------- create / edit podcasts
def _form_context(mode: str, values: dict, errors: dict, *, editing: Podcast | None = None) -> dict:
    return {
        "page": "podcast-form",
        "mode": mode,
        "values": values,
        "errors": errors,
        "tones": TONES,
        "languages": LANGUAGES,
        "editing": editing,
        "instructions_text": "\n".join(values.get("standing_instructions") or []),
        "max_instructions": get_settings().max_instructions_per_podcast,
        "idem": secrets.token_urlsafe(16),  # double-submit protection for this rendered form
    }


@router.get("/podcasts/new")
async def new_podcast(request: Request, mode: str = "host", user: CurrentUser = Depends(require_user)):
    mode = mode if mode in PODCAST_MODES else "host"
    defaults = {"target_minutes": 30, "intro_seconds": 60, "outro_seconds": 60, "language": "English", "tone": "conversational"}
    return render(request, "podcast_form.html", _form_context(mode, defaults, {}))


@router.post("/podcasts/new")
async def create_podcast(request: Request, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    form = await request.form()
    mode = str(form.get("mode", "host"))
    mode = mode if mode in PODCAST_MODES else "host"
    values, errors = parse_podcast_form(form)
    if errors:
        return render(request, "podcast_form.html", _form_context(mode, values, errors), status_code=422)
    redis = get_redis()
    idem = str(form.get("idem", ""))
    previous = await ratelimit.get_result(redis, f"form:{user.id}", idem)
    if previous:
        # A UUID means the first submit already created the podcast; "1" means it is still in flight.
        target = f"/podcasts/{previous}" if len(previous) == 36 else "/dashboard"
        return RedirectResponse(target, status_code=303)
    if not await ratelimit.claim_once(redis, f"form:{user.id}", idem):
        return RedirectResponse("/dashboard", status_code=303)
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    count = await db.scalar(select(func.count(Podcast.id)).where(Podcast.owner_id == user.id)) or 0
    if count >= settings.max_podcasts_per_user:
        errors["_"] = f"You've reached the limit of {settings.max_podcasts_per_user} podcasts. Delete an old one first."
        await db.rollback()
        return render(request, "podcast_form.html", _form_context(mode, values, errors), status_code=409)
    podcast = Podcast(owner_id=user.id, mode=mode, **values)
    db.add(podcast)
    await db.commit()
    await ratelimit.remember_result(redis, f"form:{user.id}", idem, str(podcast.id))
    suffix = "?autostart=1" if form.get("auto_generate") else ""
    return RedirectResponse(f"/podcasts/{podcast.id}{suffix}", status_code=303)


@router.get("/podcasts/{podcast_id}/settings")
async def edit_podcast(request: Request, podcast_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    values = {name: getattr(podcast, name) for name in list(PODCAST_FIELDS) + ["target_minutes", "intro_seconds", "outro_seconds", "standing_instructions"]}
    return render(request, "podcast_form.html", _form_context(podcast.mode, values, {}, editing=podcast))


@router.post("/podcasts/{podcast_id}/settings")
async def update_podcast(request: Request, podcast_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id, for_update=True)
    form = await request.form()
    values, errors = parse_podcast_form(form)
    if errors:
        # Capture plain values first: rollback() expires ORM objects, and lazy-loading them
        # afterwards is not allowed under asyncio.
        mode, editing = podcast.mode, SimpleNamespace(id=podcast.id)
        await db.rollback()
        return render(request, "podcast_form.html", _form_context(mode, values, errors, editing=editing), status_code=422)
    for name, value in values.items():
        setattr(podcast, name, value)
    await db.commit()
    response = RedirectResponse(f"/podcasts/{podcast.id}", status_code=303)
    flash(response, "Podcast details saved. Mira will use them for every AI suggestion.", "success")
    return response


@router.post("/podcasts/{podcast_id}/delete")
async def delete_podcast(request: Request, podcast_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    title = podcast.title
    await db.delete(podcast)
    await db.commit()
    response = RedirectResponse("/dashboard", status_code=303)
    flash(response, f"Deleted “{title[:60]}”.", "info")
    return response


# --------------------------------------------------------------------------- workspace
@router.get("/podcasts/{podcast_id}")
async def workspace(request: Request, podcast_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    state = await workspace_state(db, user.id, podcast)
    page_data = {
        "state": state,
        "voices": voice_options(),
        "prefs": {"voice": resolve_voice(user.tts_voice), "speed": user.tts_speed, "wpm": user.speaking_wpm},
        "limits": {
            "questions": settings.max_questions_per_podcast,
            "generate": settings.max_generate_batch,
            "rewrite": settings.max_rewrite_batch,
            "instructions": settings.max_instructions_per_podcast,
            "tts_chars": settings.max_tts_chars,
        },
        "categories": CATEGORY_LABELS,
        "flag_help": FLAG_HELP,
        "autostart": request.query_params.get("autostart") == "1" and not state["questions"],
        "suggested_count": suggest_count(state["plan"]["remaining_seconds"], settings.max_generate_batch),
    }
    return render(request, "workspace.html", {"page": "workspace", "podcast": podcast, "page_data": page_data, "plan": state["plan"]})


@router.get("/podcasts/{podcast_id}/exports/{export_id}/download")
async def download_export(podcast_id: str, export_id: str, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    podcast = await get_owned_podcast(db, user.id, podcast_id)
    export = (
        await db.execute(
            select(Export)
            .options(undefer(Export.content))
            .where(Export.id == parse_uuid(export_id), Export.podcast_id == podcast.id, Export.user_id == user.id)
        )
    ).scalar_one_or_none()
    if export is None:
        raise NotFound()
    ascii_name = export.filename.encode("ascii", "ignore").decode() or "podcast.pdf"
    return Response(
        content=export.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(export.filename)}",
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------- account
@router.get("/account")
async def account(request: Request, user: CurrentUser = Depends(require_user)):
    sessions = [
        {**s, "device": describe_device(s["agent"])} for s in await list_sessions(get_redis(), user.id, user.session_id) if s["current"]
    ]
    return render(
        request,
        "account.html",
        {"page": "account", "sessions": sessions, "voices": voice_options(), "current_voice": resolve_voice(user.tts_voice)},
    )


@router.post("/account/profile")
async def update_profile(request: Request, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    form = await request.form()
    display_name = clean_text(form.get("display_name", ""), max_len=60)
    errors = []
    color = _int(form.get("avatar_color", user.avatar_color), user.avatar_color, 0, 7)
    wpm = _int(form.get("speaking_wpm", user.speaking_wpm), user.speaking_wpm, 80, 260)
    try:
        speed = float(str(form.get("tts_speed", user.tts_speed)))
    except ValueError:
        speed = -1
    voice = str(form.get("tts_voice", user.tts_voice))
    if not display_name:
        errors.append("Display name can't be empty.")
    if color is None or wpm is None or not 0.7 <= speed <= 1.3 or voice not in VOICES:
        errors.append("Some settings were out of range.")
    response = RedirectResponse("/account", status_code=303)
    if errors:
        flash(response, " ".join(errors), "error")
        return response
    db_user = await db.get(User, user.id)
    if db_user is None:
        raise NotFound()
    db_user.display_name = display_name
    db_user.avatar_color = color
    db_user.speaking_wpm = wpm
    db_user.tts_speed = round(speed, 2)
    db_user.tts_voice = voice
    await db.commit()
    await invalidate_profile(get_redis(), user.id)
    flash(response, "Profile saved.", "success")
    return response


async def _password_attempt_allowed(user: CurrentUser, response: Response) -> bool:
    """Password checks on account pages are rate-limited: a stolen session must not be able to
    brute-force the current password, nor flood the shared Argon2 workers."""
    limit = await ratelimit.hit(get_redis(), "account_password", str(user.id), get_settings().rate("account_password"))
    if not limit.allowed:
        flash(response, f"Too many password attempts. Try again in {max(1, round(limit.retry_after / 60))} minute(s).", "error")
    return limit.allowed


@router.post("/account/password")
async def change_password(request: Request, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    form = await request.form()
    current = str(form.get("current_password", ""))[:256]
    new = str(form.get("new_password", ""))[:256]
    confirm = str(form.get("confirm_password", ""))[:256]
    response = RedirectResponse("/account", status_code=303)
    if not await _password_attempt_allowed(user, response):
        return response
    db_user = await db.get(User, user.id)
    if db_user is None:
        raise NotFound()
    ok, _ = await verify_password(db_user.password_hash, current)
    if not ok:
        flash(response, "Your current password is incorrect.", "error")
        return response
    problem = password_problem(new, username=db_user.username, email=db_user.email)
    if problem or new != confirm:
        flash(response, problem or "The new passwords don't match.", "error")
        return response
    db_user.password_hash = await hash_password(new)
    from datetime import datetime, timezone

    db_user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    signed_out = await destroy_user_sessions(get_redis(), user.id, keep_sid=user.session_id)
    await disconnect_user_sockets(getattr(request.app.state, "sio", None), user.id)
    flash(response, f"Password changed. Signed out {signed_out} other session(s).", "success")
    return response


@router.post("/account/sessions/revoke")
async def revoke_sessions(request: Request, user: CurrentUser = Depends(require_user)):
    count = await destroy_user_sessions(get_redis(), user.id, keep_sid=user.session_id)
    await disconnect_user_sockets(getattr(request.app.state, "sio", None), user.id)
    response = RedirectResponse("/account", status_code=303)
    flash(response, f"Signed out {count} other session(s).", "success")
    return response


@router.post("/account/delete")
async def delete_account(request: Request, user: CurrentUser = Depends(require_user), db: AsyncSession = Depends(get_db)):
    form = await request.form()
    response = RedirectResponse("/account", status_code=303)
    if not await _password_attempt_allowed(user, response):
        return response
    db_user = await db.get(User, user.id)
    if db_user is None:
        raise NotFound()
    ok, _ = await verify_password(db_user.password_hash, str(form.get("password", ""))[:256])
    if not ok or str(form.get("confirm", "")).strip().upper() != "DELETE":
        flash(response, "Type DELETE and enter your password to delete your account.", "error")
        return response
    await db.execute(delete(User).where(User.id == user.id))
    await db.commit()
    redis = get_redis()
    await destroy_user_sessions(redis, user.id)
    await invalidate_profile(redis, user.id)
    await disconnect_user_sockets(getattr(request.app.state, "sio", None), user.id)
    await forget_on_device(redis, request.cookies.get(device_cookie_name()), str(user.id))
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    rotate_csrf(response)
    flash(response, "Your account and all its podcasts were deleted.", "info")
    return response
