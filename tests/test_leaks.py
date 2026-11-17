"""Account-leak hunt.

Every route that takes an id is discovered from the app itself (so new routes are covered
automatically) and called with the victim's ids by: another signed-in account, an anonymous
visitor, and the victim's own device after it was signed out by a newer sign-in. None may succeed,
none may reveal any of the victim's data, and afterwards the victim's data must be unchanged.
"""

from __future__ import annotations

import httpx
from fastapi.routing import APIRoute

from app.main import app as fastapi_app
from app.main import asgi_app
from tests.conftest import META_CSRF, PASSWORD, FakeProvider, add_question, create_podcast, csrf, headers, lines, signup

MARKERS = ["VICTIM-TITLE-7781", "VICTIM-THEME-4410", "VICTIM-QUESTION-2093", "VICTIM-ANSWER-5528", "VICTIM-GUEST-9134", "victim@example.com", "Victoria Vane"]
REFUSALS = {303, 401, 403, 404, 405, 409, 413, 422, 429}


def _walk(routes, prefix: str = ""):
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):  # FastAPI >= 0.16x keeps included routers as objects
            extra = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _walk(route.original_router.routes, prefix + extra)


def id_routes() -> list[tuple[str, str]]:
    found = []
    for path, route in _walk(fastapi_app.routes):
        if "{" in path:
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((path, method))
    return sorted(set(found))


async def build_victim(client, ai) -> dict:
    victim = await signup(client, "victim", name="Victoria Vane")
    host = await create_podcast(client, victim["csrf"], title="VICTIM-TITLE-7781", theme="VICTIM-THEME-4410 mergers")
    question = await add_question(client, victim["csrf"], host, "VICTIM-QUESTION-2093 about the merger?")
    guest = await create_podcast(client, victim["csrf"], mode="guest", title="VICTIM-GUEST-9134")
    guest_q = await add_question(client, victim["csrf"], guest, "Why did you start the company really?")
    await client.post(
        f"/api/questions/{guest_q['id']}/answers",
        json={"text": "VICTIM-ANSWER-5528 our secret revenue numbers", "feedback": False},
        headers=headers(victim["csrf"], idem=True),
    )
    export = (await client.post(f"/api/podcasts/{host}/export", json={}, headers=headers(victim["csrf"]))).json()["export"]
    ai.use(FakeProvider("gateway", lines("1. [core|60] A perfectly fine question here?")))
    job = (await client.post(f"/api/podcasts/{host}/ai/generate", json={"count": 1}, headers=headers(victim["csrf"], idem=True))).json()["job"]
    return {
        "csrf": victim["csrf"],
        "ids": {"podcast_id": host, "question_id": question["id"], "job_id": job["id"], "export_id": export["id"]},
        "host": host,
        "question": question,
        "export_url": export["url"],
        "job": job["id"],
    }


async def crawl(client: httpx.AsyncClient, token: str | None, victim: dict, who: str) -> list[str]:
    problems = []
    for path, method in id_routes():
        url = path
        for name, value in victim["ids"].items():
            url = url.replace("{" + name + "}", value)
        if "{" in url:
            problems.append(f"{who}: unmapped path parameter in {path}")
            continue
        response = await client.request(
            method,
            url,
            json=None if method == "GET" else {"text": "LEAK-PROBE question text?", "instruction": "x y", "count": 1, "question_ids": [victim["ids"]["question_id"]]},
            headers={"X-CSRF-Token": token, "Idempotency-Key": f"probe-{method}-{abs(hash(url))}"} if token else {},
        )
        if response.status_code not in REFUSALS:
            problems.append(f"{who}: {method} {path} -> HTTP {response.status_code}")
        leaked = [m for m in MARKERS if m in response.text]
        if leaked:
            problems.append(f"{who}: {method} {path} revealed {leaked}")
    return problems


async def test_no_route_leaks_or_changes_another_accounts_data(client, other_client, ai):
    victim = await build_victim(client, ai)
    routes = id_routes()
    assert len(routes) >= 20, f"route discovery found too few routes: {routes}"
    for must in ("/api/podcasts/{podcast_id}", "/api/questions/{question_id}", "/api/jobs/{job_id}/cancel",
                 "/podcasts/{podcast_id}/exports/{export_id}/download", "/podcasts/{podcast_id}/delete"):
        assert any(path == must for path, _ in routes), f"crawler missed {must}"
    # The owner reaches every GET route - proves the crawled URLs are real (a typo would 404 too).
    for path, method in routes:
        if method == "GET":
            url = path
            for name, value in victim["ids"].items():
                url = url.replace("{" + name + "}", value)
            assert (await client.get(url)).status_code == 200, f"owner could not reach {url}"

    attacker = await signup(other_client, "attacker")
    problems = await crawl(other_client, attacker["csrf"], victim, "other account")
    for url in ("/dashboard", "/account", "/api/me", "/dashboard?q=VICTIM"):
        page = await other_client.get(url)
        problems += [f"other account: {url} revealed {m}" for m in MARKERS if m in page.text]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver") as anonymous:
        token = await csrf(anonymous, "/login")
        problems += await crawl(anonymous, token, victim, "anonymous")

    # The victim's own device after a newer sign-in elsewhere: must be locked out too.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver") as new_device:
        page = await new_device.get("/login?add=1")
        response = await new_device.post(
            "/login", data={"csrf_token": META_CSRF.search(page.text).group(1), "identifier": "victim", "password": PASSWORD}
        )
        assert response.status_code == 303
        problems += await crawl(client, victim["csrf"], victim, "signed-out device")

        # ...and nothing was changed by any of it.
        state = (await new_device.get(f"/api/podcasts/{victim['host']}")).json()
        assert [q["text"] for q in state["questions"]] == [victim["question"]["text"]]
        assert state["podcast"]["title"] == "VICTIM-TITLE-7781" and state["podcast"]["target_minutes"] == 30
        assert (await new_device.get(victim["export_url"])).status_code == 200
        job = (await new_device.get(f"/api/jobs/{victim['job']}")).json()["job"]
        assert job["status"] == "queued", "nobody else could cancel the victim's job"

    assert not problems, "\n".join(problems)


async def test_device_account_chooser_is_per_device(client, other_client):
    await signup(client, "chooser_a", name="Alice Device")
    page = await other_client.get("/login")
    assert "Alice Device" not in page.text and "chooser_a@example.com" not in page.text
