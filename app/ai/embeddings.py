"""Embeddings for pgvector: local EmbeddingGemma (Ollama) -> OpenAI -> hashing fallback.

Every stored vector records which model produced it; vectors are only ever compared with
vectors from the same model, so mixing providers can never produce nonsense similarities.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.ai.textsim import hash_embed, normalize
from app.config import Settings
from app.models import EMBED_DIM

log = logging.getLogger(__name__)

HASH_MODEL = "hash:v1"


class EmbeddingError(Exception):
    pass


class Embedder:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._local_ok_until = 0.0
        self._local_bad_until = 0.0

    @property
    def local_model_id(self) -> str:
        return f"ollama:{self.settings.local_embed_model}"

    @property
    def openai_model_id(self) -> str:
        return f"openai:{self.settings.openai_embed_model}@{EMBED_DIM}"

    async def _local_available(self) -> bool:
        now = time.monotonic()
        if now < self._local_bad_until:
            return False
        if now < self._local_ok_until:
            return True
        model = self.settings.local_embed_model
        try:
            resp = await self.client.get(
                f"{self.settings.ollama_url.rstrip('/')}/api/tags", timeout=httpx.Timeout(4.0, connect=2.0)
            )
            names = set()
            if resp.status_code == 200:
                for item in resp.json().get("models", []):
                    names.update({item.get("name", ""), item.get("model", "")})
            ok = model in names or f"{model}:latest" in names
        except (httpx.HTTPError, ValueError):
            ok = False
        if ok:
            self._local_ok_until = now + 60
        else:
            self._local_bad_until = now + 30
        return ok

    async def pick_model(self) -> str:
        for name in self.settings.embedding_provider_order:
            if name == "local" and self.settings.local_llm_enabled and await self._local_available():
                return self.local_model_id
            if name == "openai" and self.settings.openai_api_key.get_secret_value():
                return self.openai_model_id
            if name == "hash":
                return HASH_MODEL
        return HASH_MODEL

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not texts:
            return []
        if model == HASH_MODEL:
            return [hash_embed(t) for t in texts]
        vectors: list[list[float]] = []
        if model.startswith("ollama:"):
            name = model.split(":", 1)[1]
            prefix = "task: sentence similarity | query: " if "embeddinggemma" in name else ""
            for start in range(0, len(texts), 32):
                batch = [prefix + t for t in texts[start : start + 32]]
                resp = await self.client.post(
                    f"{self.settings.ollama_url.rstrip('/')}/api/embed",
                    json={"model": name, "input": batch, "truncate": True},
                    timeout=httpx.Timeout(90.0, connect=3.0),
                )
                if resp.status_code != 200:
                    raise EmbeddingError(f"Ollama HTTP {resp.status_code}")
                data = resp.json().get("embeddings") or []
                if len(data) != len(batch):
                    raise EmbeddingError("Ollama returned the wrong number of embeddings")
                vectors.extend(data)
        elif model.startswith("openai:"):
            resp = await self.client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/embeddings",
                json={"model": self.settings.openai_embed_model, "input": texts, "dimensions": EMBED_DIM},
                headers={"Authorization": f"Bearer {self.settings.openai_api_key.get_secret_value()}"},
                timeout=httpx.Timeout(60.0, connect=5.0),
            )
            if resp.status_code != 200:
                raise EmbeddingError(f"OpenAI HTTP {resp.status_code}")
            rows = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
            vectors = [row["embedding"] for row in rows]
            if len(vectors) != len(texts):
                raise EmbeddingError("OpenAI returned the wrong number of embeddings")
        else:
            raise EmbeddingError(f"Unknown embedding model {model}")
        out = []
        for vector in vectors:
            if len(vector) < EMBED_DIM:
                raise EmbeddingError(f"Embedding has {len(vector)} dimensions, expected {EMBED_DIM}")
            # Matryoshka-style truncation is valid for EmbeddingGemma and text-embedding-3.
            out.append(normalize([float(v) for v in vector[:EMBED_DIM]]))
        return out

    async def embed_best(self, texts: list[str]) -> tuple[str, list[list[float]]]:
        model = await self.pick_model()
        if model == HASH_MODEL:
            return model, await self.embed(texts, model)
        try:
            return model, await self.embed(texts, model)
        except (httpx.HTTPError, EmbeddingError, KeyError, ValueError) as exc:
            log.warning("Embedding with %s failed (%s); using hashing fallback", model, exc)
            if model.startswith("ollama:"):
                self._local_bad_until = time.monotonic() + 30
            return HASH_MODEL, [hash_embed(t) for t in texts]
