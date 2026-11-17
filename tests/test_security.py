"""Security headers (Talisman/CSP nonce), CSRF, host guard, body limits, input cleaning, XSS."""

from __future__ import annotations

import re

from app.security.sanitize import clean_text, safe_next, spam_reason
from tests.conftest import FakeProvider, add_question, create_podcast, csrf, headers, lines, signup

NONCE = re.compile(r"'nonce-([A-Za-z0-9_\-]+)'")


async def test_security_headers_and_csp_nonce(client):
    first = await client.get("/")
    second = await client.get("/")
    csp = first.headers["content-security-policy"]
    nonce = NONCE.search(csp).group(1)
    assert nonce != NONCE.search(second.headers["content-security-policy"]).group(1), "nonce must change per request"
    assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    scripts = re.findall(r"<script\b[^>]*>", first.text)
    executable = [s for s in scripts if 'type="application/json"' not in s]
    assert executable and all(f'nonce="{nonce}"' in s for s in executable)
    assert first.headers["x-frame-options"] == "DENY"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "no-store" in first.headers["cache-control"]
    assert "microphone=()" in first.headers["permissions-policy"]
    assert "cross-origin-opener-policy" not in first.headers, "COOP is ignored (and logged) on plain-HTTP LAN origins"
    local = await client.get("/", headers={"Host": "localhost:8080"})
    assert local.headers["cross-origin-opener-policy"] == "same-origin"
    assert "server" not in {k.lower() for k in first.headers if k.lower() == "server" and "uvicorn" in first.headers[k].lower()}


async def test_html_is_never_gzip_compressed(client):
    response = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") != "gzip"
    css = await client.get("/static/dist/app.min.css", headers={"Accept-Encoding": "gzip"})
    assert css.headers.get("content-encoding") == "gzip"


async def test_csrf_required_for_every_unsafe_request(client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    no_token = await client.patch(f"/api/podcasts/{podcast_id}", json={"target_minutes": 10})
    assert no_token.status_code == 403 and no_token.json()["code"] == "csrf"
    bad_token = await client.patch(f"/api/podcasts/{podcast_id}", json={"target_minutes": 10}, headers={"X-CSRF-Token": "f" * 64})
    assert bad_token.status_code == 403
    cross_site = await client.patch(
        f"/api/podcasts/{podcast_id}", json={"target_minutes": 10}, headers={"X-CSRF-Token": me["csrf"], "Origin": "https://evil.example"}
    )
    assert cross_site.status_code == 403
    form = await client.post("/logout", data={"csrf_token": "wrong"})
    assert form.status_code == 403
    good = await client.patch(f"/api/podcasts/{podcast_id}", json={"target_minutes": 10}, headers=headers(me["csrf"]))
    assert good.status_code == 200


async def test_csrf_cookie_rotates_on_login(client):
    before = await csrf(client, "/login")
    me = await signup(client)
    assert me["csrf"] != before


async def test_host_header_guard(client):
    evil = await client.get("/", headers={"Host": "evil.example"})
    assert evil.status_code == 400
    ip = await client.get("/healthz", headers={"Host": "192.168.0.10:8080"})
    assert ip.status_code == 200


async def test_request_body_limit(client):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"])
    huge = "x" * 1_200_000
    response = await client.post(
        f"/api/podcasts/{podcast_id}/questions", content=huge, headers={**headers(me["csrf"]), "Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_clean_text_removes_code_and_invisible_characters():
    assert clean_text("<script>alert(1)</script>Hello", max_len=100) == "Hello"
    assert clean_text('<img src=x onerror="alert(1)">Hi <b>there</b>', max_len=100) == "Hi there"
    assert clean_text("&lt;script&gt;alert(1)&lt;/script&gt;ok", max_len=100) == "ok"
    assert clean_text("a\u200bb\u202ec\x00d\x07e", max_len=100) == "abcde"
    assert clean_text("  many   spaces \n\n\n\n lines ", max_len=100, multiline=True) == "many spaces\n\nlines"
    assert clean_text("R&D costs < 5% & rising", max_len=100) == "R&D costs < 5% & rising"
    assert len(clean_text("x" * 500, max_len=120)) == 120


def test_safe_next_blocks_open_redirects():
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)", "/ok\r\nSet-Cookie: x"):
        assert safe_next(bad) == "/dashboard"
    assert safe_next("/podcasts/123?x=1") == "/podcasts/123?x=1"


def test_spam_reasons():
    assert spam_reason("hi") is not None
    assert spam_reason("a" * 40 + " b c") is not None
    assert spam_reason("!!!! ???? #### $$$$ %%%% ^^^^") is not None
    assert spam_reason("spam " * 30) is not None
    assert spam_reason("I started the company after watching farmers lose crops to heat.") is None


async def test_xss_is_stripped_and_escaped_everywhere(client, ai):
    me = await signup(client)
    podcast_id = await create_podcast(client, me["csrf"], title='Show <script>alert("t")</script> One', audience='<img src=x onerror=alert(1)>Founders')
    page = await client.get(f"/podcasts/{podcast_id}")
    assert "<script>alert" not in page.text and "onerror=alert" not in page.text
    assert "Show  One" in page.text or "Show One" in page.text
    question = await add_question(client, me["csrf"], podcast_id, 'Why <b onmouseover="x()">this</b> idea? <script>steal()</script>')
    assert question["text"] == "Why this idea?"
    # AI output is cleaned too.
    ai.use(FakeProvider("gateway", lines('1. [core|120] What <script>alert(1)</script>made you <a href="javascript:x">start</a>?')))
    job = (await client.post(f"/api/podcasts/{podcast_id}/ai/generate", json={"count": 1}, headers=headers(me["csrf"], idem=True))).json()["job"]
    await ai.run(job["id"])
    state = (await client.get(f"/api/podcasts/{podcast_id}")).json()
    texts = [q["text"] for q in state["questions"]]
    assert "What made you start?" in texts
    assert not any("<" in t for t in texts)
    # The page embeds state as JSON: no raw "<" can appear inside the data block.
    page = await client.get(f"/podcasts/{podcast_id}")
    data_block = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', page.text, re.S).group(1)
    assert "<" not in data_block
