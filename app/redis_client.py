"""Shared async Redis clients (text + binary)."""

from __future__ import annotations

import redis.asyncio as aioredis


class _RedisState:
    text: aioredis.Redis | None = None
    binary: aioredis.Redis | None = None


redis_state = _RedisState()


def init_redis(url: str) -> aioredis.Redis:
    common = {"socket_connect_timeout": 5, "socket_timeout": 15, "health_check_interval": 30}
    redis_state.text = aioredis.from_url(url, decode_responses=True, **common)
    redis_state.binary = aioredis.from_url(url, decode_responses=False, **common)
    return redis_state.text


async def close_redis() -> None:
    for client in (redis_state.text, redis_state.binary):
        if client is not None:
            await client.aclose()
    redis_state.text = None
    redis_state.binary = None


def get_redis() -> aioredis.Redis:
    if redis_state.text is None:
        raise RuntimeError("Redis is not initialised")
    return redis_state.text


def get_redis_binary() -> aioredis.Redis:
    if redis_state.binary is None:
        raise RuntimeError("Redis is not initialised")
    return redis_state.binary
