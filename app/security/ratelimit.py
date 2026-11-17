"""Redis-backed rate limits, idempotency keys (double-submit protection) and locks.

All operations are atomic in Redis, so limits hold even when requests arrive at the same moment
from several web workers.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

import redis.asyncio as aioredis

_HIT_LUA = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if current == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {current, ttl}
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    count: int
    limit: int
    retry_after: int


class RateLimited(Exception):
    def __init__(self, retry_after: int, message: str = "Too many requests. Please slow down."):
        self.retry_after = max(1, int(retry_after))
        self.message = message
        super().__init__(message)


def _key(scope: str, ident: str) -> str:
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
    return f"pq:rl:{scope}:{digest}"


async def hit(redis: aioredis.Redis, scope: str, ident: str, limit: tuple[int, int]) -> RateResult:
    max_count, window = limit
    current, ttl = await redis.eval(_HIT_LUA, 1, _key(scope, ident), window)
    current, ttl = int(current), int(ttl)
    return RateResult(current <= max_count, current, max_count, max(1, ttl))


async def peek(redis: aioredis.Redis, scope: str, ident: str, limit: tuple[int, int]) -> RateResult:
    key = _key(scope, ident)
    raw = await redis.get(key)
    current = int(raw or 0)
    ttl = await redis.ttl(key) if current else 0
    return RateResult(current < limit[0], current, limit[0], max(1, int(ttl)))


async def clear(redis: aioredis.Redis, scope: str, ident: str) -> None:
    await redis.delete(_key(scope, ident))


async def enforce(
    redis: aioredis.Redis,
    scope: str,
    ident: str,
    limit: tuple[int, int],
    message: str = "Too many requests. Please slow down.",
) -> None:
    result = await hit(redis, scope, ident, limit)
    if not result.allowed:
        raise RateLimited(result.retry_after, message)


def valid_idempotency_key(key: str | None) -> bool:
    return bool(key) and bool(_IDEM_RE.match(key or ""))


async def claim_once(redis: aioredis.Redis, owner: str, key: str | None, ttl: int = 900) -> bool:
    """Return True the first time ``key`` is seen for ``owner``; False for a duplicate submit."""
    if not valid_idempotency_key(key):
        return True
    return bool(await redis.set(f"pq:idem:{owner}:{key}", "1", nx=True, ex=ttl))


async def remember_result(redis: aioredis.Redis, owner: str, key: str | None, value: str, ttl: int = 900) -> None:
    if valid_idempotency_key(key):
        await redis.set(f"pq:idem:{owner}:{key}", value, ex=ttl)


async def get_result(redis: aioredis.Redis, owner: str, key: str | None) -> str | None:
    if not valid_idempotency_key(key):
        return None
    return await redis.get(f"pq:idem:{owner}:{key}")


async def acquire_lock(redis: aioredis.Redis, name: str, ttl: int) -> str | None:
    token = secrets.token_hex(12)
    ok = await redis.set(f"pq:lock:{name}", token, nx=True, ex=max(1, int(ttl)))
    return token if ok else None


async def release_lock(redis: aioredis.Redis, name: str, token: str | None) -> None:
    if token:
        await redis.eval(_RELEASE_LUA, 1, f"pq:lock:{name}", token)


async def force_release_lock(redis: aioredis.Redis, name: str) -> None:
    await redis.delete(f"pq:lock:{name}")
