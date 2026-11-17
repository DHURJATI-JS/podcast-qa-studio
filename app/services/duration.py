"""Run-time budgeting: every podcast has a target length, every question a time slot."""

from __future__ import annotations

SPEAKING_WPM_DEFAULT = 150
ASK_SECONDS = 10  # time the host needs to ask a question


def fmt_duration(seconds: int | float | None) -> str:
    total = max(0, int(round(seconds or 0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def plan_summary(target_minutes: int, intro_seconds: int, outro_seconds: int, question_seconds: list[int]) -> dict:
    target = int(target_minutes) * 60
    questions_total = sum(int(s) for s in question_seconds)
    planned = int(intro_seconds) + questions_total + int(outro_seconds)
    if planned < target * 0.9:
        status = "under"
    elif planned > target * 1.05:
        status = "over"
    else:
        status = "on_track"
    return {
        "target_seconds": target,
        "planned_seconds": planned,
        "questions_seconds": questions_total,
        "intro_seconds": int(intro_seconds),
        "outro_seconds": int(outro_seconds),
        "remaining_seconds": max(0, target - planned),
        "over_seconds": max(0, planned - target),
        "question_count": len(question_seconds),
        "status": status,
        "target_label": fmt_duration(target),
        "planned_label": fmt_duration(planned),
    }


def suggest_count(remaining_seconds: int, max_batch: int) -> int:
    if remaining_seconds <= 0:
        return 3
    return max(1, min(8, max_batch, round(remaining_seconds / 150)))


def speaking_seconds(words: int, wpm: int) -> int:
    return int(round(words * 60 / max(80, int(wpm or SPEAKING_WPM_DEFAULT))))


def answer_target_words(slot_seconds: int, wpm: int) -> int:
    return max(25, int(round(max(20, int(slot_seconds) - ASK_SECONDS) * max(80, int(wpm)) / 60)))
