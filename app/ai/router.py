"""Provider routing: health checks, circuit breaking and automatic fallback.

Order (configurable): AI gateway -> local Docker Gemma -> Claude API -> ChatGPT API.
If a provider fails *before* producing text we silently move on; if it fails *mid-stream*
the caller is told to discard the partial text (``on_reset``) and the next provider starts.
If nothing is available, :class:`NoAIAvailable` lists every provider and why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

import redis.asyncio as aioredis

from app.ai.providers import Health, Message, Provider, ProviderError, short

log = logging.getLogger(__name__)

HEALTH_KEY = "pq:ai:health"
CB_PREFIX = "pq:ai:cb:"
RECHECK_DOWN_SECONDS = 8
UNAVAILABLE = ("not_configured", "offline", "loading", "auth_error")
STATUS_LABELS = {
    "online": "Online",
    "offline": "Offline",
    "loading": "Downloading model",
    "auth_error": "API key rejected",
    "not_configured": "Not configured",
    "unknown": "Checking",
}


class NoAIAvailable(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        message = "No AI is available right now."
        if reasons:
            message += " " + " | ".join(reasons)
        super().__init__(message)


@dataclass
class StreamResult:
    provider: Provider
    text: str


BuildFn = Callable[[], Awaitable[tuple[str, list[Message], int]]]
StartFn = Callable[[Provider], Awaitable[None]]
TokenFn = Callable[[str], Awaitable[None]]
ResetFn = Callable[[Provider, str], Awaitable[None]]


class AIRouter:
    def __init__(
        self,
        providers: list[Provider],
        redis: aioredis.Redis,
        *,
        temperature: float = 0.7,
        cooldown: int = 45,
    ) -> None:
        self.providers = providers
        self.redis = redis
        self.temperature = temperature
        self.cooldown = cooldown

    # ------------------------------------------------------------------ health
    async def check_provider(self, provider: Provider) -> dict:
        try:
            health = await asyncio.wait_for(provider.health(), timeout=20)
        except Exception as exc:  # noqa: BLE001 - any failure means offline
            health = Health("offline", short(exc) or type(exc).__name__)
        entry = {
            "name": provider.name,
            "label": provider.label,
            "model": provider.model,
            "status": health.status,
            "detail": health.detail,
            "checked_at": time.time(),
        }
        await self.redis.hset(HEALTH_KEY, provider.name, json.dumps(entry))
        if health.status == "online":
            await self.redis.delete(CB_PREFIX + provider.name)
        return entry

    async def check_all(self, *, force: bool = False) -> list[dict]:
        """Check providers whose last check is stale - in parallel.

        Providers that are currently down are re-checked more often so a recovered gateway or
        a finished model download is picked up within seconds.
        """
        current = await self._read()
        now = time.time()

        def interval(p: Provider) -> float:
            status = current.get(p.name, {}).get("status")
            return min(p.health_interval, RECHECK_DOWN_SECONDS) if status in ("offline", "loading") else p.health_interval

        due = [p for p in self.providers if force or now - float(current.get(p.name, {}).get("checked_at", 0)) >= interval(p)]
        if due:
            await asyncio.gather(*(self.check_provider(p) for p in due))
        return await self.statuses()

    async def _read(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, raw in (await self.redis.hgetall(HEALTH_KEY)).items():
            try:
                out[name] = json.loads(raw)
            except ValueError:
                continue
        return out

    async def statuses(self) -> list[dict]:
        current = await self._read()
        breakers = await self.redis.mget([CB_PREFIX + p.name for p in self.providers]) if self.providers else []
        now = time.time()
        out = []
        for provider, breaker in zip(self.providers, breakers):
            entry = current.get(provider.name)
            if not provider.configured():
                status = "not_configured"
                detail = "No API key set" if provider.name in ("anthropic", "openai") else "Disabled in settings"
            elif not entry or now - float(entry.get("checked_at", 0)) > max(90, provider.health_interval * 3):
                status, detail = "unknown", ""
            else:
                status, detail = entry.get("status", "unknown"), entry.get("detail", "")
            if status == "online" and breaker:
                status, detail = "offline", f"Last request failed ({breaker[:80]}); retrying shortly"
            out.append(
                {
                    "name": provider.name,
                    "label": provider.label,
                    "model": provider.model,
                    "status": status,
                    "status_label": STATUS_LABELS.get(status, status),
                    "detail": detail,
                }
            )
        return out

    @staticmethod
    def summarize(statuses: list[dict]) -> dict:
        online = [s for s in statuses if s["status"] == "online"]
        maybe = [s for s in statuses if s["status"] == "unknown"]
        return {
            "any_available": bool(online or maybe),
            "active": online[0]["label"] if online else (maybe[0]["label"] if maybe else None),
            "providers": statuses,
        }

    # ------------------------------------------------------------------ generation
    async def run(
        self, build: BuildFn, *, on_start: StartFn, on_token: TokenFn, on_reset: ResetFn, temperature: float | None = None
    ) -> StreamResult:
        """Try healthy providers in order; then, as a last resort, ones last seen offline
        (a health check can be a few seconds stale - better to try than to say "no AI").
        ``temperature`` overrides the default for this call (0 for yes/no judgements)."""
        temperature = self.temperature if temperature is None else temperature
        reasons: dict[str, str] = {}  # one (latest) reason per provider, in provider order
        statuses = {s["name"]: s for s in await self.statuses()}
        first_pass: list[Provider] = []
        last_resort: list[Provider] = []
        for provider in self.providers:
            status = statuses.get(provider.name, {"status": "unknown", "status_label": "Checking", "detail": ""})
            if status["status"] in UNAVAILABLE:
                why = status["status_label"].lower()
                if status.get("detail"):
                    why += f" ({short(status['detail'], 90)})"
                reasons[provider.name] = f"{provider.label}: {why}"
                if status["status"] == "offline" and provider.configured():
                    last_resort.append(provider)
                continue
            first_pass.append(provider)
        tried_last_resort = False
        for provider in first_pass + last_resort:
            if provider in last_resort:
                if not tried_last_resort:
                    log.info("No healthy AI provider answered; retrying providers last seen offline")
                tried_last_resort = True
            system, messages, max_tokens = await build()
            await on_start(provider)
            pieces: list[str] = []
            try:
                async with aclosing(
                    provider.stream(system, messages, max_tokens=max_tokens, temperature=temperature)
                ) as stream:
                    async for piece in stream:
                        pieces.append(piece)
                        await on_token(piece)
                text = "".join(pieces)
                if not text.strip():
                    raise ProviderError("returned an empty reply")
                return StreamResult(provider, text)
            except ProviderError as exc:
                log.warning("AI provider %s failed: %s", provider.name, exc)
                await self.redis.set(CB_PREFIX + provider.name, short(exc, 200), ex=self.cooldown)
                reasons[provider.name] = f"{provider.label}: {short(exc, 120)}"
                if pieces:
                    await on_reset(provider, short(exc, 120))
        raise NoAIAvailable([reasons[p.name] for p in self.providers if p.name in reasons])
