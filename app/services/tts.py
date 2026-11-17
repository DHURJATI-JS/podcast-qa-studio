"""Kokoro text-to-speech (Kokoro-FastAPI container, OpenAI-compatible ``/v1/audio/speech``).

Mira's default voice is a soft, warm female blend. If Kokoro is offline the browser falls back
to its own speech synthesis (lip-sync then follows word boundaries instead of audio levels).
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import Settings

# id -> (label, Kokoro voice expression). Only these ids are accepted from clients.
VOICES: dict[str, tuple[str, str]] = {
    "mira_soft": ("Mira - soft & warm (default)", "af_heart(3)+af_nicole(1)"),
    "af_heart": ("Heart - warm", "af_heart"),
    "af_bella": ("Bella - bright", "af_bella"),
    "af_nicole": ("Nicole - whisper-soft", "af_nicole"),
    "af_sky": ("Sky - airy", "af_sky"),
    "af_sarah": ("Sarah - calm", "af_sarah"),
    "bf_emma": ("Emma - British", "bf_emma"),
    "bf_isabella": ("Isabella - British, gentle", "bf_isabella"),
}
DEFAULT_VOICE = "mira_soft"
MAX_AUDIO_BYTES = 6_000_000


class TTSUnavailable(Exception):
    pass


def voice_options() -> list[dict]:
    return [{"id": key, "label": label} for key, (label, _) in VOICES.items()]


def resolve_voice(voice_id: str | None) -> str:
    return voice_id if voice_id in VOICES else DEFAULT_VOICE


class KokoroClient:
    # Kokoro runs on CPU and its memory grows with parallel work (seen at 4 GB under load), so at
    # most GLOBAL_SLOTS syntheses run at once across *all* web processes (Redis slots). When all
    # are busy we fall back to the browser's voice after SLOT_WAIT seconds instead of queueing.
    GLOBAL_SLOTS = 3
    SLOT_WAIT = 2.5
    REQUEST_TIMEOUT = 25.0

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def _acquire_slot(self) -> tuple[str, str] | None:
        from app.redis_client import get_redis
        from app.security.ratelimit import acquire_lock

        redis = get_redis()
        deadline = time.monotonic() + self.SLOT_WAIT
        while True:
            for index in range(self.GLOBAL_SLOTS):
                token = await acquire_lock(redis, f"tts:{index}", int(self.REQUEST_TIMEOUT) + 10)
                if token:
                    return f"tts:{index}", token
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.15)

    async def _release_slot(self, slot: tuple[str, str]) -> None:
        from app.redis_client import get_redis
        from app.security.ratelimit import release_lock

        await release_lock(get_redis(), *slot)

    @property
    def base(self) -> str:
        return self.settings.kokoro_url.rstrip("/")

    async def health(self) -> bool:
        if not self.settings.tts_enabled:
            return False
        try:
            resp = await self.client.get(f"{self.base}/health", timeout=httpx.Timeout(4.0, connect=2.0))
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def synthesize(self, text: str, voice_id: str, speed: float) -> bytes:
        if not self.settings.tts_enabled:
            raise TTSUnavailable("Voice is disabled")
        body = {
            "model": "kokoro",
            "input": text,
            "voice": VOICES[resolve_voice(voice_id)][1],
            "response_format": "mp3",
            "speed": max(0.7, min(1.3, float(speed))),
            "stream": False,
        }
        slot = await self._acquire_slot()
        if slot is None:
            raise TTSUnavailable("Kokoro is busy")
        try:
            resp = await self.client.post(
                f"{self.base}/v1/audio/speech", json=body, timeout=httpx.Timeout(self.REQUEST_TIMEOUT, connect=3.0)
            )
        except httpx.HTTPError as exc:
            raise TTSUnavailable(f"Kokoro unreachable: {type(exc).__name__}") from exc
        finally:
            await self._release_slot(slot)
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("audio/"):
            raise TTSUnavailable(f"Kokoro returned HTTP {resp.status_code}")
        if len(resp.content) > MAX_AUDIO_BYTES:
            raise TTSUnavailable("Audio too large")
        return resp.content
