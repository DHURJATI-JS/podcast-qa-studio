"""Stress test: many concurrent users exercising every feature at once, then invariant checks.

    python -m loadtest.stress http://app:8000          (inside docker-compose.stress.yml)

Run it only against the throwaway stress stack - it creates many accounts.
"""

from __future__ import annotations

import asyncio
import collections
import os
import random
import statistics
import sys
import time
import uuid

import httpx

from loadtest.common import User
from loadtest.sio import SocketClient

USERS = int(os.environ.get("STRESS_USERS", "40"))
CAP_QUESTIONS = int(os.environ.get("CAP_QUESTIONS", "60"))
TOPICS = ["pricing", "hiring", "fundraising", "burnout", "mentors", "failure", "regulation", "culture", "metrics", "brand"]


class Stats:
    def __init__(self) -> None:
        self.latency: dict[str, list[float]] = collections.defaultdict(list)
        self.codes: collections.Counter = collections.Counter()
        self.problems: list[str] = []
        self.jobs: collections.Counter = collections.Counter()
        self.job_seconds: list[float] = []
        self.providers: collections.Counter = collections.Counter()
        self.resets = 0

    def problem(self, message: str) -> None:
        self.problems.append(message)
        print("  !! " + message[:300], flush=True)


S = Stats()
REGISTRY: dict[str, dict] = {}  # username -> {"podcast": id, "question": id}


async def timed(name: str, coro, expect: set[int] | None = None) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await coro
    except Exception as exc:  # noqa: BLE001
        S.latency[name].append(time.perf_counter() - started)
        S.codes[(name, "EXC")] += 1
        S.problem(f"{name}: {type(exc).__name__}: {exc}")
        return None
    S.latency[name].append(time.perf_counter() - started)
    S.codes[(name, response.status_code)] += 1
    if response.status_code >= 500:
        S.problem(f"{name}: HTTP {response.status_code}: {response.text[:200]}")
    elif expect and response.status_code not in expect:
        S.problem(f"{name}: expected {sorted(expect)} got {response.status_code}: {response.text[:200]}")
    return response


async def wait_job(sock: SocketClient, job_id: str, label: str, timeout: float = 300) -> dict | None:
    started = time.perf_counter()
    try:
        name, data = await sock.wait_for(lambda n, d: n in ("job:done", "job:error") and d.get("job_id") == job_id, timeout)
    except TimeoutError:
        S.problem(f"{label}: job {job_id} did not finish within {timeout}s")
        S.jobs["timeout"] += 1
        return None
    S.job_seconds.append(time.perf_counter() - started)
    S.jobs[name] += 1
    S.providers[data.get("provider") or "-"] += 1
    S.resets += sum(1 for n, d in sock.events if n == "job:reset" and d.get("job_id") == job_id)
    if name == "job:error":
        S.problem(f"{label}: job failed: {data.get('message')}")
        return None
    if not any(n == "job:delta" and d.get("job_id") == job_id for n, d in sock.events):
        S.problem(f"{label}: no streamed tokens arrived for job {job_id}")
    return data


async def journey(base: str, n: int) -> None:
    user = User(base, timeout=120)
    try:
        await asyncio.sleep(random.random() * 3)
        signed = await timed("signup", user.signup(), {303})
        if not signed or signed.status_code != 303:
            return
        me = await timed("GET /api/me", user.http.get("/api/me"), {200})
        if me and me.json()["username"] != user.username:
            S.problem("api/me returned another user's profile!")
        sock = SocketClient(base, dict(user.http.cookies), user.csrf)
        if not await sock.connect():
            S.problem(f"socket refused: {sock.refused}")
            return

        host_resp = await timed("create podcast", user.create_podcast("host", f"Host show {n}", instructions="Avoid politics"), {303})
        guest_resp = await timed("create podcast", user.create_podcast("guest", f"Guest prep {n}", talking_points="Point A"), {303})
        if not host_resp or not guest_resp:
            return
        host, guest = User.podcast_id(host_resp), User.podcast_id(guest_resp)

        texts = [f"What did year {i} teach you about {TOPICS[(i + n) % len(TOPICS)]} and {uuid.uuid4().hex[:4]}?" for i in range(1, 9)]
        await asyncio.gather(
            *(timed("add question", user.http.post(f"/api/podcasts/{host}/questions", json={"text": t}, headers=user.headers()), {201}) for t in texts)
        )
        dup = await timed("add question (dup)", user.http.post(f"/api/podcasts/{host}/questions", json={"text": texts[0]}, headers=user.headers()), {201})
        if dup and (dup.json()["question"]["flags"].get("match") or {}).get("kind") != "duplicate":
            S.problem("duplicate question was not flagged as a repeat")

        state = (await user.http.get(f"/api/podcasts/{host}")).json()
        questions = state["questions"]
        positions = [q["position"] for q in questions]
        if positions != list(range(1, len(questions) + 1)):
            S.problem(f"positions not contiguous after concurrent adds: {positions}")
        REGISTRY[user.username] = {"podcast": host, "question": questions[0]["id"]}

        await timed("PATCH question", user.http.patch(f"/api/questions/{questions[1]['id']}", json={"text": "Reworded: " + questions[1]["text"]}, headers=user.headers()), {200})
        ids = [q["id"] for q in questions]
        random.shuffle(ids)
        await timed("reorder", user.http.post(f"/api/podcasts/{host}/questions/reorder", json={"ids": ids}, headers=user.headers()), {200})
        await timed("quality check", user.http.post(f"/api/podcasts/{host}/quality-check", json={}, headers=user.headers()), {200})
        await timed("undo", user.http.post(f"/api/questions/{questions[1]['id']}/undo", json={}, headers=user.headers()), {200})
        await timed("GET workspace", user.http.get(f"/podcasts/{host}"), {200})
        await timed("GET dashboard", user.http.get("/dashboard"), {200})

        # --- double submit: same Idempotency-Key 4x at once -> exactly one job
        key = uuid.uuid4().hex
        same = await asyncio.gather(
            *(
                timed("generate (same key x4)", user.http.post(f"/api/podcasts/{host}/ai/generate", json={"count": 4}, headers={"X-CSRF-Token": user.csrf, "Idempotency-Key": key}), {202, 409})
                for _ in range(4)
            )
        )
        accepted = [r for r in same if r is not None and r.status_code == 202]
        if len(accepted) != 1:
            S.problem(f"double submit accepted {len(accepted)} times")
            return
        await wait_job(sock, accepted[0].json()["job"]["id"], "generate")

        # --- per-podcast lock: 4 *different* keys at the same moment -> exactly one job, others "busy"
        racing = await asyncio.gather(
            *(timed("generate (4 at once)", user.http.post(f"/api/podcasts/{host}/ai/generate", json={"count": 2}, headers=user.headers()), {202, 409}) for _ in range(4))
        )
        won = [r for r in racing if r is not None and r.status_code == 202]
        lost = [r for r in racing if r is not None and r.status_code == 409]
        if not won:
            S.problem("no generate request won the race")
        if any(r.json().get("code") != "busy" for r in lost):
            S.problem("busy response has the wrong code")
        # A fast job may finish before a delayed request arrives (then a 2nd job is fine), but two
        # jobs on one podcast must never *run* at the same time.
        for r in won:
            await wait_job(sock, r.json()["job"]["id"], "generate (race)")
        windows = []
        for r in won:
            job = (await user.http.get(f"/api/jobs/{r.json()['job']['id']}")).json()["job"]
            windows.append((job["started_at"], job["finished_at"]))
        windows.sort()
        for (s1, f1), (s2, f2) in zip(windows, windows[1:]):
            if s2 < f1:
                S.problem(f"per-podcast AI lock violated: jobs overlapped {s1}..{f1} and {s2}..{f2}")

        ids = [q["id"] for q in (await user.http.get(f"/api/podcasts/{host}")).json()["questions"]][:3]
        rewrite = await timed("rewrite", user.http.post(f"/api/podcasts/{host}/ai/rewrite", json={"question_ids": ids, "instruction": "make them shorter", "remember": True}, headers=user.headers()), {202})
        if rewrite is not None and rewrite.status_code == 202:
            await wait_job(sock, rewrite.json()["job"]["id"], "rewrite")

        # --- guest: answer feedback (+ concurrent duplicate submit) and a follow-up question
        added = await timed("add question", user.http.post(f"/api/podcasts/{guest}/questions", json={"text": "Why did you start your company?"}, headers=user.headers()), {201})
        if added is None or added.status_code != 201:
            return
        q = added.json()["question"]
        answer_key = uuid.uuid4().hex
        body = {"text": f"I started after a heatwave wiped out a season of crops near Pune, and I knew cold storage could help. {n}", "feedback": True, "rehearsed_seconds": 30}
        answers = await asyncio.gather(
            *(timed("answer (same key x3)", user.http.post(f"/api/questions/{q['id']}/answers", json=body, headers={"X-CSRF-Token": user.csrf, "Idempotency-Key": answer_key}), {201, 409}) for _ in range(3))
        )
        created = [r for r in answers if r is not None and r.status_code == 201]
        if len(created) != 1:
            S.problem(f"answer double submit created {len(created)} answers")
        elif created[0].json().get("job"):
            done = await wait_job(sock, created[0].json()["job"]["id"], "feedback")
            if done and not any(n == "job:feedback" for n, _ in sock.events):
                S.problem("feedback job finished but no job:feedback event arrived")
            answer_id = created[0].json()["answer"]["id"]
            nxt = await timed("follow-up question", user.http.post(f"/api/podcasts/{guest}/ai/next", json={"answer_id": answer_id}, headers=user.headers()), {202})
            if nxt is not None and nxt.status_code == 202:
                await wait_job(sock, nxt.json()["job"]["id"], "next question")
        else:
            S.problem(f"feedback not started: {created[0].json().get('job_error')}")

        await timed("export PDF", user.http.post(f"/api/podcasts/{host}/export", json={}, headers=user.headers(idem=False)), {201})

        # --- isolation under load: other users' resources must be 404
        others = [v for k, v in REGISTRY.items() if k != user.username]
        for other in random.sample(others, min(3, len(others))):
            for method, url, payload in (
                ("GET", f"/api/podcasts/{other['podcast']}", None),
                ("PATCH", f"/api/questions/{other['question']}", {"text": "hijack attempt here?"}),
                ("POST", f"/api/podcasts/{other['podcast']}/ai/generate", {"count": 1}),
            ):
                response = await timed("cross-account (expect 404)", user.http.request(method, url, json=payload, headers=user.headers()), {404})
                if response is not None and response.status_code == 200:
                    S.problem(f"CROSS-ACCOUNT LEAK: {method} {url}")

        # --- session cycle
        logout = await timed("logout", user.http.post("/logout", data={"csrf_token": user.csrf}), {303})
        if logout:
            await timed("login", user.login(), {303})
            await timed("GET /api/me after login", user.http.get("/api/me"), {200})
        await sock.close()
    finally:
        await user.close()


async def cap_race(base: str) -> None:
    user = User(base)
    await user.signup()
    host = User.podcast_id(await user.create_podcast("host", "Cap race"))
    total = CAP_QUESTIONS + 15
    results = await asyncio.gather(
        *(timed("cap race add", user.http.post(f"/api/podcasts/{host}/questions", json={"text": f"Cap race question {i} about {uuid.uuid4().hex}?"}, headers=user.headers()), {201, 409}) for i in range(total))
    )
    ok = sum(1 for r in results if r is not None and r.status_code == 201)
    final = len((await user.http.get(f"/api/podcasts/{host}")).json()["questions"])
    print(f"  cap race: {ok} accepted of {total}, final count {final} (cap {CAP_QUESTIONS})", flush=True)
    if ok != CAP_QUESTIONS or final != CAP_QUESTIONS:
        S.problem(f"question cap violated: accepted {ok}, stored {final}, cap {CAP_QUESTIONS}")
    await user.close()


async def rate_limit_and_lockout(base: str) -> None:
    user = User(base)
    await user.signup()
    host = User.podcast_id(await user.create_podcast("host", "Rate limits"))
    results = await asyncio.gather(
        *(timed("api write flood", user.http.patch(f"/api/podcasts/{host}", json={"target_minutes": 20 + i % 10}, headers=user.headers()), {200, 429}) for i in range(700))
    )
    limited = sum(1 for r in results if r is not None and r.status_code == 429)
    print(f"  api write flood: {limited} of 700 rate-limited", flush=True)
    if not limited:
        S.problem("API write rate limit never triggered")
    victim = User(base)
    await victim.signup()
    victim.http.cookies.clear()
    codes = []
    for _ in range(7):
        page = await victim.http.get("/login?add=1")
        token = page.text.split('name="csrf-token" content="')[1][:64]
        response = await victim.http.post("/login", data={"csrf_token": token, "identifier": victim.username, "password": "Wrong-Password-000"})
        codes.append(response.status_code)
    print(f"  brute force login codes: {codes}", flush=True)
    if codes[-1] != 429:
        S.problem(f"login lockout did not trigger: {codes}")
    await user.close()
    await victim.close()


async def read_burst(base: str) -> None:
    user = User(base)
    await user.signup()
    host = User.podcast_id(await user.create_podcast("host", "Read burst"))
    await asyncio.gather(*(timed("burst GET workspace", user.http.get(f"/podcasts/{host}"), {200}) for _ in range(150)))
    await asyncio.gather(*(timed("burst GET /", user.http.get("/"), {200}) for _ in range(150)))
    await user.close()


def report(elapsed: float) -> int:
    print("\n=== latency (seconds) ===")
    print(f"{'endpoint':34} {'n':>5} {'p50':>7} {'p95':>7} {'max':>7}")
    for name, values in sorted(S.latency.items()):
        values = sorted(values)
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(f"{name:34} {len(values):5d} {statistics.median(values):7.3f} {p95:7.3f} {values[-1]:7.3f}")
    print("\n=== status codes ===")
    for (name, code), count in sorted(S.codes.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        print(f"{name:34} {code!s:>5} x{count}")
    print("\n=== AI jobs ===")
    print(dict(S.jobs), "| providers:", dict(S.providers), "| mid-stream fallbacks:", S.resets)
    if S.job_seconds:
        print(f"job time p50 {statistics.median(S.job_seconds):.2f}s, max {max(S.job_seconds):.2f}s")
    print(f"\n{USERS} users, {sum(len(v) for v in S.latency.values())} requests in {elapsed:.1f}s")
    if S.problems:
        print(f"\nPROBLEMS FOUND: {len(S.problems)}")
        for problem in S.problems[:50]:
            print(" -", problem[:300])
        return 1
    print("\nSTRESS TEST PASSED - no errors, no invariant violations")
    return 0


async def main(base: str) -> int:
    started = time.perf_counter()
    print(f"Stress test against {base} with {USERS} concurrent users", flush=True)
    await asyncio.gather(*(journey(base, n) for n in range(USERS)), cap_race(base), read_burst(base))
    await rate_limit_and_lockout(base)
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            print("fake LLM stats:", (await client.get(os.environ.get("FAKE_LLM_STATS", "http://fakellm:9000/stats"))).json())
        except httpx.HTTPError:
            pass
    return report(time.perf_counter() - started)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://app:8000")))
