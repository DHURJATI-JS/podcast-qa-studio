"""Async email (aiosmtplib).

If SMTP is not configured and ``EMAIL_FALLBACK_TO_LOG`` is on (local single-user installs),
the email is printed to the server log so the person running the server can still reset a
password. Never enable that fallback on a shared/public server.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings

log = logging.getLogger("app.mail")


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return (local[:2] + "***@" + domain) if domain else "***"


async def send_email(to: str, subject: str, text: str) -> bool:
    settings = get_settings()
    if not settings.smtp_host:
        if settings.email_fallback_to_log:
            bar = "=" * 72
            log.warning("\n%s\nEMAIL NOT SENT (SMTP not configured) - copy for the server operator\nTo: %s\nSubject: %s\n\n%s\n%s", bar, to, subject, text, bar)
        else:
            log.error("Email to %s not sent: SMTP is not configured", mask_email(to))
        return False
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password.get_secret_value() or None,
            start_tls=settings.smtp_starttls and not settings.smtp_ssl,
            use_tls=settings.smtp_ssl,
            timeout=20,
        )
        return True
    except Exception:  # noqa: BLE001 - never let email failures break a request
        log.exception("Failed to send email to %s", mask_email(to))
        return False
