"""Text cleaning so that nothing a user (or an AI model) types can ever execute as code.

Defence in depth:
1. On the way IN, every free-text value goes through :func:`clean_text`, which strips HTML
   tags (including ``<script>``/``<style>`` *content*), control characters, invisible
   bidi/zero-width characters and normalises whitespace. Text is stored as plain text.
2. On the way OUT, Jinja2 autoescaping, ``textContent`` in the browser and XML-escaping in
   the PDF builder make sure stored text is always rendered as text.
3. A strict nonce-based Content-Security-Policy blocks any inline script that slipped through.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

import nh3

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb]")
_TAG_HINT = re.compile(r"<\s*[a-zA-Z!/?]")
_ENCODED_TAG = re.compile(r"&(?:lt|#0*60|#x0*3c);", re.IGNORECASE)
_SPACES = re.compile("[ \t\f\v\u00a0]+")
_MULTI_NL = re.compile(r"\n{3,}")
_DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "textarea", "title", "svg", "math"}
_WORD = re.compile(r"[\w'’-]+", re.UNICODE)
_REPEAT_CHAR = re.compile(r"(.)\1{14,}", re.DOTALL)
_NON_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)


def clean_text(value: object, *, max_len: int, multiline: bool = False) -> str:
    """Return a safe plain-text version of ``value``."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _INVISIBLE.sub("", text)
    # Entity-encoded markup (&lt;script&gt;) is decoded first so it is stripped like real tags.
    if _ENCODED_TAG.search(text):
        text = html.unescape(text)
    # Strip markup (loop: decoding can reveal another layer of tags).
    for _ in range(3):
        if not _TAG_HINT.search(text):
            break
        text = html.unescape(
            nh3.clean(text, tags=set(), clean_content_tags=_DROP_CONTENT, strip_comments=True)
        )
        text = _CONTROL.sub("", text)
    if multiline:
        text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
        text = _MULTI_NL.sub("\n\n", text)
    else:
        text = " ".join(text.split())
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def strip_markdown(text: str) -> str:
    """Remove the light markdown decoration small models like to add."""
    text = re.sub(r"\*\*|__|`+", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text


def normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_compare(text).encode("utf-8")).hexdigest()


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def spam_reason(text: str, *, min_words: int = 3) -> str | None:
    """Cheap checks that stop junk being sent to the AI (AI spam protection)."""
    words = _WORD.findall(text or "")
    if len(words) < min_words:
        return f"Please write at least {min_words} words."
    if _REPEAT_CHAR.search(text):
        return "That looks like repeated characters rather than a real answer."
    letters = sum(ch.isalpha() for ch in text)
    if letters < len(text) * 0.4:
        return "That doesn't look like a real answer."
    if len(words) >= 12 and len({w.lower() for w in words}) / len(words) < 0.2:
        return "That looks like the same words repeated."
    return None


def slugify(text: str, *, max_len: int = 60, fallback: str = "podcast") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].strip("-")) or fallback


def safe_next(url: str | None, default: str = "/dashboard") -> str:
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not url or len(url) > 500:
        return default
    if not url.startswith("/") or url.startswith("//") or url.startswith("/\\"):
        return default
    if any(ch in url for ch in ("\\", "\r", "\n", "\t")) or _CONTROL.search(url):
        return default
    return url
