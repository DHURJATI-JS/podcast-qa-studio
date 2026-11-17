"""Shared helpers for the smoke and stress tests (a real HTTP client against a running stack)."""

from __future__ import annotations

import asyncio
import re
import uuid

import httpx

META_CSRF = re.compile(r'<meta name="csrf-token" content="([0-9a-f]{64})">')
FORM_TS = re.compile(r'name="form_ts" value="([^"]+)"')
IDEM = re.compile(r'name="idem" value="([^"]+)"')
PASSWORD = "Stress-Test-Passphrase-2026"


class User:
    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, follow_redirects=False)
        self.csrf = ""
        self.username = ""

    async def close(self) -> None:
        await self.http.aclose()

    async def refresh_csrf(self, path: str = "/dashboard") -> str:
        response = await self.http.get(path)
        self.csrf = META_CSRF.search(response.text).group(1)
        return self.csrf

    def headers(self, idem: bool = True) -> dict:
        out = {"X-CSRF-Token": self.csrf}
        if idem:
            out["Idempotency-Key"] = uuid.uuid4().hex
        return out

    async def signup(self, username: str | None = None) -> httpx.Response:
        self.username = username or f"u{uuid.uuid4().hex[:10]}"
        page = await self.http.get("/signup")
        token = META_CSRF.search(page.text).group(1)
        form_ts = FORM_TS.search(page.text).group(1)
        await asyncio.sleep(2.2)  # the sign-up form rejects submissions faster than a human (anti-bot)
        response = await self.http.post(
            "/signup",
            data={
                "csrf_token": token,
                "form_ts": form_ts,
                "display_name": f"Load {self.username}",
                "username": self.username,
                "email": f"{self.username}@example.com",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        if response.status_code == 303:
            await self.refresh_csrf()
        return response

    async def login(self) -> httpx.Response:
        page = await self.http.get("/login?add=1")
        token = META_CSRF.search(page.text).group(1)
        response = await self.http.post("/login", data={"csrf_token": token, "identifier": self.username, "password": PASSWORD})
        if response.status_code == 303:
            await self.refresh_csrf()
        return response

    async def create_podcast(self, mode: str = "host", title: str = "Stress show", **extra) -> httpx.Response:
        page = await self.http.get(f"/podcasts/new?mode={mode}")
        data = {
            "csrf_token": self.csrf,
            "mode": mode,
            "idem": IDEM.search(page.text).group(1),
            "title": title,
            "theme": extra.pop("theme", "How small climate startups survive their first three years"),
            "target_minutes": str(extra.pop("target_minutes", 20)),
            "intro_seconds": "45",
            "outro_seconds": "45",
        }
        data.update({k: str(v) for k, v in extra.items()})
        return await self.http.post("/podcasts/new", data=data)

    @staticmethod
    def podcast_id(response: httpx.Response) -> str:
        return response.headers["location"].split("/podcasts/")[1].split("?")[0]
