"""Builds the provider list from settings (single place, easy to swap in tests)."""

from __future__ import annotations

import httpx

from app.ai.providers import AnthropicProvider, OpenAICompatibleProvider, Provider
from app.config import Settings


def build_providers(settings: Settings, client: httpx.AsyncClient) -> list[Provider]:
    timeouts = {"connect_timeout": settings.ai_connect_timeout, "read_timeout": settings.ai_read_timeout}
    available: dict[str, Provider] = {
        "gateway": OpenAICompatibleProvider(
            name="gateway",
            label=f"AI Gateway ({settings.gateway_model})",
            base_url=settings.gateway_base_url,
            api_key=settings.gateway_api_key.get_secret_value(),
            model=settings.gateway_model,
            client=client,
            kind="gateway",
            enabled=settings.gateway_enabled,
            **timeouts,
        ),
        "local": OpenAICompatibleProvider(
            name="local",
            label=f"Local Gemma ({settings.local_llm_model})",
            base_url=settings.ollama_url.rstrip("/") + "/v1",
            api_key="",
            model=settings.local_llm_model,
            client=client,
            kind="ollama",
            enabled=settings.local_llm_enabled,
            **timeouts,
        ),
        "anthropic": AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.anthropic_model,
            client=client,
            base_url=settings.anthropic_base_url,
            **timeouts,
        ),
        "openai": OpenAICompatibleProvider(
            name="openai",
            label="ChatGPT API",
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
            client=client,
            kind="openai",
            **timeouts,
        ),
    }
    return [available[name] for name in settings.provider_order]
