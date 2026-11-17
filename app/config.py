"""Application settings.

Everything is configured through environment variables (or a local ``.env`` file).
All user-visible caps and rate limits live here so they can be tuned in one place.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRETS = {"", "change-me", "changeme", "__SECRET_KEY__", "please-change-me"}


def parse_rate(value: str) -> tuple[int, int]:
    """Parse ``"count/seconds"`` into a tuple."""
    count, _, seconds = value.partition("/")
    return max(1, int(count)), max(1, int(seconds or 60))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ core
    app_name: str = "Podcast Q&A Studio"
    app_env: Literal["development", "production", "test"] = "production"
    secret_key: SecretStr
    base_url: str = "http://localhost:8080"
    # Host names the app answers to (comma separated). IP-literal hosts are always
    # accepted because DNS-rebinding attacks need a domain name.
    allowed_hosts: str = "localhost"
    force_https: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    # --------------------------------------------------------------- storage
    database_url: str = "postgresql+asyncpg://podcastqa:podcastqa@db:5432/podcastqa"
    redis_url: str = "redis://redis:6379/0"
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # ------------------------------------------------------------ AI chat
    # Providers are tried in this order; the first healthy one answers.
    ai_provider_order: str = "gateway,local,anthropic,openai"
    gateway_enabled: bool = True
    gateway_base_url: str = "http://192.168.0.226:8000/v1"
    gateway_api_key: SecretStr = SecretStr("")
    gateway_model: str = "gemma"
    local_llm_enabled: bool = True
    ollama_url: str = "http://ollama:11434"
    local_llm_model: str = "gemma3:4b"
    local_embed_model: str = "embeddinggemma"
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_embed_model: str = "text-embedding-3-small"
    # Embeddings (pgvector) are produced by the first available of these.
    embedding_order: str = "local,openai,hash"
    ai_connect_timeout: float = 5.0
    ai_read_timeout: float = 180.0  # max silence between streamed chunks
    ai_job_timeout: float = 900.0
    ai_max_output_tokens: int = 1500
    ai_temperature: float = 0.7
    ai_circuit_cooldown: int = 45  # seconds a failing provider is skipped
    ai_health_interval: int = 20
    worker_concurrency: int = 8  # parallel AI jobs per worker process
    pdf_workers: int = 2  # process pool size for PDF rendering

    # ------------------------------------------------------------ voice
    tts_enabled: bool = True
    kokoro_url: str = "http://kokoro:8880"
    tts_default_voice: str = "af_heart"
    tts_default_speed: float = 0.95

    # ------------------------------------------------------------ email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "Podcast Q&A Studio <no-reply@localhost>"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    # Local single-user installs only: print emails (e.g. reset links) to the server log
    # when SMTP is not configured. Never enable on a shared/public server.
    email_fallback_to_log: bool = False

    # ------------------------------------------------------------ sessions
    session_idle_days: int = 7
    session_absolute_days: int = 30
    password_reset_minutes: int = 30
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65536

    # ------------------------------------------------------------ caps
    max_podcasts_per_user: int = 200
    max_questions_per_podcast: int = 60
    max_answers_per_question: int = 20
    max_revisions_per_question: int = 20
    max_instructions_per_podcast: int = 20
    max_exports_per_podcast: int = 5
    max_sessions_per_user: int = 20
    max_device_accounts: int = 5
    max_sockets_per_user: int = 10
    max_generate_batch: int = 20
    max_rewrite_batch: int = 20
    max_repeat_review_pairs: int = 20  # question pairs per AI repeat check
    max_reviews_per_podcast: int = 300  # AI repeat verdicts kept per podcast
    max_ai_jobs_per_user: int = 3
    max_body_bytes: int = 1_000_000
    max_tts_chars: int = 800

    # ------------------------------------------------------------ rate limits (count/seconds)
    rl_login_ip: str = "30/900"
    rl_login_account: str = "5/900"
    rl_signup_ip: str = "5/3600"
    rl_forgot_ip: str = "10/3600"
    rl_forgot_account: str = "3/3600"
    rl_reset_ip: str = "20/3600"
    rl_ai_hour: str = "40/3600"
    rl_ai_day: str = "300/86400"
    rl_tts: str = "120/600"
    rl_pdf: str = "10/600"
    rl_api_write: str = "300/60"
    rl_socket_events: str = "60/60"
    rl_account_password: str = "10/900"

    # ----------------------------------------------------------------- derived
    @model_validator(mode="after")
    def _check(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if self.app_env == "production" and (secret in _PLACEHOLDER_SECRETS or len(secret) < 32):
            raise ValueError("SECRET_KEY must be a random string of at least 32 characters in production")
        for name in self.provider_order:
            if name not in {"gateway", "local", "anthropic", "openai"}:
                raise ValueError(f"Unknown AI provider in AI_PROVIDER_ORDER: {name}")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def cookie_secure(self) -> bool:
        return self.force_https or self.base_url.lower().startswith("https://")

    @property
    def base_origin(self) -> str:
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def allowed_host_set(self) -> frozenset[str]:
        hosts = {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}
        base_host = urlsplit(self.base_url).hostname
        if base_host:
            hosts.add(base_host.lower())
        return frozenset(hosts)

    @property
    def provider_order(self) -> list[str]:
        return [p.strip().lower() for p in self.ai_provider_order.split(",") if p.strip()]

    @property
    def embedding_provider_order(self) -> list[str]:
        return [p.strip().lower() for p in self.embedding_order.split(",") if p.strip()]

    def rate(self, name: str) -> tuple[int, int]:
        return parse_rate(getattr(self, f"rl_{name}"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
