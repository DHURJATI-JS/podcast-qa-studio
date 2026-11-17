"""Locust stress test for Podcast Q&A Studio (run against the throwaway stress stack only).

    docker compose -f docker-compose.stress.yml run --name pq-locust locust

Five kinds of simulated users run at once - hosts, guests, read-mostly browsers, sign-in churn and
security probes - over a staged 10-minute load shape (warm-up, ramp, sustained, spike, recovery).
Correctness is checked *during* the load: unexpected status codes, AI jobs that never finish over
Socket.IO, double submits that create two jobs, cross-account access that is not a 404, broken
question ordering, XSS that is not stripped... each counts as a Locust failure.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid

import gevent
import websocket
from locust import HttpUser, LoadTestShape, between, events, task
from locust.clients import HttpSession

META_CSRF = re.compile(r'<meta name="csrf-token" content="([0-9a-f]{64})">')
FORM_TS = re.compile(r'name="form_ts" value="([^"]+)"')
IDEM = re.compile(r'name="idem" value="([^"]+)"')
ASSET = re.compile(r'(/static/dist/app\.min\.(?:css|js)\?v=[0-9a-f]+)')
PASSWORD = "Locust-Stress-Passphrase-2026"
TOPICS = ["pricing", "hiring", "fundraising", "burnout", "mentors", "failure", "regulation", "culture", "metrics", "brand",
          "first customer", "supply chain", "remote work", "storytelling", "competition", "investors"]
PHRASES = [
    "Welcome back to the show.",
    "Why did you start your company?",
    "Tell us about your hardest year.",
    "What would you tell your younger self?",
    "Thanks so much for joining us today.",
]
REGISTRY: list[tuple[str, str, str]] = []  # (owner username, podcast id, question id) - targets for isolation probes


def custom_metric(name: str, started: float, error: str | None = None, request_type: str = "SOCKET") -> None:
    events.request.fire(
        request_type=request_type,
        name=name,
        response_time=(time.time() - started) * 1000,
        response_length=0,
        exception=Exception(error) if error else None,
        context={},
    )


class Socket:
    """Minimal Socket.IO (Engine.IO v4) client over websocket-client (gevent-friendly)."""

    def __init__(self, base_url: str, cookies: dict, csrf: str) -> None:
        url = base_url.replace("http://", "ws://").rstrip("/") + "/socket.io/?EIO=4&transport=websocket"
        self.ws = websocket.create_connection(
            url,
            origin=base_url.rstrip("/"),  # websocket-client sends Origin itself; a 2nd header is (rightly) rejected
            cookie="; ".join(f"{k}={v}" for k, v in cookies.items()),
            timeout=15,
        )
        opened = self.ws.recv()
        if not opened.startswith("0"):
            raise RuntimeError(f"bad open packet {opened[:40]}")
        self.ws.send("40" + json.dumps({"csrf": csrf}))
        while True:
            message = self.ws.recv()
            if message.startswith("40"):
                break
            if message.startswith("44"):
                raise RuntimeError(f"socket refused: {message[2:80]}")
            if message == "2":
                self.ws.send("3")
        self.ws.settimeout(None)
        self.finished: dict[str, tuple[str, dict]] = {}
        self.deltas: dict[str, int] = {}
        self.alive = True
        self.reader = gevent.spawn(self._read)

    def _read(self) -> None:
        while self.alive:
            try:
                message = self.ws.recv()
            except Exception:  # noqa: BLE001
                self.alive = False
                return
            if message == "2":
                try:
                    self.ws.send("3")
                except Exception:  # noqa: BLE001
                    self.alive = False
                    return
            elif message.startswith("41"):
                self.alive = False
                return
            elif message.startswith("42"):
                try:
                    name, data = json.loads(message[2:])
                except ValueError:
                    continue
                job_id = data.get("job_id") if isinstance(data, dict) else None
                if not job_id:
                    continue
                if name == "job:delta":
                    self.deltas[job_id] = self.deltas.get(job_id, 0) + 1
                elif name in ("job:done", "job:error"):
                    self.finished[job_id] = (name, data)

    def wait(self, job_id: str, timeout: float) -> tuple[str, dict] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job_id in self.finished:
                return self.finished.pop(job_id)
            if not self.alive:
                return None
            gevent.sleep(0.05)
        return None

    def close(self) -> None:
        self.alive = False
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class Base(HttpUser):
    abstract = True
    wait_time = between(1, 3)

    # ------------------------------------------------------------------ helpers
    def expect(self, method: str, url: str, name: str, ok: set[int], **kwargs):
        headers = kwargs.pop("headers", {})
        if kwargs.pop("csrf", True) and method != "GET":
            headers.setdefault("X-CSRF-Token", self.csrf)
        if kwargs.pop("idem", False):
            headers["Idempotency-Key"] = uuid.uuid4().hex
        with self.client.request(method, url, name=name, headers=headers, catch_response=True, allow_redirects=False, **kwargs) as response:
            if response.status_code in ok:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code} (expected {sorted(ok)}): {response.text[:150]}")
            return response

    def signup(self) -> None:
        self.csrf = ""
        page = self.client.get("/signup", name="GET /signup")
        token, form_ts = META_CSRF.search(page.text).group(1), FORM_TS.search(page.text).group(1)
        gevent.sleep(2.2)  # the form rejects bot-speed submissions
        self.username = "lc" + uuid.uuid4().hex[:10]
        self.display_name = f"Locust {self.username[-4:]}"
        with self.client.post(
            "/signup",
            data={
                "csrf_token": token, "form_ts": form_ts, "display_name": self.display_name, "username": self.username,
                "email": f"{self.username}@example.com", "password": PASSWORD, "confirm_password": PASSWORD,
            },
            name="POST /signup",
            allow_redirects=False,
            catch_response=True,
        ) as response:
            if response.status_code == 303:
                response.success()
            else:
                response.failure(f"signup HTTP {response.status_code}: {response.text[:150]}")
                raise RuntimeError("signup failed")
        self.refresh_csrf()

    def refresh_csrf(self) -> None:
        page = self.client.get("/dashboard", name="GET /dashboard")
        self.csrf = META_CSRF.search(page.text).group(1)

    def create_podcast(self, mode: str) -> str:
        page = self.client.get(f"/podcasts/new?mode={mode}", name="GET /podcasts/new")
        data = {
            "csrf_token": self.csrf, "mode": mode, "idem": IDEM.search(page.text).group(1),
            "title": f"{'Host' if mode == 'host' else 'Guest'} load show {uuid.uuid4().hex[:6]}",
            "theme": f"How small climate startups handle {random.choice(TOPICS)}",
            "target_minutes": str(random.choice([20, 30, 45])), "intro_seconds": "45", "outro_seconds": "45",
            "instructions": "Avoid politics", "talking_points": "Cold storage cuts crop loss by 30 percent",
        }
        with self.client.post("/podcasts/new", data=data, name="POST /podcasts/new", allow_redirects=False, catch_response=True) as response:
            if response.status_code != 303:
                response.failure(f"create podcast HTTP {response.status_code}: {response.text[:150]}")
                raise RuntimeError("create podcast failed")
            response.success()
            return response.headers["location"].split("/podcasts/")[1].split("?")[0]

    def question_text(self) -> str:
        return f"What did year {random.randint(1, 12)} teach you about {random.choice(TOPICS)} ({uuid.uuid4().hex[:5]})?"

    def connect_socket(self) -> None:
        started = time.time()
        try:
            self.sock = Socket(self.host, self.client.cookies.get_dict(), self.csrf)
            custom_metric("connect Socket.IO", started)
        except Exception as exc:  # noqa: BLE001
            self.sock = None
            custom_metric("connect Socket.IO", started, f"{type(exc).__name__}: {exc}")

    def ensure_socket(self) -> None:
        if getattr(self, "sock", None) is None or not self.sock.alive:
            self.connect_socket()

    def run_ai_job(self, url: str, body: dict, label: str, timeout: float = 150) -> dict | None:
        """Start an AI job and measure its full round trip (API -> queue -> worker -> LLM -> Socket.IO)."""
        self.ensure_socket()
        started = time.time()
        response = self.expect("POST", url, f"POST {label}", {202, 409}, json=body, idem=True)
        if response.status_code != 202:
            return None  # busy: another job for this podcast is still running (a correct refusal)
        job_id = response.json()["job"]["id"]
        if self.sock is None:
            return None
        result = self.sock.wait(job_id, timeout)
        if result is None:
            custom_metric(f"AI job {label} (round trip)", started, "no job:done/job:error over Socket.IO in time")
            return None
        name, data = result
        if name == "job:error":
            custom_metric(f"AI job {label} (round trip)", started, f"job failed: {data.get('message', '')[:120]}")
            return None
        if not self.sock.deltas.pop(job_id, 0):
            custom_metric(f"AI job {label} (round trip)", started, "finished but no streamed tokens arrived")
            return None
        custom_metric(f"AI job {label} (round trip)", started)
        return data

    def on_stop(self) -> None:
        if getattr(self, "sock", None) is not None:
            self.sock.close()


class HostUser(Base):
    """Plans an episode: questions, edits, AI generation/rewrites, PDFs, voice."""

    weight = 5

    def on_start(self) -> None:
        self.signup()
        self.podcast = self.create_podcast("host")
        self.questions: list[dict] = []
        for _ in range(3):
            self.add_question()
        self.connect_socket()

    def state(self) -> dict | None:
        response = self.expect("GET", f"/api/podcasts/{self.podcast}", "GET /api/podcasts/[id]", {200})
        if response.status_code != 200:
            return None
        data = response.json()
        self.questions = data["questions"]
        positions = [q["position"] for q in self.questions]
        if positions != list(range(1, len(positions) + 1)):
            custom_metric("invariant: question positions contiguous", time.time(), f"positions {positions}", "CHECK")
        if self.questions:
            REGISTRY.append((self.username, self.podcast, self.questions[0]["id"]))
            del REGISTRY[:-300]
        return data

    @task(8)
    def view_workspace(self) -> None:
        self.expect("GET", f"/podcasts/{self.podcast}", "GET /podcasts/[id]", {200})

    @task(6)
    def view_dashboard(self) -> None:
        self.expect("GET", "/dashboard", "GET /dashboard", {200})

    @task(10)
    def read_state(self) -> None:
        self.state()

    @task(6)
    def add_question(self) -> None:
        if len(self.questions) >= 45:
            self.podcast = self.create_podcast("host")  # keep exercising normal paths, not just the cap
            self.questions = []
        text = self.questions[0]["text"] if self.questions and random.random() < 0.1 else self.question_text()
        response = self.expect(
            "POST", f"/api/podcasts/{self.podcast}/questions", "POST /api/podcasts/[id]/questions", {201, 409},
            json={"text": text, "category": random.choice(["core", "story", "opener", "closing"]), "est_seconds": random.choice([60, 90, 120, 180])},
            idem=True,
        )
        if response.status_code == 201:
            self.questions.append(response.json()["question"])

    @task(4)
    def edit_question(self) -> None:
        if not self.questions:
            return
        q = random.choice(self.questions)
        self.expect("PATCH", f"/api/questions/{q['id']}", "PATCH /api/questions/[id]", {200, 404},
                    json={"text": self.question_text(), "est_seconds": random.choice([60, 120, 240])})

    @task(2)
    def reorder(self) -> None:
        data = self.state()
        if not data or len(self.questions) < 2:
            return
        ids = [q["id"] for q in self.questions]
        random.shuffle(ids)
        # 409 = an AI job inserted a question meanwhile (correctly refused)
        self.expect("POST", f"/api/podcasts/{self.podcast}/questions/reorder", "POST reorder", {200, 409}, json={"ids": ids})

    @task(2)
    def quality_check(self) -> None:
        self.expect("POST", f"/api/podcasts/{self.podcast}/quality-check", "POST quality-check", {200}, json={})

    @task(1)
    def undo(self) -> None:
        if self.questions:
            self.expect("POST", f"/api/questions/{random.choice(self.questions)['id']}/undo", "POST undo", {200, 404, 409}, json={})

    @task(2)
    def delete_question(self) -> None:
        if len(self.questions) > 3:
            q = self.questions.pop(random.randrange(len(self.questions)))
            self.expect("DELETE", f"/api/questions/{q['id']}", "DELETE /api/questions/[id]", {200, 404})

    @task(3)
    def ai_generate(self) -> None:
        self.run_ai_job(f"/api/podcasts/{self.podcast}/ai/generate", {"count": 3, "focus": random.choice(["", "early days", "failure"])}, "generate")

    @task(1)
    def ai_rewrite(self) -> None:
        if self.questions:
            ids = [q["id"] for q in self.questions[:3]]
            self.run_ai_job(f"/api/podcasts/{self.podcast}/ai/rewrite", {"question_ids": ids, "instruction": "make them shorter"}, "rewrite")

    @task(1)
    def double_submit(self) -> None:
        """The same Idempotency-Key sent twice at the same moment must start at most one job."""
        key = uuid.uuid4().hex
        url = f"/api/podcasts/{self.podcast}/ai/generate"
        results: list[int] = []

        def fire() -> None:
            with self.client.post(url, json={"count": 2}, headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": key},
                                  name="POST generate (same key x2)", catch_response=True) as response:
                results.append(response.status_code)
                if response.status_code in (202, 409):
                    response.success()
                else:
                    response.failure(f"HTTP {response.status_code}: {response.text[:120]}")

        gevent.joinall([gevent.spawn(fire), gevent.spawn(fire)])
        if results.count(202) > 1:
            custom_metric("invariant: one job per idempotency key", time.time(), f"{results.count(202)} jobs started", "CHECK")
        else:
            custom_metric("invariant: one job per idempotency key", time.time(), None, "CHECK")

    @task(1)
    def export_pdf(self) -> None:
        response = self.expect("POST", f"/api/podcasts/{self.podcast}/export", "POST export PDF", {201, 409}, json={})
        if response.status_code == 201:
            with self.client.get(response.json()["export"]["url"], name="GET PDF download", catch_response=True) as pdf:
                if pdf.status_code == 200 and pdf.content.startswith(b"%PDF"):
                    pdf.success()
                else:
                    pdf.failure(f"bad PDF download HTTP {pdf.status_code}")

    @task(1)
    def voice(self) -> None:
        text = random.choice(PHRASES) if random.random() < 0.9 else f"Question {random.randint(1, 10_000)}: what changed for you?"
        # 503 = Kokoro busy/offline -> the browser falls back to its own voice (expected under heavy load)
        with self.client.post("/api/tts", json={"text": text}, headers={"X-CSRF-Token": self.csrf}, name="POST /api/tts",
                              catch_response=True) as response:
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("audio/"):
                response.success()
            elif response.status_code in (503, 429):
                response.success()
                custom_metric("TTS fell back to browser voice", time.time(), None, "INFO")
            else:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")

    @task(2)
    def status(self) -> None:
        self.expect("GET", "/api/ai/status", "GET /api/ai/status", {200})
        me = self.expect("GET", "/api/me", "GET /api/me", {200})
        if me.status_code == 200 and me.json().get("username") != self.username:
            custom_metric("invariant: /api/me is my own profile", time.time(), "returned another user's profile!", "CHECK")


class GuestUser(Base):
    """Prepares as a guest: answers with AI feedback, follow-up questions, attempts history."""

    weight = 3

    def on_start(self) -> None:
        self.signup()
        self.podcast = self.create_podcast("guest")
        self.questions = []
        for text in ("Why did you start your company?", "What was your hardest year?", "Where is the industry heading?"):
            response = self.expect("POST", f"/api/podcasts/{self.podcast}/questions", "POST /api/podcasts/[id]/questions", {201}, json={"text": text}, idem=True)
            if response.status_code == 201:
                self.questions.append(response.json()["question"])
        self.connect_socket()

    def answer_text(self) -> str:
        return (
            f"I started after a heatwave near Pune wiped out {random.randint(20, 90)} percent of a farmer's harvest, "
            f"and I realised cheap solar cold rooms could change that. Take {uuid.uuid4().hex[:6]}."
        )

    @task(6)
    def answer_with_feedback(self) -> None:
        if not self.questions:
            return
        self.ensure_socket()
        q = random.choice(self.questions)
        started = time.time()
        response = self.expect("POST", f"/api/questions/{q['id']}/answers", "POST answer (feedback)", {201, 409},
                               json={"text": self.answer_text(), "feedback": True, "rehearsed_seconds": random.randint(20, 90)}, idem=True)
        if response.status_code != 201:
            return
        body = response.json()
        if not body.get("job"):
            if body.get("job_error") and "already working" not in body["job_error"]:
                custom_metric("AI job feedback (round trip)", started, f"not started: {body['job_error'][:120]}")
            return
        job_id = body["job"]["id"]
        result = self.sock.wait(job_id, 150) if self.sock else None
        if result is None:
            custom_metric("AI job feedback (round trip)", started, "no job:done/job:error over Socket.IO in time")
        elif result[0] == "job:error":
            custom_metric("AI job feedback (round trip)", started, f"job failed: {result[1].get('message', '')[:120]}")
        else:
            custom_metric("AI job feedback (round trip)", started)
            if random.random() < 0.3:
                self.run_ai_job(f"/api/podcasts/{self.podcast}/ai/next", {"answer_id": body["answer"]["id"]}, "follow-up")
                state = self.expect("GET", f"/api/podcasts/{self.podcast}", "GET /api/podcasts/[id]", {200})
                if state.status_code == 200:
                    self.questions = state.json()["questions"][:10]

    @task(3)
    def save_without_feedback(self) -> None:
        if self.questions:
            q = random.choice(self.questions)
            self.expect("POST", f"/api/questions/{q['id']}/answers", "POST answer (save only)", {201},
                        json={"text": self.answer_text(), "feedback": False}, idem=True)

    @task(4)
    def attempts(self) -> None:
        if self.questions:
            q = random.choice(self.questions)
            self.expect("GET", f"/api/questions/{q['id']}/answers", "GET answers list", {200, 404})

    @task(3)
    def workspace(self) -> None:
        self.expect("GET", f"/podcasts/{self.podcast}", "GET /podcasts/[id]", {200})


class BrowseUser(Base):
    """Read-mostly: public pages, static assets (minified, cache-busted), dashboard, account."""

    weight = 3

    def on_start(self) -> None:
        self.signup()
        home = self.client.get("/", name="GET /")
        self.assets = ASSET.findall(home.text)

    @task(4)
    def home(self) -> None:
        self.expect("GET", "/", "GET /", {200})

    @task(1)
    def legal(self) -> None:
        self.expect("GET", "/legal", "GET /legal", {200})

    @task(3)
    def static(self) -> None:
        for asset in self.assets:
            with self.client.get(asset, name="GET static (versioned)", catch_response=True, headers={"Accept-Encoding": "gzip"}) as response:
                if response.status_code == 200 and "immutable" in response.headers.get("cache-control", ""):
                    response.success()
                else:
                    response.failure(f"HTTP {response.status_code} cache-control={response.headers.get('cache-control')}")

    @task(3)
    def dashboard_search(self) -> None:
        self.expect("GET", f"/dashboard?q={random.choice(TOPICS)}&mode=host", "GET /dashboard?q=", {200})

    @task(2)
    def account(self) -> None:
        self.expect("GET", "/account", "GET /account", {200})

    @task(2)
    def voices(self) -> None:
        self.expect("GET", "/api/tts/voices", "GET /api/tts/voices", {200})


class AuthChurnUser(Base):
    """Sign out / account chooser / sign in loops, forgot-password, validation errors."""

    weight = 1

    def on_start(self) -> None:
        self.signup()

    @task(4)
    def logout_login(self) -> None:
        self.expect("POST", "/logout", "POST /logout", {303}, data={"csrf_token": self.csrf}, csrf=False)
        chooser = self.expect("GET", "/login", "GET /login (chooser)", {200})
        if self.display_name not in chooser.text:
            custom_metric("invariant: chooser shows my name", time.time(), "account chooser missing the account", "CHECK")
        token = META_CSRF.search(chooser.text).group(1)
        self.expect("POST", "/login", "POST /login", {303}, data={"csrf_token": token, "identifier": self.username, "password": PASSWORD}, csrf=False)
        self.refresh_csrf()
        me = self.expect("GET", "/api/me", "GET /api/me", {200})
        if me.status_code == 200 and me.json().get("username") != self.username:
            custom_metric("invariant: /api/me is my own profile", time.time(), "returned another user's profile!", "CHECK")

    @task(1)
    def forgot_password(self) -> None:
        page = self.client.get("/forgot-password", name="GET /forgot-password")
        token, form_ts = META_CSRF.search(page.text).group(1), FORM_TS.search(page.text).group(1)
        gevent.sleep(2.1)
        # 429 once the per-IP limit is reached - correct (all Locust users share one IP)
        self.expect("POST", "/forgot-password", "POST /forgot-password", {200, 429},
                    data={"csrf_token": token, "form_ts": form_ts, "email": f"{self.username}@example.com"}, csrf=False)

    @task(1)
    def bad_signup(self) -> None:
        # Signed in, the sign-up page sends you to your dashboard (like Google does)...
        self.expect("GET", "/signup", "GET /signup (signed in -> dashboard)", {303})
        # ...so the validation probe runs as a separate, signed-out visitor.
        visitor = HttpSession(base_url=self.host, request_event=self.environment.events.request, user=self)
        page = visitor.get("/signup", name="GET /signup")
        token, form_ts = META_CSRF.search(page.text).group(1), FORM_TS.search(page.text).group(1)
        gevent.sleep(2.1)
        with visitor.post("/signup", name="POST /signup (weak password -> 422)", allow_redirects=False, catch_response=True,
                          data={"csrf_token": token, "form_ts": form_ts, "username": "x" + uuid.uuid4().hex[:8],
                                "email": "x@example.com", "password": "short", "confirm_password": "short"}) as response:
            if response.status_code == 422:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code} (expected 422): {response.text[:150]}")


class SecurityProbeUser(Base):
    """Hostile requests mixed into the load. Every one must be refused correctly."""

    weight = 1

    def on_start(self) -> None:
        self.signup()
        self.podcast = self.create_podcast("host")

    @task(5)
    def cross_account(self) -> None:
        targets = [t for t in REGISTRY[-200:] if t[0] != self.username]
        if not targets:
            return
        owner, podcast, question = random.choice(targets)
        for method, url, body in (
            ("GET", f"/api/podcasts/{podcast}", None),
            ("GET", f"/podcasts/{podcast}", None),
            ("PATCH", f"/api/questions/{question}", {"text": "hijacked by another account?"}),
            ("POST", f"/api/podcasts/{podcast}/ai/generate", {"count": 1}),
            ("DELETE", f"/api/questions/{question}", None),
        ):
            response = self.expect(method, url, f"probe: other user's data ({method})", {404}, json=body)
            if response.status_code == 200:
                custom_metric("invariant: cross-account isolation", time.time(), f"LEAK {method} {url}", "CHECK")

    @task(2)
    def no_csrf(self) -> None:
        self.expect("PATCH", f"/api/podcasts/{self.podcast}", "probe: write without CSRF token", {403}, json={"target_minutes": 10}, csrf=False)
        self.expect("PATCH", f"/api/podcasts/{self.podcast}", "probe: cross-site Origin", {403}, json={"target_minutes": 10},
                    headers={"Origin": "https://evil.example"})

    @task(1)
    def bad_host(self) -> None:
        self.expect("GET", "/", "probe: bad Host header", {400}, headers={"Host": "evil.example"})

    @task(2)
    def xss(self) -> None:
        response = self.expect("POST", f"/api/podcasts/{self.podcast}/questions", "probe: XSS in question", {201, 409},
                               json={"text": f'Why <script>alert({random.randint(1, 99)})</script><img src=x onerror=alert(1)> this {uuid.uuid4().hex[:6]}?'},
                               idem=True)
        if response.status_code == 201 and ("<" in response.json()["question"]["text"]):
            custom_metric("invariant: XSS stripped", time.time(), response.json()["question"]["text"][:80], "CHECK")
        page = self.client.get(f"/podcasts/{self.podcast}", name="GET /podcasts/[id]")
        if "<script>alert(" in page.text:
            custom_metric("invariant: XSS stripped", time.time(), "script tag rendered in page", "CHECK")

    @task(1)
    def oversized(self) -> None:
        self.expect("POST", f"/api/podcasts/{self.podcast}/questions", "probe: 1.2 MB body", {413},
                    data="x" * 1_200_000, headers={"Content-Type": "application/json"})

    @task(1)
    def malformed(self) -> None:
        self.expect("GET", "/api/podcasts/not-a-uuid", "probe: malformed id", {404})


class SecondDeviceUser(Base):
    """One login per account under load: the account signs in on a second device; the first
    device's next write must be refused with the reason and must change nothing."""

    weight = 1
    agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

    def on_start(self) -> None:
        self.signup()
        self.podcast = self.create_podcast("host")

    def count_questions(self, session) -> int | None:
        response = session.get(f"/api/podcasts/{self.podcast}", name="GET /api/podcasts/[id]")
        return len(response.json()["questions"]) if response.status_code == 200 else None

    @task
    def sign_in_elsewhere(self) -> None:
        second = HttpSession(base_url=self.host, request_event=self.environment.events.request, user=self)
        page = second.get("/login?add=1", name="GET /login (second device)")
        token = META_CSRF.search(page.text).group(1)
        with second.post("/login", data={"csrf_token": token, "identifier": self.username, "password": PASSWORD},
                         headers={"User-Agent": self.agent}, name="POST /login (second device)", allow_redirects=False,
                         catch_response=True) as response:
            if response.status_code != 303:
                response.failure(f"second-device sign-in HTTP {response.status_code}")
                return
            response.success()
        before = self.count_questions(second)
        with self.client.post(f"/api/podcasts/{self.podcast}/questions", json={"text": self.question_text()},
                              headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": uuid.uuid4().hex},
                              name="old device: write after sign-in elsewhere (expect 401)", catch_response=True) as response:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code == 401 and body.get("reason") == "replaced" and "another device" in body.get("detail", ""):
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}: {response.text[:150]}")
        after = self.count_questions(second)
        custom_metric("invariant: signed-out device changed nothing", time.time(),
                      None if before is not None and before == after else f"questions {before} -> {after}", "CHECK")
        # The second device is this user's device from now on.
        self.client = second
        self.csrf = META_CSRF.search(second.get("/dashboard", name="GET /dashboard").text).group(1)


class StagedLoad(LoadTestShape):
    """10 minutes: warm-up -> ramp -> sustained -> spike -> recovery."""

    stages = [
        (60, 50, 5),     # 0-1 min   warm up to 50 users
        (180, 150, 5),   # 1-3 min   ramp to 150
        (360, 250, 5),   # 3-6 min   sustained 250
        (480, 300, 10),  # 6-8 min   spike to 300
        (600, 100, 10),  # 8-10 min  recover at 100
    ]

    time_scale = float(os.environ.get("LOCUST_TIME_SCALE", "1"))  # e.g. 0.08 for a ~50 s dry run

    def tick(self):
        run_time = self.get_run_time()
        for until, users, rate in self.stages:
            if run_time < until * self.time_scale:
                return users, rate
        return None
