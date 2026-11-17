"""A fake LLM server for stress tests - no GPU, deterministic, fast.

Speaks both protocols the app uses:
* OpenAI-compatible ``/v1/chat/completions`` (stream) + ``/v1/models``
* Anthropic ``/v1/messages`` (stream) + ``/v1/models``

It reads the prompt to decide what to answer (questions / rewrites / feedback / one question)
and can inject failures (``FAIL_RATE``, before or mid-stream) to exercise the fallback chain.
Run: ``uvicorn loadtest.fake_llm:app --port 9000``
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.1"))
CHUNK_DELAY = float(os.environ.get("CHUNK_DELAY", "0.004"))
STATS = {"openai": 0, "anthropic": 0, "failures": 0}
TOPICS = [
    "pricing", "hiring", "fundraising", "first customer", "burnout", "competition", "mentors", "failure",
    "regulation", "supply chain", "remote teams", "brand", "storytelling", "metrics", "culture", "exit",
]
VERBS = ["shape", "change", "surprise", "challenge", "teach", "test", "push", "slow"]


def unique_question(i: int) -> str:
    topic = random.choice(TOPICS)
    return f"How did {topic} {random.choice(VERBS)} you in year {random.randint(1, 9)} - case {uuid.uuid4().hex[:6]}?"


def reply_for(prompt: str) -> str:
    if "Are A and B the same question?" in prompt:  # the AI repeat check: one word per pair
        return random.choice(("SAME", "DIFFERENT"))
    if "Rewrite every question" in prompt:
        refs = sorted({int(n) for n in re.findall(r"^Q(\d+) \[", prompt, re.MULTILINE)})
        return "\n".join(f"Q{n} [core|90] Shorter take {uuid.uuid4().hex[:5]}: what changed for you?" for n in refs)
    if "Reply in exactly this format" in prompt:
        return (
            f"SCORE: {random.randint(4, 9)}\nWORKED:\n- Clear hook\n- Honest tone\nIMPROVE:\n- Add a number\n- Cut the middle\n"
            "- End with a takeaway\nSTRONGER ANSWER:\nI started after one hard summer taught me what farmers lose.\n"
            "TIMING: Fits the slot.\nFOLLOW-UP: What did that summer cost you?"
        )
    if "Output exactly one line" in prompt:
        return f"[follow_up|120] {unique_question(0)}"
    match = re.search(r"Output exactly (\d+) lines", prompt)
    count = int(match.group(1)) if match else 3
    return "Here are your questions:\n" + "\n".join(f"{i}. [core|120] {unique_question(i)}" for i in range(1, count + 1))


def chunks(text: str) -> list[str]:
    return [text[i : i + 6] for i in range(0, len(text), 6)]


def failure_plan() -> str | None:
    roll = random.random()
    if roll < FAIL_RATE / 2:
        return "before"
    if roll < FAIL_RATE:
        return "mid"
    return None


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "gemma", "object": "model"}, {"id": "fake-claude", "object": "model"}]}


@app.get("/stats")
async def stats():
    return STATS


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATS["openai"] += 1
    plan = failure_plan()
    if plan == "before":
        STATS["failures"] += 1
        return JSONResponse({"error": {"message": "fake overload"}}, status_code=503)
    prompt = "\n".join(m.get("content", "") for m in body.get("messages", []))
    pieces = chunks(reply_for(prompt))

    async def stream():
        yield 'data: {"choices":[{"delta":{"role":"assistant","content":null},"index":0}]}\n\n'
        for n, piece in enumerate(pieces):
            if plan == "mid" and n == len(pieces) // 2:
                STATS["failures"] += 1
                raise RuntimeError("fake mid-stream crash")
            await asyncio.sleep(CHUNK_DELAY)
            yield "data: " + json.dumps({"choices": [{"delta": {"content": piece}, "index": 0}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    STATS["anthropic"] += 1
    if request.headers.get("anthropic-version") != "2023-06-01" or not request.headers.get("x-api-key"):
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "bad headers"}}, status_code=401)
    prompt = body.get("system", "") + "\n" + "\n".join(m.get("content", "") for m in body.get("messages", []))
    pieces = chunks(reply_for(prompt))

    async def stream():
        def event(name: str, data: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(data)}\n\n"

        yield event("message_start", {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant", "content": []}})
        yield event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        yield event("ping", {"type": "ping"})
        for piece in pieces:
            await asyncio.sleep(CHUNK_DELAY)
            yield event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
        yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": len(pieces)}})
        yield event("message_stop", {"type": "message_stop"})

    return StreamingResponse(stream(), media_type="text/event-stream")
