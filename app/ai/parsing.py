"""Forgiving, streaming-friendly parsers for model output.

Small local models do not always follow formats perfectly, so these parsers accept common
variations (markdown bold, different separators, missing numbers) and drop anything that is
not a plausible question. All parsed text is passed through :func:`clean_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.security.sanitize import clean_text, strip_markdown, word_count

CATEGORY_ALIASES = {
    "opener": "opener", "opening": "opener", "intro": "opener", "introduction": "opener", "icebreaker": "opener",
    "warm up": "opener", "warm-up": "opener", "warmup": "opener",
    "background": "background", "origin": "background", "backstory": "background", "context": "background",
    "core": "core", "deep dive": "core", "deep-dive": "core", "deepdive": "core", "main": "core", "topic": "core",
    "insight": "core", "expertise": "core",
    "story": "story", "personal": "story", "anecdote": "story", "storytelling": "story",
    "challenge": "challenge", "challenging": "challenge", "tough": "challenge", "hard": "challenge",
    "controversial": "challenge", "debate": "challenge",
    "rapid fire": "rapid_fire", "rapid-fire": "rapid_fire", "rapid_fire": "rapid_fire", "rapidfire": "rapid_fire",
    "lightning": "rapid_fire", "quickfire": "rapid_fire", "quick fire": "rapid_fire", "quick": "rapid_fire",
    "audience": "audience", "listener": "audience", "listeners": "audience", "practical": "audience",
    "advice": "audience", "takeaway": "audience",
    "closing": "closing", "close": "closing", "closer": "closing", "outro": "closing", "wrap up": "closing",
    "wrap-up": "closing", "wrapup": "closing", "final": "closing", "ending": "closing",
    "follow up": "follow_up", "follow-up": "follow_up", "follow_up": "follow_up", "followup": "follow_up",
}
DEFAULT_SECONDS = {
    "opener": 90, "background": 150, "core": 180, "story": 180, "challenge": 150,
    "rapid_fire": 45, "audience": 120, "closing": 90, "follow_up": 120,
}
_IMPERATIVE_STARTS = (
    "tell ", "describe ", "walk ", "share ", "explain ", "talk ", "take us", "give us", "paint ", "help us",
    "imagine ", "picture ", "finish ", "complete ",
)

_LINE = re.compile(
    r"""^\s*(?:[-*•>]\s*)?
        (?:(?:Q(?:uestion)?\s*)?(?P<ref>\d{1,3})\s*[.):\]\-]{0,2}\s*)?
        [\[(]\s*(?P<cat>[A-Za-z][A-Za-z _/\-]{1,24}?)\s*[|,/;:]\s*
        (?P<num>\d{1,4})\s*(?P<unit>s|sec|secs|seconds|m|min|mins|minutes)?\s*[\])]
        \s*[:\-–—]?\s*(?P<text>.+?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_PLAIN = re.compile(
    r"^\s*(?:[-*•>]\s*)?(?:(?:Q(?:uestion)?\s*)?(?P<ref>\d{1,3})\s*[.):\]\-]{1,2}\s*)?(?P<text>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedQuestion:
    text: str
    category: str
    seconds: int
    ref: int | None = None


def normalize_category(raw: str | None, default: str = "core") -> str:
    key = " ".join((raw or "").lower().replace("_", " ").split())
    return CATEGORY_ALIASES.get(key) or CATEGORY_ALIASES.get(key.replace(" ", "")) or default


def _looks_like_question(text: str) -> bool:
    lower = text.lower()
    if word_count(text) < 4:
        return False
    if lower.endswith((":", ";")):
        return False
    return text.rstrip().endswith("?") or lower.startswith(_IMPERATIVE_STARTS)


def _clean_question(text: str) -> str:
    text = strip_markdown(text).strip().strip("\"'“”‘’").strip()
    return clean_text(text, max_len=500)


def parse_question_line(line: str, *, require_ref: bool = False) -> ParsedQuestion | None:
    raw = strip_markdown(line or "").strip()
    if len(raw) < 8 or len(raw) > 1200:
        return None
    match = _LINE.match(raw)
    if match:
        ref = int(match.group("ref")) if match.group("ref") else None
        category = normalize_category(match.group("cat"))
        seconds = int(match.group("num"))
        unit = (match.group("unit") or "").lower()
        if unit.startswith("m") or (not unit and seconds <= 15 and category != "rapid_fire"):
            seconds *= 60  # "[core|3]" almost always means minutes
        text = match.group("text")
    else:
        plain = _PLAIN.match(raw)
        if not plain:
            return None
        ref = int(plain.group("ref")) if plain.group("ref") else None
        text = plain.group("text")
        if not _looks_like_question(text):
            return None
        category, seconds = "core", 0
    if require_ref and ref is None:
        return None
    text = _clean_question(text)
    if not text or word_count(text) < 3 or text.lower().startswith("format example"):
        return None
    seconds = seconds or DEFAULT_SECONDS.get(category, 150)
    return ParsedQuestion(text=text, category=category, seconds=max(20, min(900, seconds)), ref=ref)


class QuestionStreamParser:
    """Feed streamed text; get complete questions as soon as each line ends."""

    MAX_LINE = 2000

    def __init__(self, *, require_ref: bool = False) -> None:
        self.require_ref = require_ref
        self.buffer = ""

    def feed(self, chunk: str) -> list[ParsedQuestion]:
        self.buffer += chunk
        out: list[ParsedQuestion] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            item = parse_question_line(line, require_ref=self.require_ref)
            if item:
                out.append(item)
        if len(self.buffer) > self.MAX_LINE:
            self.buffer = ""
        return out

    def flush(self) -> list[ParsedQuestion]:
        line, self.buffer = self.buffer, ""
        item = parse_question_line(line, require_ref=self.require_ref)
        return [item] if item else []

    def reset(self) -> None:
        self.buffer = ""


def first_question(text: str) -> ParsedQuestion | None:
    for line in (text or "").splitlines():
        item = parse_question_line(line)
        if item:
            return item
    return None


_HEADINGS = {
    "score": "score",
    "worked": "worked", "what worked": "worked", "strength": "worked", "strengths": "worked",
    "improve": "improve", "improvement": "improve", "improvements": "improve", "to improve": "improve",
    "what to improve": "improve",
    "stronger answer": "stronger_answer", "improved answer": "stronger_answer", "better answer": "stronger_answer",
    "suggested answer": "stronger_answer", "rewritten answer": "stronger_answer",
    "timing": "timing", "length": "timing",
    "follow-up": "follow_up", "follow up": "follow_up", "followup": "follow_up",
    "follow-up question": "follow_up", "follow up question": "follow_up",
}
_HEAD_RE = re.compile(
    r"^\s*[#>*\-\s]*(?P<head>" + "|".join(sorted((re.escape(h) for h in _HEADINGS), key=len, reverse=True)) + r")"
    r"\s*(?:[:\-–—]\s*(?P<rest>.*)|$)",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s*")
_QUOTES = "\"'\u201c\u201d\u2018\u2019 "
_SCORE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:/\s*10|out of 10)?")


def parse_feedback(text: str) -> dict:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in (text or "").splitlines():
        line = strip_markdown(raw_line).rstrip()
        match = _HEAD_RE.match(line)
        if match:
            current = _HEADINGS[match.group("head").lower()]
            sections.setdefault(current, [])
            rest = (match.group("rest") or "").strip()
            if rest:
                sections[current].append(rest)
            continue
        if current:
            sections[current].append(line)

    def bullets(key: str) -> list[str]:
        items = []
        for line in sections.get(key, []):
            item = _BULLET.sub("", line).strip()
            if item:
                items.append(clean_text(item, max_len=400))
        return [i for i in items if i][:6]

    def paragraph(key: str, limit: int) -> str:
        return clean_text("\n".join(sections.get(key, [])).strip(), max_len=limit, multiline=True)

    score = None
    for line in sections.get("score", []):
        found = _SCORE.search(line)
        if found:
            score = max(0, min(10, round(float(found.group(1)))))
            break
    result = {
        "score": score,
        "worked": bullets("worked"),
        "improve": bullets("improve"),
        "stronger_answer": paragraph("stronger_answer", 3000),
        "timing": clean_text(" ".join(sections.get("timing", [])), max_len=400),
        "follow_up": clean_text(" ".join(_BULLET.sub("", l) for l in sections.get("follow_up", [])), max_len=400).strip(_QUOTES),
    }
    result["parsed"] = bool(result["worked"] or result["improve"] or result["stronger_answer"] or score is not None)
    if not result["parsed"]:
        result["raw"] = clean_text(text, max_len=4000, multiline=True)
    return result


_VERDICT = re.compile(r"\b(same|different)\b", re.I)


def parse_verdict(text: str) -> bool | None:
    """'SAME' -> True, 'DIFFERENT' -> False (first such word wins, any case or decoration); None if
    the reply contains neither."""
    match = _VERDICT.search(text or "")
    return None if match is None else match.group(1).lower() == "same"
