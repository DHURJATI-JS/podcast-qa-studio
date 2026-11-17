"""Sign up, Google-style two-step sign in with an account chooser, sign out, forgot/reset password."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import session_scope
from app.models import PasswordResetToken, User
from app.redis_client import get_redis
from app.security import ratelimit
from app.security.passwords import burn_time, hash_password, password_problem, verify_password
from app.security.sanitize import clean_text, safe_next
from app.security.sessions import (
    clear_session_cookie,
    client_ip,
    end_user_sessions,
    current_user,
    destroy_session,
    destroy_user_sessions,
    device_accounts,
    device_cookie_name,
    ensure_device_token,
    forget_on_device,
    invalidate_profile,
    release_login_key,
    remember_on_device,
    rotate_csrf,
    session_cookie_name,
    set_session_cookie,
    start_exclusive_session,
)
from app.realtime import disconnect_user_sockets, notify_signed_out_devices
from app.services.mailer import send_email
from app.templating import flash, form_token_age, render

router = APIRouter()

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._]{1,28})[a-z0-9]$")
RESERVED_USERNAMES = {
    "admin", "administrator", "root", "support", "help", "system", "mira", "api", "static", "login", "logout",
    "signup", "account", "dashboard", "podcast", "podcasts", "security", "staff", "moderator", "null", "undefined",
}
GENERIC_RESET_MESSAGE = (
    "If an account exists for that email, we've sent a link to reset your password. "
    "Check your inbox (and spam folder). The link expires in {minutes} minutes."
)


def _minutes(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_bot(form, request: Request) -> bool:  # noqa: ANN001
    """Honeypot field + minimum fill time (bots submit instantly)."""
    if str(form.get("website", "")).strip():
        return True
    age = form_token_age(str(form.get("form_ts", "")))
    settings = get_settings()
    minimum = 0 if settings.is_test else 2
    return age is None or age < minimum


# --------------------------------------------------------------------------- sign in
@router.get("/login")
async def login_page(request: Request, next: str = "/dashboard", add: int = 0):  # noqa: A002
    if await current_user(request):
        return RedirectResponse(safe_next(next), status_code=303)
    accounts = [] if add else await device_accounts(get_redis(), request.cookies.get(device_cookie_name()))
    return render(request, "auth/login.html", {"accounts": accounts, "next": safe_next(next), "identifier": "", "page": "login"})


@router.post("/login")
async def login_submit(request: Request):
    settings = get_settings()
    form = await request.form()
    identifier = clean_text(form.get("identifier", ""), max_len=254).lower()
    password = str(form.get("password", ""))[:256]
    next_url = safe_next(str(form.get("next", "")))
    redis = get_redis()
    ip = client_ip(request)

    def fail(message: str, status: int = 400):
        return render(
            request,
            "auth/login.html",
            {"accounts": [], "next": next_url, "identifier": identifier, "error": message, "page": "login"},
            status_code=status,
        )

    if not identifier or not password:
        return fail("Enter your email or username and your password.")
    ip_limit = await ratelimit.hit(redis, "login_ip", ip, settings.rate("login_ip"))
    if not ip_limit.allowed:
        return fail(f"Too many sign-in attempts. Try again in {_minutes(ip_limit.retry_after)}.", 429)
    account_limit = await ratelimit.peek(redis, "login_fail", identifier, settings.rate("login_account"))
    if not account_limit.allowed:
        return fail(f"Too many failed attempts for this account. Try again in {_minutes(account_limit.retry_after)}.", 429)

    async with session_scope() as db:
        column = User.email if "@" in identifier else User.username
        user = (await db.execute(select(User).where(column == identifier))).scalar_one_or_none()
        if user is None or not user.is_active:
            await burn_time(password)
            await ratelimit.hit(redis, "login_fail", identifier, settings.rate("login_account"))
            return fail("Wrong email/username or password.", 401)
        ok, needs_rehash = await verify_password(user.password_hash, password)
        if not ok:
            await ratelimit.hit(redis, "login_fail", identifier, settings.rate("login_account"))
            return fail("Wrong email/username or password.", 401)
        if needs_rehash:
            user.password_hash = await hash_password(password)
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        user_id = user.id

    await ratelimit.clear(redis, "login_fail", identifier)
    await destroy_session(redis, request.cookies.get(session_cookie_name()))
    # One login per account: this sign-in signs every other device out (and they're told why).
    token, replaced = await start_exclusive_session(redis, user_id, ip=ip, user_agent=request.headers.get("user-agent", ""))
    if replaced:
        await notify_signed_out_devices(request.app.state, user_id)
    response = RedirectResponse(next_url, status_code=303)
    set_session_cookie(response, token)
    rotate_csrf(response)
    await remember_on_device(redis, ensure_device_token(request, response), user_id)
    return response


@router.post("/login/forget")
async def forget_account(request: Request):
    form = await request.form()
    await forget_on_device(get_redis(), request.cookies.get(device_cookie_name()), str(form.get("user_id", ""))[:36])
    return RedirectResponse("/login", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    user = await current_user(request)
    await destroy_session(get_redis(), request.cookies.get(session_cookie_name()))
    if user is not None:
        await release_login_key(user.id, user.session_id)
        await disconnect_user_sockets(getattr(request.app.state, "sio", None), user.id)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    rotate_csrf(response)
    flash(response, "You're signed out.", "info")
    return response


# --------------------------------------------------------------------------- sign up
@router.get("/signup")
async def signup_page(request: Request):
    if await current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "auth/signup.html", {"values": {}, "errors": {}, "page": "signup"})


@router.post("/signup")
async def signup_submit(request: Request):
    settings = get_settings()
    form = await request.form()
    values = {
        "username": clean_text(form.get("username", ""), max_len=40).lower(),
        "email": clean_text(form.get("email", ""), max_len=254).lower(),
        "display_name": clean_text(form.get("display_name", ""), max_len=60),
    }
    password = str(form.get("password", ""))[:256]
    confirm = str(form.get("confirm_password", ""))[:256]
    errors: dict[str, str] = {}

    def page(status: int):
        return render(request, "auth/signup.html", {"values": values, "errors": errors, "page": "signup"}, status_code=status)

    if _is_bot(form, request):
        errors["_"] = "Please take a moment and submit the form again."
        return page(400)
    limit = await ratelimit.hit(get_redis(), "signup_ip", client_ip(request), settings.rate("signup_ip"))
    if not limit.allowed:
        errors["_"] = f"Too many sign-ups from your network. Try again in {_minutes(limit.retry_after)}."
        return page(429)

    username = values["username"]
    if not USERNAME_RE.match(username) or ".." in username:
        errors["username"] = "3-30 characters: lowercase letters, numbers, dots or underscores (not at the ends)."
    elif username in RESERVED_USERNAMES:
        errors["username"] = "That username is reserved. Please pick another."
    try:
        values["email"] = validate_email(values["email"], check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        errors["email"] = "Enter a valid email address."
    if not values["display_name"]:
        values["display_name"] = username[:60]
    problem = password_problem(password, username=username, email=values["email"])
    if problem:
        errors["password"] = problem
    elif password != confirm:
        errors["confirm_password"] = "The passwords don't match."
    if errors:
        return page(422)

    async with session_scope() as db:
        taken = (
            await db.execute(select(User.username, User.email).where(or_(User.username == username, User.email == values["email"])))
        ).all()
        for row in taken:
            if row.username == username:
                errors["username"] = "That username is taken."
            if row.email == values["email"]:
                errors["email"] = "An account with this email already exists. Try signing in instead."
        if errors:
            return page(409)
        user = User(
            username=username,
            email=values["email"],
            display_name=values["display_name"],
            password_hash=await hash_password(password),
            avatar_color=secrets.randbelow(8),
            tts_voice="mira_soft",
            tts_speed=settings.tts_default_speed,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            errors["_"] = "That username or email was just taken. Please try another."
            return page(409)
        user_id = user.id
        first_name = values["display_name"].split()[0]

    redis = get_redis()
    token, _ = await start_exclusive_session(redis, user_id, ip=client_ip(request), user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, token)
    rotate_csrf(response)
    await remember_on_device(redis, ensure_device_token(request, response), user_id)
    flash(response, f"Welcome, {first_name}! Pick an option below to get started.", "success")
    return response


# --------------------------------------------------------------------------- forgot / reset password
@router.get("/forgot-password")
async def forgot_page(request: Request):
    return render(request, "auth/forgot.html", {"sent": False, "page": "forgot"})


@router.post("/forgot-password")
async def forgot_submit(request: Request, background: BackgroundTasks):
    settings = get_settings()
    form = await request.form()
    email = clean_text(form.get("email", ""), max_len=254).lower()
    message = GENERIC_RESET_MESSAGE.format(minutes=settings.password_reset_minutes)
    sent_page = render(request, "auth/forgot.html", {"sent": True, "message": message, "page": "forgot"})
    if _is_bot(form, request) or "@" not in email:
        return sent_page
    redis = get_redis()
    ip = client_ip(request)
    ip_limit = await ratelimit.hit(redis, "forgot_ip", ip, settings.rate("forgot_ip"))
    if not ip_limit.allowed:
        return render(
            request,
            "auth/forgot.html",
            {"sent": False, "error": f"Too many requests. Try again in {_minutes(ip_limit.retry_after)}.", "page": "forgot"},
            status_code=429,
        )
    if not (await ratelimit.hit(redis, "forgot_account", email, settings.rate("forgot_account"))).allowed:
        return sent_page  # don't reveal anything
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == email, User.is_active.is_(True)))).scalar_one_or_none()
        if user is not None:
            token = secrets.token_urlsafe(32)
            await db.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
                .values(used_at=func.now())
            )
            db.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=_token_hash(token),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_minutes),
                    request_ip=ip[:45],
                )
            )
            await db.commit()
            link = f"{settings.base_url.rstrip('/')}/reset-password?token={token}"
            body = (
                f"Hi {user.display_name},\n\n"
                f"Someone (hopefully you) asked to reset the password for your {settings.app_name} account.\n\n"
                f"PASSWORD RESET LINK (valid for {settings.password_reset_minutes} minutes):\n{link}\n\n"
                "If you didn't ask for this, you can ignore this email - your password won't change.\n"
            )
            background.add_task(send_email, user.email, f"Reset your {settings.app_name} password", body)
    return sent_page


async def _valid_reset_token(token: str) -> PasswordResetToken | None:
    if not token or len(token) > 100:
        return None
    async with session_scope() as db:
        return (
            await db.execute(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_hash == _token_hash(token),
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.expires_at > func.now(),
                )
            )
        ).scalar_one_or_none()


@router.get("/reset-password")
async def reset_page(request: Request, token: str = ""):
    valid = await _valid_reset_token(token) is not None
    response = render(request, "auth/reset.html", {"token": token if valid else "", "invalid": not valid, "page": "reset"})
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/reset-password")
async def reset_submit(request: Request, background: BackgroundTasks):
    settings = get_settings()
    form = await request.form()
    token = str(form.get("token", ""))[:100]
    password = str(form.get("password", ""))[:256]
    confirm = str(form.get("confirm_password", ""))[:256]
    redis = get_redis()

    def page(error: str | None = None, invalid: bool = False, status: int = 400):
        response = render(
            request, "auth/reset.html", {"token": "" if invalid else token, "invalid": invalid, "error": error, "page": "reset"}, status_code=status
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    limit = await ratelimit.hit(redis, "reset_ip", client_ip(request), settings.rate("reset_ip"))
    if not limit.allowed:
        return page(f"Too many attempts. Try again in {_minutes(limit.retry_after)}.", status=429)
    record = await _valid_reset_token(token)
    if record is None:
        return page(invalid=True)
    async with session_scope() as db:
        user = await db.get(User, record.user_id)
        if user is None or not user.is_active:
            return page(invalid=True)
        problem = password_problem(password, username=user.username, email=user.email)
        if problem:
            return page(problem, status=422)
        if password != confirm:
            return page("The passwords don't match.", status=422)
        consumed = (
            await db.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == record.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.expires_at > func.now(),
                )
                .values(used_at=func.now())
                .returning(PasswordResetToken.id)
            )
        ).first()
        if consumed is None:
            await db.rollback()
            return page(invalid=True)
        user.password_hash = await hash_password(password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.login_key_hash = None  # every device must sign in again...
        user.signout_reason = "password_reset"  # ...and is told why
        await db.commit()
        user_id, email, name = user.id, user.email, user.display_name
    await end_user_sessions(redis, user_id, {"reason": "password_reset"})
    await invalidate_profile(redis, user_id)
    await notify_signed_out_devices(request.app.state, user_id)
    background.add_task(
        send_email,
        email,
        f"Your {settings.app_name} password was changed",
        f"Hi {name},\n\nYour password was just changed and all your devices were signed out.\n"
        "If this wasn't you, reset your password again right away.\n",
    )
    response = RedirectResponse("/login", status_code=303)
    flash(response, "Your password has been reset. Sign in with your new password.", "success")
    return response
