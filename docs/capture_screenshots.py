"""Regenerate the README screenshots (Playwright/Chromium) against a THROWAWAY stack - never the
real one. The first run signs up a fictional demo account and lets the configured AI chain fill
two podcasts through the normal UI (slow on a small model); later runs reuse them and only retake
the pictures. Everything shown is produced by the app itself - nothing is painted in.

    python docs/capture_screenshots.py http://app:8000 /out
"""

from __future__ import annotations

import asyncio
import sys
import time

from playwright.async_api import Page, async_playwright

USER, NAME, EMAIL = "alexrivera", "Alex Rivera", "alex.rivera@example.com"
PASSWORD = "Demo-Screenshot-Passphrase-26"
WINDOWS_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
AI_WAIT = 900.0  # seconds - a small LAN model can be slow

HOST = {
    "title": "Cold Chain Stories - Ep. 12",
    "theme": "How solar cold rooms are cutting crop loss for small farmers",
    "show_name": "Climate Founders",
    "guest_name": "Priya Nair",
    "audience": "Early-stage climate founders",
    "tone": "Warm and curious",
    "guest_bio": "Founder of FrostLine, which builds solar-powered cold rooms for farming co-operatives in India. "
    "Ten years in supply chains before that; 400 rooms installed in five states.",
    "target_minutes": "30",
    "instructions": "Avoid politics\nAsk at least one question about failure",
}
HOST_MANUAL = [  # (category, seconds, text) - the 2nd rewords the 1st, the 3rd is a yes/no question
    ("opener", "90", "What first pulled you toward cold-chain technology?"),
    ("core", "120", "What originally drew you to cold-chain technology?"),
    ("challenge", "150", "Do you think investors understand farming?"),
]
GUEST = {
    "title": "Guest spot - The Farm Tech Show",
    "theme": "Building a hardware startup for smallholder farmers",
    "show_name": "The Farm Tech Show",
    "host_name": "Sam Okafor",
    "audience": "Agritech founders and investors",
    "guest_bio": "Co-founder of Rootwise, which makes low-cost soil sensors for small farms. Eight years as an agronomist before that.",
    "talking_points": "Our sensors cost under $40 and last three seasons\n"
    "Farmers using them cut fertiliser use by about 20 percent\nWe sell through farmer co-operatives, not one farm at a time",
    "target_minutes": "45",
}
GUEST_QUESTION = "Why did you start Rootwise?"
GUEST_ANSWER = (
    "So basically I was an agronomist for about eight years and I kept seeing farmers guess how much fertiliser to use, "
    "and usually they used way too much because they were scared of losing the crop. One season I visited a co-op in "
    "Kenya where half the farms had burned their soil and yields were dropping, and that kind of stuck with me. So we "
    "built a cheap soil sensor that tells you what the soil actually needs, and we sell it through co-ops. Now a lot of "
    "farmers use it and they use less fertiliser."
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


async def wait_until(page: Page, expression: str, timeout: float = 15.0) -> None:
    """Poll a JS expression via DevTools (wait_for_function needs 'unsafe-eval', which the CSP forbids)."""
    deadline = time.monotonic() + timeout
    while not await page.evaluate(expression):
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for: {expression}")
        await asyncio.sleep(0.25)


async def settle(page: Page) -> None:
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(600)  # colour transitions and fonts


async def element_shot(page: Page, selector: str, path: str) -> None:
    """Screenshot one element without the sticky header drawn over it (CSSOM - allowed by the CSP)."""
    await page.evaluate("document.querySelector('header').style.visibility = 'hidden'")
    await page.locator(selector).screenshot(path=path)
    await page.evaluate("document.querySelector('header').style.visibility = ''")


async def sign_in(page: Page, base: str) -> bool:
    await page.goto(base + "/login?add=1")
    await page.fill("#identifier", USER)
    await page.click("[data-next]")
    await page.fill("#password", PASSWORD)
    await page.click("[data-submit]")
    try:
        await page.wait_for_url("**/dashboard", timeout=8000)
        return True
    except Exception:  # noqa: BLE001 - the account does not exist yet
        return False


async def sign_up(page: Page, base: str) -> None:
    await page.goto(base + "/signup")
    for field, value in (("display_name", NAME), ("username", USER), ("email", EMAIL), ("password", PASSWORD), ("confirm_password", PASSWORD)):
        await page.fill(f"#{field}", value)
    await page.wait_for_timeout(2300)  # the form rejects bot-speed submissions
    await page.click("form[data-signup] button:not([type=button])")
    await page.wait_for_url("**/dashboard", timeout=20000)


async def create_podcast(page: Page, base: str, mode: str, fields: dict) -> str:
    await page.goto(f"{base}/podcasts/new?mode={mode}")
    for field, value in fields.items():
        await page.fill(f"#{field}", value)
    await page.uncheck("input[name=auto_generate]")
    await page.click("form[data-once] button:not([type=button]) >> text=Create")
    await page.wait_for_url("**/podcasts/*", timeout=20000)
    await page.wait_for_selector("[data-q-empty]:not(.hidden)", timeout=10000)
    return page.url


async def add_question(page: Page, category: str, seconds: str, text: str) -> None:
    before = await page.locator("[data-q-list] li").count()
    await page.select_option("[data-add-category]", category)
    await page.fill("[data-add-seconds]", seconds)
    await page.fill("[data-add-text]", text)
    await page.click("[data-add-form] button")
    await wait_until(page, f"document.querySelectorAll('[data-q-list] li').length > {before}")


async def generate(page: Page, count: int) -> None:
    await page.fill("[data-gen-count]", str(count))
    await page.click("[data-generate]")
    await page.wait_for_timeout(1500)
    await wait_until(page, "!document.querySelector('[data-generate]').disabled", timeout=AI_WAIT)


async def seed(page: Page, base: str) -> None:
    log("seeding: host podcast")
    await create_podcast(page, base, "host", HOST)
    for category, seconds, text in HOST_MANUAL:
        await add_question(page, category, seconds, text)
    await generate(page, 5)
    log(f"  host questions: {await page.locator('[data-q-list] li').count()}")

    log("seeding: guest podcast")
    await create_podcast(page, base, "guest", GUEST)
    await add_question(page, "opener", "120", GUEST_QUESTION)
    await page.fill("[data-answer]", GUEST_ANSWER)
    await generate(page, 3)
    log(f"  guest questions: {await page.locator('[data-q-list] li').count()}")
    await page.click("[data-submit-answer]")
    await page.wait_for_selector("[data-feedback] .space-y-4", timeout=AI_WAIT * 1000)
    log("  feedback received")


async def open_podcast(page: Page, base: str, title: str) -> None:
    await page.goto(base + "/dashboard")
    await page.click(f"a:has-text('{title}')")
    await page.wait_for_url("**/podcasts/*", timeout=10000)
    await wait_until(page, "document.querySelectorAll('[data-q-list] li').length > 0")
    await settle(page)


async def main(base: str, out: str) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ---- signed-out home page: light, dark, phone
        for name, scheme, viewport, scale in (
            ("home", "light", {"width": 1280, "height": 800}, 2),
            ("home-dark", "dark", {"width": 1280, "height": 800}, 2),
            ("home-mobile", "dark", {"width": 390, "height": 844}, 2),
        ):
            context = await browser.new_context(viewport=viewport, device_scale_factor=scale, color_scheme=scheme,
                                                is_mobile=name.endswith("mobile"), has_touch=name.endswith("mobile"))
            page = await context.new_page()
            await page.goto(base + "/")
            await settle(page)
            await page.screenshot(path=f"{out}/{name}.png")
            if name == "home":
                for section in ("modes-title", "how-title", "ai-title"):
                    await element_shot(page, f"section[aria-labelledby={section}]", f"{out}/home-{section.split('-')[0]}.png")
                await element_shot(page, "#problems", f"{out}/home-problems.png")
            await context.close()
            log(f"captured {name}")

        # ---- signed in (one login per account: everything below shares one browser context)
        context = await browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2, color_scheme="light")
        page = await context.new_page()
        if not await sign_in(page, base):
            await sign_up(page, base)
            await seed(page, base)
        await page.goto(base + "/dashboard")
        await wait_until(page, "document.querySelector('#ai-pill [data-label]').textContent !== 'AI: checking'")
        await settle(page)
        await page.click("[data-menu-button]")
        await page.wait_for_timeout(300)
        await page.screenshot(path=f"{out}/dashboard.png")
        await page.keyboard.press("Escape")
        log("captured dashboard")

        await open_podcast(page, base, HOST["title"])
        await page.set_viewport_size({"width": 1280, "height": 1440})  # down to the flagged questions
        await settle(page)
        await page.screenshot(path=f"{out}/host-workspace.png")
        await element_shot(page, "[data-q-list]", f"{out}/host-questions.png")
        log("captured host workspace")

        await page.set_viewport_size({"width": 1280, "height": 1100})
        await open_podcast(page, base, GUEST["title"])
        await page.emulate_media(color_scheme="dark")
        await page.reload()
        await wait_until(page, "document.querySelectorAll('[data-q-list] li').length > 0")
        await page.click(f"[data-q-list] li button:text-is('{GUEST_QUESTION}')")  # selects it (Mira reads it aloud)
        await page.wait_for_selector("[data-feedback] .space-y-4", timeout=15000)
        await page.fill("[data-answer]", GUEST_ANSWER)  # the answer as it was submitted
        await settle(page)
        await page.screenshot(path=f"{out}/guest-workspace-dark.png")
        await element_shot(page, "[data-practice]", f"{out}/guest-feedback-dark.png")
        await page.emulate_media(color_scheme="light")
        await page.set_viewport_size({"width": 1280, "height": 800})
        log("captured guest workspace")

        await page.goto(base + "/dashboard")
        await page.click("[data-menu-button]")
        await page.click("[data-menu-panel] button:has-text('Sign out')")
        await page.wait_for_url("**/login", timeout=10000)
        await settle(page)
        await page.screenshot(path=f"{out}/chooser.png")
        await page.click("[data-account]")
        await page.fill("#password", PASSWORD)
        await page.click("[data-submit]")
        await page.wait_for_url("**/dashboard", timeout=15000)
        log("captured account chooser")

        other = await browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=WINDOWS_CHROME)
        second = await other.new_page()
        await second.goto(base + "/login?add=1")
        await second.fill("#identifier", USER)
        await second.click("[data-next]")
        await second.fill("#password", PASSWORD)
        await second.click("[data-submit]")
        await second.wait_for_url("**/dashboard", timeout=15000)
        await page.wait_for_selector("#signed-out-modal", timeout=15000)
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{out}/signed-out.png")
        log("captured signed-out notice")
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/tmp")))
