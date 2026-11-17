"""End-to-end smoke test against a RUNNING stack, using the real AI path.

    python -m loadtest.e2e_smoke http://<host>:<port>

Signs up, creates podcasts, connects a real WebSocket (Socket.IO), generates questions through
whatever AI is first available (gateway / local Gemma / ...), gets answer feedback, uses Kokoro
voice and exports a PDF. Prints timings. Creates one throwaway account.
"""

from __future__ import annotations

import asyncio
import sys
import time

from loadtest.common import User
from loadtest.sio import SocketClient


def step(name: str, started: float) -> None:
    print(f"  ok  {name:<46} {time.perf_counter() - started:7.1f}s", flush=True)


async def wait_job(sock: SocketClient, job_id: str, timeout: float) -> tuple[str, dict]:
    name, data = await sock.wait_for(lambda n, d: n in ("job:done", "job:error") and d.get("job_id") == job_id, timeout)
    return name, data


async def main(base_url: str) -> int:
    t0 = time.perf_counter()
    user = User(base_url, timeout=120)
    signed_up = await user.signup()
    assert signed_up.status_code == 303, (signed_up.status_code, signed_up.text[:300])
    step("sign up + Redis session", t0)
    me = (await user.http.get("/api/me")).json()
    assert me["username"] == user.username
    step(f"fetch name by user id -> {me['greeting']}", t0)

    status = (await user.http.get("/api/ai/status")).json()
    print("      AI providers:", ", ".join(f"{p['label']}={p['status']}" for p in status["providers"]), "| worker:", status["worker_alive"], "| voice:", status["voice"]["status"])

    sock = SocketClient(base_url, dict(user.http.cookies), user.csrf)
    assert await sock.connect(), f"socket refused: {sock.refused}"
    step("Socket.IO connected (cookie + CSRF + origin)", t0)

    host = await user.create_podcast("host", "Smoke test: climate founders", guest_name="Asha Rao", guest_bio="Runs a solar cold-storage startup in Pune", instructions="Avoid politics")
    podcast_id = User.podcast_id(host)
    step("create host podcast", t0)

    response = await user.http.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 3, "focus": "the early days"}, headers=user.headers())
    assert response.status_code == 202, response.text
    job_id = response.json()["job"]["id"]
    name, data = await wait_job(sock, job_id, 900)
    deltas = sum(1 for n, d in sock.events if n == "job:delta" and d.get("job_id") == job_id)
    added = [d["question"]["text"] for n, d in sock.events if n == "job:question" and d.get("job_id") == job_id]
    assert name == "job:done", data
    step(f"AI generated {len(added)} questions via {data.get('provider')} ({deltas} stream events)", t0)
    for text in added:
        print("        -", text)

    state = (await user.http.get(f"/api/podcasts/{podcast_id}")).json()
    print(f"      plan: {state['plan']['planned_label']} of {state['plan']['target_label']} ({state['plan']['status']})")
    check = (await user.http.post(f"/api/podcasts/{podcast_id}/quality-check", json={}, headers=user.headers())).json()
    step(f"quality + redundancy check {check['summary']}", t0)

    guest = User.podcast_id(await user.create_podcast("guest", "Smoke test: guest prep", talking_points="Cold storage cuts crop loss by 30 percent"))
    question = (await user.http.post(f"/api/podcasts/{guest}/questions", json={"text": "Why did you start your company?", "est_seconds": 90}, headers=user.headers())).json()["question"]
    answer = await user.http.post(
        f"/api/questions/{question['id']}/answers",
        json={"text": "I started it after watching farmers near Pune lose a whole season of tomatoes in a heatwave. I knew cheap solar cold rooms could fix that, so I built the first one in my uncle's yard.", "rehearsed_seconds": 25, "feedback": True},
        headers=user.headers(),
    )
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["job"], body.get("job_error")
    name, data = await wait_job(sock, body["job"]["id"], 900)
    assert name == "job:done", data
    feedback = next(d["answer"] for n, d in sock.events if n == "job:feedback")
    step(f"answer feedback: score {feedback['score']}/10 via {feedback['provider']}", t0)
    print("        improve:", (feedback["feedback"] or {}).get("improve"))

    tts = await user.http.post("/api/tts", json={"text": "Hi, I'm Mira. Why did you start your company?"}, headers={"X-CSRF-Token": user.csrf})
    print(f"      TTS: HTTP {tts.status_code}, {tts.headers.get('content-type')}, {len(tts.content)} bytes")
    step("Kokoro voice", t0)

    export = (await user.http.post(f"/api/podcasts/{podcast_id}/export", json={}, headers={"X-CSRF-Token": user.csrf})).json()["export"]
    pdf = await user.http.get(export["url"])
    assert pdf.content.startswith(b"%PDF")
    step(f"PDF export stored + downloaded ({len(pdf.content)} bytes)", t0)

    await sock.close()
    await user.close()
    print(f"\nSMOKE TEST PASSED in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080")))
