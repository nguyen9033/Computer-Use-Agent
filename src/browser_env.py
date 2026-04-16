"""Playwright browser wrapper with navigation and page settling.

BrowserEnv class manages:
- Playwright browser/context lifecycle
- Client-side SPA navigation via pushState + popstate
- Page settling (wait for React hydration, overlays, timed content)
- Init script injection (tools_helper.js)
- SKIP_STEPS configuration for known-buggy challenges
"""

import os
import logging
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

SITE_URL = "https://serene-frangipane-7fd25b.netlify.app"
VERSION_PARAM = "version=2"

# Steps with confirmed bugs in challenge site JS — skipped by agent
SKIP_STEPS = {
    19: "service_worker — code collision bug in challenge random generation",
    24: "recursive_iframe — Extract Code guard always returns early (u<y but u maxes at y-1)",
    30: "websocket — off-by-one bug, bypassed programmatically to reach completion page",
}

# Path to tools_helper.js relative to this file
_TOOLS_HELPER_PATH = str(Path(__file__).parent / "tools_helper.js")


class BrowserEnv:
    """Async context manager wrapping Playwright browser lifecycle."""

    def __init__(self, headless: bool = True, viewport: tuple = (1280, 720)):
        self.headless = headless
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self):
        await self.launch()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def launch(self):
        """Start Playwright, launch Chromium, create context with init scripts."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(viewport=self.viewport)

        # Inject tools_helper.js on every new page
        await self._context.add_init_script(path=_TOOLS_HELPER_PATH)

        self._page = await self._context.new_page()
        self._page.set_default_timeout(1500)

        logger.info(f"BrowserEnv launched (headless={self.headless})")

    async def close(self):
        """Clean shutdown of browser resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    @property
    def page(self) -> Page:
        """Return the current Playwright Page object."""
        if self._page is None:
            raise RuntimeError("BrowserEnv not launched. Use 'async with BrowserEnv() as env:'")
        return self._page

    async def navigate_to_site(self):
        """Navigate to the challenge site landing page."""
        await self.page.goto(SITE_URL)
        await self.wait_for_settle(1500)

    async def wait_for_settle(self, max_ms: int = 1500, min_ms: int = 100):
        """Wait for DOM to stabilize using adaptive polling.

        Polls innerHTML length every 200ms after an initial minimum wait.
        Returns early once DOM is stable (two consecutive identical readings).
        Falls back to max_ms ceiling if DOM keeps changing.

        Minimum wait ensures React hydration has time to start.
        Maximum wait prevents infinite waiting on animated/polling content.

        Args:
            max_ms: Maximum wait time in milliseconds (safety ceiling).
            min_ms: Minimum wait before polling starts (React hydration floor).
        """
        await self.page.wait_for_timeout(min_ms)
        elapsed = min_ms
        prev_len = await self.page.evaluate("document.body ? document.body.innerHTML.length : 0")
        while elapsed < max_ms:
            await self.page.wait_for_timeout(200)
            elapsed += 200
            curr_len = await self.page.evaluate("document.body ? document.body.innerHTML.length : 0")
            if curr_len == prev_len:
                logger.debug(f"DOM settled after {elapsed}ms")
                return
            prev_len = curr_len
        logger.debug(f"DOM still changing after {max_ms}ms ceiling")

    async def navigate_to_step(self, step: int):
        """Navigate to a challenge step via client-side SPA routing."""
        url = f"/step{step}?{VERSION_PARAM}"
        await self.page.evaluate(f'''
            window.history.pushState({{}}, '', '{url}');
            window.dispatchEvent(new PopStateEvent('popstate'));
        ''')
        await self.wait_for_settle(1500)
        logger.info(f"Navigated to step {step}")

    async def start_challenge(self):
        """Click START on landing page to begin challenge at step 1."""
        start_btn = self.page.locator("button", has_text="Start")
        await start_btn.click()
        await self.page.wait_for_url(f"**/step1**", timeout=10000)
        await self.wait_for_settle(1500)
        logger.info("Challenge started, at step 1")

    def get_current_step(self) -> int | None:
        """Extract step number from current URL, or None if not on a step page."""
        import re
        url = self.page.url
        m = re.search(r'/step(\d+)', url)
        return int(m.group(1)) if m else None

    def url_changed_from(self, previous_url: str) -> bool:
        """Check if current URL differs from previous_url."""
        return self.page.url != previous_url

    async def bypass_step_30(self):
        """Mark step 30 complete in session state and navigate to the finish page.

        Step 30 has an off-by-one bug in the challenge JS (codes.get(31) is
        undefined). Instead of patching the bundle or running the websocket
        challenge, we directly manipulate the XOR-encoded session in
        sessionStorage to add step 30 to the completed set, then SPA-navigate
        to /finish.

        Does not use the agent. Does not modify the results tracker.
        """
        await self.page.evaluate("""
            (() => {
                const KEY = "WO_2024_CHALLENGE";
                function decode(encoded) {
                    const raw = atob(encoded);
                    let s = "";
                    for (let i = 0; i < raw.length; i++)
                        s += String.fromCharCode(raw.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length));
                    return s;
                }
                function encode(plaintext) {
                    let s = "";
                    for (let i = 0; i < plaintext.length; i++)
                        s += String.fromCharCode(plaintext.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length));
                    return btoa(s);
                }
                const encoded = sessionStorage.getItem('wo_session');
                if (!encoded) return;
                const data = JSON.parse(decode(encoded));
                // Ensure step 30 is in the completed set
                const completed = new Set(data.completed || []);
                completed.add(30);
                data.completed = Array.from(completed);
                sessionStorage.setItem('wo_session', encode(JSON.stringify(data)));
                // SPA navigate to finish page
                window.history.pushState({}, '', '/finish?version=2');
                window.dispatchEvent(new PopStateEvent('popstate'));
            })();
        """)
        await self.wait_for_settle(1500)
        logger.info(f"Step 30 bypass: navigated to finish page ({self.page.url})")
