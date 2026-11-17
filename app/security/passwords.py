"""Argon2id password hashing (run in a thread so the event loop never blocks)."""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher: PasswordHasher | None = None
_dummy_hash: str = ""
# Each Argon2 hash uses ARGON2_MEMORY_KIB of RAM (64 MiB by default). Without a cap, a burst of
# sign-ins could run dozens at once and exhaust memory, so only a few run per process.
_MAX_PARALLEL_HASHES = 4
_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(_MAX_PARALLEL_HASHES)
    return _slots

_COMMON = {
    "password", "password1", "password123", "passw0rd", "123456789", "1234567890", "12345678910",
    "qwertyuiop", "qwerty123", "iloveyou", "letmein123", "welcome123", "admin12345", "administrator",
    "abc1234567", "abcdefghij", "trustno1", "football", "baseball", "superman", "sunshine",
    "princess", "dragon1234", "monkey1234", "starwars", "whatever", "1q2w3e4r5t", "zaq12wsx",
    "qazwsxedc", "asdfghjkl", "zxcvbnm123", "11111111111", "0000000000", "changeme123", "podcast123",
    "podcasting", "microphone", "letmeinplease", "mypassword", "secret1234", "loveyou123",
}


def _get() -> PasswordHasher:
    global _hasher, _dummy_hash
    if _hasher is None:
        settings = get_settings()
        _hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost, memory_cost=settings.argon2_memory_kib, parallelism=2
        )
        _dummy_hash = _hasher.hash("timing-equaliser-not-a-real-password")
    return _hasher


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _get().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    async with _semaphore():
        return await asyncio.to_thread(_get().hash, password)


async def verify_password(password_hash: str, password: str) -> tuple[bool, bool]:
    """Return ``(ok, needs_rehash)``."""
    async with _semaphore():
        ok = await asyncio.to_thread(_verify, password_hash, password)
    return ok, bool(ok and _get().check_needs_rehash(password_hash))


async def burn_time(password: str) -> None:
    """Spend the same time as a real verification (no user-enumeration by timing)."""
    _get()
    async with _semaphore():
        await asyncio.to_thread(_verify, _dummy_hash, password)


def password_problem(password: str, *, username: str = "", email: str = "") -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    lower = password.lower()
    if lower in _COMMON or lower.rstrip("0123456789!@#$%^&*.") in _COMMON:
        return "That password is too common. Pick something less guessable."
    if len(set(password)) < 5:
        return "Use a wider mix of characters."
    for part in (username.lower(), email.lower().split("@")[0]):
        if len(part) >= 4 and part in lower:
            return "Don't include your username or email in your password."
    return None
