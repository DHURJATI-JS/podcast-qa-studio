"""Chat providers spoken over plain HTTP (httpx), so no vendor SDK version can break us.

* :class:`OpenAICompatibleProvider` - the LAN AI gateway (llama.cpp / Gemma), the local
  Docker Ollama (Gemma) and the official OpenAI (ChatGPT) API all speak this protocol.
* :class:`AnthropicProvider` - the Claude Messages API.

Every provider streams text chunks; errors surface as :class:`ProviderError` so the router
can fall back to the next provider.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """The provider failed; the router may try the next one."""


@dataclass(frozen=True)
class Health:
    status: str  # online | offline | loading | auth_error | not_configured
    detail: str = ""


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant"
    content: str


def short(text: object, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]


class Provider:
    name: str = ""
    label: str = ""
    model: str = ""
    health_interval: int = 20

    def configured(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    async def health(self) -> Health:  # pragma: no cover - interface
        raise NotImplementedError

    def stream(self, system: str, messages: list[Message], *, max_tokens: int, temperature: float) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "model": self.model}


class OpenAICompatibleProvider(Provider):
    def __init__(
        self,
        *,
        name: str,
        label: str,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
        kind: str = "gateway",  # gateway | ollama | openai
        enabled: bool = True,
        connect_timeout: float = 5.0,
        read_timeout: float = 180.0,
    ) -> None:
        self.name = name
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = client
        self.kind = kind
        self.enabled = enabled
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.health_interval = 300 if kind == "openai" else 20

    def configured(self) -> bool:
        if not self.enabled or not self.base_url or not self.model:
            return False
        if self.kind == "openai":
            return bool(self.api_key)
        return True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @property
    def _root(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    async def health(self) -> Health:
        if not self.configured():
            return Health("not_configured", "No API key set" if self.kind == "openai" else "Disabled in settings")
        timeout = httpx.Timeout(self.connect_timeout + 3, connect=self.connect_timeout)
        try:
            if self.kind == "ollama":
                resp = await self.client.get(f"{self._root}/api/tags", timeout=timeout)
                if resp.status_code != 200:
                    return Health("offline", f"HTTP {resp.status_code}")
                names = set()
                for item in resp.json().get("models", []):
                    names.update({item.get("name", ""), item.get("model", "")})
                if self.model not in names and f"{self.model}:latest" not in names:
                    return Health("loading", f"Model {self.model} is still downloading")
                return Health("online", self.model)
            resp = await self.client.get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout)
            if resp.status_code in (401, 403):
                return Health("auth_error", "API key was rejected")
            if resp.status_code != 200:
                return Health("offline", f"HTTP {resp.status_code}")
            if self.kind == "gateway":
                payload = resp.json()
                ids = {str(m.get("id")) for m in payload.get("data", []) if isinstance(m, dict)}
                if ids and self.model not in ids:
                    return Health("offline", f"Model '{self.model}' is not served by the gateway")
            return Health("online", self.model)
        except (httpx.HTTPError, ValueError) as exc:
            return Health("offline", short(f"{type(exc).__name__}: {exc}") or "Unreachable")

    async def stream(self, system: str, messages: list[Message], *, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        body: dict = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}] + [{"role": m.role, "content": m.content} for m in messages],
        }
        if self.kind == "openai":
            # Reasoning-capable models count hidden reasoning tokens here, so leave head-room;
            # they also only accept the default temperature.
            body["max_completion_tokens"] = max_tokens + 4000
        else:
            body["max_tokens"] = max_tokens
            body["temperature"] = temperature
        timeout = httpx.Timeout(self.read_timeout, connect=self.connect_timeout, write=30.0, pool=10.0)
        try:
            async with self.client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers(), timeout=timeout
            ) as resp:
                if resp.status_code != 200:
                    detail = short((await resp.aread()).decode("utf-8", "replace"))
                    raise ProviderError(f"HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("error"):
                        raise ProviderError(short(obj["error"]))
                    for choice in obj.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            yield piece
        except httpx.HTTPError as exc:
            raise ProviderError(short(f"{type(exc).__name__}: {exc}") or "connection failed") from exc


class AnthropicProvider(Provider):
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
        base_url: str = "https://api.anthropic.com",
        connect_timeout: float = 5.0,
        read_timeout: float = 180.0,
    ) -> None:
        self.name = "anthropic"
        self.label = "Claude API"
        self.api_key = api_key
        self.model = model
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.health_interval = 300

    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": self.API_VERSION, "content-type": "application/json"}

    async def health(self) -> Health:
        if not self.configured():
            return Health("not_configured", "No API key set")
        try:
            resp = await self.client.get(
                f"{self.base_url}/v1/models",
                params={"limit": 1},
                headers=self._headers(),
                timeout=httpx.Timeout(self.connect_timeout + 5, connect=self.connect_timeout),
            )
        except httpx.HTTPError as exc:
            return Health("offline", short(f"{type(exc).__name__}: {exc}") or "Unreachable")
        if resp.status_code in (401, 403):
            return Health("auth_error", "API key was rejected")
        if resp.status_code != 200:
            return Health("offline", f"HTTP {resp.status_code}")
        return Health("online", self.model)

    async def stream(self, system: str, messages: list[Message], *, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        timeout = httpx.Timeout(self.read_timeout, connect=self.connect_timeout, write=30.0, pool=10.0)
        try:
            async with self.client.stream(
                "POST", f"{self.base_url}/v1/messages", json=body, headers=self._headers(), timeout=timeout
            ) as resp:
                if resp.status_code != 200:
                    detail = short((await resp.aread()).decode("utf-8", "replace"))
                    raise ProviderError(f"HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind = obj.get("type")
                    if kind == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
                    elif kind == "error":
                        raise ProviderError(short((obj.get("error") or {}).get("message", "error")))
                    elif kind == "message_stop":
                        return
        except httpx.HTTPError as exc:
            raise ProviderError(short(f"{type(exc).__name__}: {exc}") or "connection failed") from exc
