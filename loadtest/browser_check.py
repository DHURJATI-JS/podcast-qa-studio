"""Headless-browser end-to-end check (Playwright/Chromium) against a throwaway stack.

    python loadtest/browser_check.py http://app:8000 /out

Fails on any console error, uncaught exception or CSP violation, and saves screenshots.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from playwright.async_api import async_playwright

PASSWORD = "Browser-Test-Passphrase-42"


async def wait_until(page, expression: str, timeout: float = 15.0) -> None:
    """Poll a JS expression via DevTools evaluate (not subject to the page's CSP, unlike
    wait_for_function, which needs 'unsafe-eval' - and our CSP correctly forbids that)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await page.evaluate(expression):
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out waiting for: {expression}")
        await asyncio.sleep(0.05)


async def main(base: str, out: str) -> int:
    problems: list[str] = []
    console: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("  ok  " if ok else "  !!  ") + what, flush=True)
        if not ok:
            problems.append(what)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = await browser.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
        page = await context.new_page()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        await page.goto(base + "/")
        check("Problems it solves" in await page.content(), "home page renders with features + problems")
        before = await page.evaluate("document.documentElement.classList.contains('dark')")
        await page.click("[data-theme-toggle]")
        after = await page.evaluate("document.documentElement.classList.contains('dark')")
        check(before != after, "dark/light toggle works")
        await page.screenshot(path=f"{out}/1-home.png")
        await page.click("[data-theme-toggle]")

        user = "b" + uuid.uuid4().hex[:8]
        await page.goto(base + "/signup")
        await page.fill("#display_name", "Browser Tester")
        await page.fill("#username", user)
        await page.fill("#email", f"{user}@example.com")
        await page.fill("#password", PASSWORD)
        strength = await page.text_content("[data-strength-label]")
        check(strength in ("Strong", "Very strong"), f"password strength meter ({strength})")
        await page.fill("#confirm_password", PASSWORD)
        await page.wait_for_timeout(2300)
        await page.click("form[data-signup] button:not([type=button])")
        await page.wait_for_url("**/dashboard", timeout=20000)
        check(True, "sign up -> dashboard")
        await wait_until(page, "document.querySelector('#ai-pill [data-label]').textContent !== 'AI: checking'", timeout=15.0)
        check(True, "AI status pill fetched: " + (await page.text_content("#ai-pill [data-label]") or ""))
        await page.click("[data-menu-button]")
        greeting = await page.text_content("[data-greeting]")
        check(greeting == "Hi, Browser!", f"Google-style account menu greeting ({greeting})")
        await page.screenshot(path=f"{out}/2-dashboard-menu.png")
        await page.keyboard.press("Escape")

        await page.goto(base + "/podcasts/new?mode=guest")
        await page.fill("#title", "Browser guest prep")
        await page.fill("#theme", "Cold storage for small farmers")
        await page.fill("#talking_points", "Cold storage cuts crop loss by 30 percent")
        await page.uncheck("input[name=auto_generate]")
        await page.click("form[data-once] button:not([type=button]) >> text=Create")
        await page.wait_for_url("**/podcasts/*", timeout=20000)
        await page.wait_for_selector("[data-q-empty]:not(.hidden)", timeout=10000)
        check(True, "guest workspace initialised (JS ran under CSP)")
        await wait_until(page, "document.querySelectorAll('[data-ai-status] li').length >= 2", timeout=15.0)
        check(True, "AI provider list rendered")

        await page.fill("[data-add-text]", "Why did you start your company?")
        await page.click("[data-add-form] button")
        await page.wait_for_selector("[data-q-list] li", timeout=10000)
        check((await page.text_content("[data-practice-question]")) == "Why did you start your company?", "practice panel shows the question")
        await page.fill("[data-answer]", "I started it after a heatwave ruined a whole season of crops near Pune, and I knew solar cold rooms could help.")
        meter = await page.text_content("[data-answer-meter]")
        check("words" in (meter or ""), f"speaking-time meter ({meter})")

        await page.fill("[data-gen-count]", "3")
        await page.click("[data-generate]")
        await wait_until(page, "document.querySelectorAll('[data-q-list] li').length >= 4", timeout=90.0)
        check(True, "AI questions streamed in over Socket.IO")
        await page.wait_for_selector("[data-generate]:not([disabled])", timeout=30000)

        await page.click("[data-submit-answer]")
        await page.wait_for_selector("[data-feedback] .space-y-4", timeout=90000)
        feedback = await page.text_content("[data-feedback]")
        check("Mira's feedback" in (feedback or "") and "/ 10" in (feedback or ""), "answer feedback rendered with score")
        await page.screenshot(path=f"{out}/3-guest-workspace.png", full_page=True)

        await page.click("[data-ask-aloud]")
        try:
            await wait_until(page, "document.querySelector('[data-mira-wrap]').classList.contains('mira-speaking')", timeout=20.0)
            check(True, "Mira speaks (avatar lip-sync active)")
        except Exception:  # noqa: BLE001
            check(False, "Mira speaks (avatar lip-sync active)")
        await page.click("[data-stop-voice]")

        async with page.expect_download(timeout=30000) as download_info:
            await page.click("[data-export] >> nth=0")
        download = await download_info.value
        path = await download.path()
        size = (await asyncio.to_thread(lambda: open(path, "rb").read())) if path else b""
        check(size.startswith(b"%PDF"), f"PDF downloaded ({len(size)} bytes)")

        await page.goto(base + "/podcasts/new?mode=host")
        await page.fill("#title", "Browser host plan")
        await page.fill("#theme", "Hiring the first ten people")
        await page.click("form[data-once] button:not([type=button]) >> text=Create")
        await page.wait_for_url("**/podcasts/*", timeout=20000)
        await wait_until(page, "document.querySelectorAll('[data-q-list] li').length >= 3", timeout=90.0)
        check(True, "host workspace auto-generated questions")
        await page.wait_for_selector("[data-generate]:not([disabled])", timeout=60000)
        await page.click("[data-quality]")
        quality_toast = "[...document.querySelectorAll('#toasts > *')].find(t => /Found:|All clear|No clear repeats/.test(t.textContent))"
        await wait_until(page, f"!!{quality_toast}", timeout=20.0)
        summary = await page.evaluate(f"{quality_toast}.textContent")
        check(True, f"quality & repeat check ({summary.strip()[:90]})")
        await page.wait_for_timeout(1000)  # an AI repeat check, if one was needed, has started by now
        await wait_until(page, "document.querySelectorAll('[data-jobs] .animate-spin').length === 0", timeout=90.0)
        check(True, "AI repeat check (if any) finished")
        await page.evaluate("document.documentElement.classList.add('dark')")
        await page.wait_for_timeout(500)  # let the 150 ms colour transitions finish
        await page.screenshot(path=f"{out}/4-host-workspace-dark.png", full_page=True)
        await page.evaluate("document.documentElement.classList.remove('dark')")

        await page.click("[data-menu-button]")
        await page.click("[data-menu-panel] button:has-text('Sign out')")
        await page.wait_for_url("**/login", timeout=10000)
        chooser = await page.text_content("[data-chooser]")
        check("Browser Tester" in (chooser or ""), "account chooser lists the account by name")
        await page.screenshot(path=f"{out}/5-chooser.png")
        await page.click("[data-account]")
        title = await page.text_content("[data-login-title]")
        check(title == "Hi Browser", f"two-step sign-in greets by name ({title})")
        await page.fill("#password", PASSWORD)
        await page.click("[data-submit]")
        await page.wait_for_url("**/dashboard", timeout=15000)
        check(True, "signed back in")

        # One login per account: a second window signs in to the same account.
        await page.goto(base + "/dashboard")
        other = await browser.new_context(viewport={"width": 1280, "height": 860}, user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"))
        second = await other.new_page()
        await second.goto(base + "/login?add=1")
        await second.fill("#identifier", user)
        await second.click("[data-next]")
        await second.fill("#password", PASSWORD)
        await second.click("[data-submit]")
        await second.wait_for_url("**/dashboard", timeout=15000)
        try:
            await page.wait_for_selector("#signed-out-modal", timeout=10000)
            notice = await page.text_content("#signed-out-modal")
            check("another device" in (notice or "") and "Chrome on Windows" in (notice or ""),
                  "first window told at once why it was signed out")
        except Exception:  # noqa: BLE001
            check(False, "first window told at once why it was signed out")
        await page.screenshot(path=f"{out}/6-signed-out-notice.png")
        await page.reload()
        await page.wait_for_load_state("load")
        print("      after reload:", page.url, flush=True)
        check("/login" in page.url and "signed in on another device" in await page.content(), "reload shows the reason on the sign-in page")
        await other.close()

        relevant = [c for c in console if "favicon" not in c and "401" not in c]
        check(not relevant, "no console errors / CSP violations" + (": " + " | ".join(relevant[:5]) if relevant else ""))
        await browser.close()

    print("\nBROWSER CHECK PASSED" if not problems else f"\nBROWSER CHECK FAILED: {problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/tmp")))
