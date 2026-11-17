"""Model-independent text similarity + a deterministic hashing embedder (always-available fallback)."""

from __future__ import annotations

import hashlib
import math
import re
from difflib import SequenceMatcher

from app.models import EMBED_DIM
from app.security.sanitize import normalize_for_compare

STOP_WORDS = frozenset(
    """a an the and or but if then else of to in on for with about as at by from into onto over under
    is are was were be been being am do does did done have has had having i you he she it we they me him her
    us them my your his its our their this that these those what which who whom whose when where why how
    can could would should will shall may might must just so very really also too not no yes tell please
    let lets let's more most some any much many there here than s t""".split()
)
_SUFFIXES = ("ations", "ation", "ments", "ment", "ingly", "edly", "ing", "ers", "er", "ies", "es", "ed", "ly", "s")


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def content_tokens(text: str) -> list[str]:
    return [stem(w) for w in normalize_for_compare(text).split() if w not in STOP_WORDS and len(w) > 1]


def lexical_similarity(a: str, b: str) -> float:
    """Wording overlap (0..1) on *content* words, so "first job" vs "first car" stay distinct."""
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return 0.95  # same content words, different filler or order
    jaccard = len(sa & sb) / len(sa | sb)
    words = SequenceMatcher(None, ta, tb, autojunk=False).ratio()
    # Character-level match only for near-identical strings (typos, punctuation).
    chars = SequenceMatcher(None, na, nb, autojunk=False).ratio() if max(len(na), len(nb)) <= 600 else 0.0
    return max(jaccard, words, chars if chars >= 0.95 else 0.0)


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(math.sumprod(vector, vector)) or 1.0
    return [v / norm for v in vector]


def as_list(vector: object) -> list[float] | None:
    if vector is None:
        return None
    if hasattr(vector, "to_list"):
        return [float(v) for v in vector.to_list()]  # type: ignore[attr-defined]
    if hasattr(vector, "tolist"):
        return [float(v) for v in vector.tolist()]  # type: ignore[attr-defined]
    return [float(v) for v in vector]  # type: ignore[union-attr]


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if a is None or b is None or len(a) != len(b):
        return None
    na = math.sqrt(math.sumprod(a, a))
    nb = math.sqrt(math.sumprod(b, b))
    if not na or not nb:
        return None
    return math.sumprod(a, b) / (na * nb)


def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Feature-hashing embedding of stemmed words + word pairs. Deterministic, no network."""
    words = content_tokens(text)
    features = [(w, 1.0) for w in words] + [(f"{a}_{b}", 0.7) for a, b in zip(words, words[1:])]
    vector = [0.0] * dim
    for feature, weight in features:
        h = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
        vector[h % dim] += weight if (h >> 63) & 1 else -weight
    if not any(vector):
        vector[0] = 1.0
    return normalize(vector)


def thresholds(model: str) -> tuple[float, float]:
    """(duplicate, similar) cosine thresholds per embedding family."""
    if model.startswith("ollama:"):
        return 0.90, 0.80
    if model.startswith("openai:"):
        return 0.88, 0.72
    return 0.85, 0.62


REVIEW_BAND = 0.12


def review_floor(model: str) -> float:
    """Lowest cosine similarity that still gets an AI repeat check. Embeddings alone can't settle
    this band: with EmbeddingGemma, reworded repeats scored 0.71-0.76 while different questions on
    the same topic scored up to 0.78 - so pairs between this floor and the "similar" threshold are
    asked of the AI instead."""
    return round(thresholds(model)[1] - REVIEW_BAND, 3)
