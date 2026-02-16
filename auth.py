"""
TDSB Authentication Handler.

TDSB uses a hybrid authentication model:
- Microsoft Entra (Azure AD) for identity (username@tdsb.ca)
- Google Workspace for Education (federated via Entra) for Google Classroom
- Brightspace uses TDSB SSO which goes through Entra

This module handles the SSO login flows for both platforms using Playwright
browser automation, since we cannot register an Entra app for API access.
"""

import asyncio
import logging
import os
from playwright.async_api import Page, BrowserContext, Browser, async_playwright

logger = logging.getLogger(__name__)

# Directory to save debug screenshots
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "debug_screenshots")


class TDSBAuth:
    """Handles TDSB authentication for both Google Classroom and Brightspace."""

    # Use the Google Accounts sign-in URL that forces the login flow and
    # redirects back to Classroom after auth.  Going to classroom.google.com
    # directly when not logged in lands on a marketing page.
    GOOGLE_LOGIN_URL = (
        "https://accounts.google.com/ServiceLogin"
        "?continue=https://classroom.google.com/u/0/h"
        "&passive=true"
    )
    BRIGHTSPACE_URL = "https://tdsb.elearningontario.ca/d2l/home"

    def __init__(self, username: str, password: str, debug: bool = False):
        self.username = username
        self.password = password
        self.debug = debug
        self._playwright = None
        self._browser: Browser | None = None
        self._gc_context: BrowserContext | None = None
        self._bs_context: BrowserContext | None = None

    async def start_browser(self, headless: bool = False):
        """Launch the browser using the locally-installed Chrome."""
        self._playwright = await async_playwright().start()
        try:
            # Use the user's installed Chrome so the version is always current
            self._browser = await self._playwright.chromium.launch(
                channel="chrome",
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            logger.info("Launched installed Chrome (headless=%s)", headless)
        except Exception:
            # Fall back to Playwright's bundled Chromium if Chrome isn't installed
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            logger.info("Launched bundled Chromium (headless=%s)", headless)
        if self.debug:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    async def close(self):
        """Clean up browser resources — tolerant of already-closed objects."""
        for label, ctx in [("gc", self._gc_context), ("bs", self._bs_context)]:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception as e:
                    logger.debug("Closing %s context: %s", label, e)
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.debug("Closing browser: %s", e)
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.debug("Stopping playwright: %s", e)
        logger.info("Browser closed")

    async def _screenshot(self, page: Page, name: str):
        """Save a debug screenshot if debug mode is on."""
        if self.debug:
            try:
                path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
                await page.screenshot(path=path, full_page=True)
                logger.debug("Screenshot saved: %s", path)
            except Exception as e:
                logger.debug("Screenshot failed (%s): %s", name, e)

    def _new_context_args(self) -> dict:
        # No custom user_agent — let the real browser send its own current UA
        return {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }

    # ─── Google Classroom Login ─────────────────────────────────────────

    async def login_google_classroom(self) -> BrowserContext:
        """
        Log into Google Classroom via TDSB SSO.

        Flow:
          1. accounts.google.com/ServiceLogin  → shows email input
          2. Enter @tdsb.ca email              → Google detects federated domain
          3. Redirect to Microsoft Entra (login.microsoftonline.com)
          4. Enter password on Entra           → Entra authenticates
          5. Redirect back through Google      → lands on classroom.google.com
        """
        self._gc_context = await self._browser.new_context(**self._new_context_args())
        page = await self._gc_context.new_page()

        # ── Step 1: Navigate to Google sign-in (not classroom.google.com) ──
        logger.info("Navigating to Google sign-in page...")
        await page.goto(self.GOOGLE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("load", timeout=30000)
        await self._screenshot(page, "01_google_signin_page")
        logger.info("Google sign-in page loaded: %s", page.url)

        # ── Step 2: Enter email on the Google sign-in form ──
        await self._handle_google_sign_in(page)

        # ── Step 3+4: Handle Entra login (Google needs generous timeouts) ──
        await self._handle_entra_login_google(page)

        # ── Step 5: Wait for Google Classroom to fully load ──
        await self._wait_for_google_classroom(page)

        logger.info("Google Classroom login complete — url: %s", page.url)
        return self._gc_context

    async def _handle_google_sign_in(self, page: Page):
        """Handle the Google sign-in / account chooser page."""
        current = page.url
        logger.info("Handling Google sign-in — current URL: %s", current)

        # Google sign-in has several possible selectors for the email field
        email_selectors = [
            'input[type="email"]',
            'input#identifierId',
            'input[name="identifier"]',
        ]

        filled = False
        for sel in email_selectors:
            try:
                locator = page.locator(sel)
                await locator.first.wait_for(state="visible", timeout=10000)
                await locator.first.fill(self.username)
                logger.info("Filled email with selector: %s", sel)
                filled = True
                break
            except Exception:
                continue

        if not filled:
            # Maybe we're already past this page or on a different page
            await self._screenshot(page, "02_email_not_found")
            logger.warning(
                "Could not find email input on Google page. URL: %s", page.url
            )
            # Check if there's a "Sign in" or "Use another account" button
            try:
                alt = page.locator(
                    'div:has-text("Use another account"), '
                    'div:has-text("Sign in"), '
                    'button:has-text("Sign in")'
                )
                if await alt.count() > 0:
                    await alt.first.click()
                    await page.wait_for_timeout(2000)
                    # Retry email entry
                    for sel in email_selectors:
                        try:
                            loc = page.locator(sel)
                            await loc.first.wait_for(state="visible", timeout=5000)
                            await loc.first.fill(self.username)
                            filled = True
                            break
                        except Exception:
                            continue
            except Exception:
                pass

        if not filled:
            await self._screenshot(page, "02_email_still_not_found")
            logger.error("FAILED to enter email — page URL: %s", page.url)
            return

        await self._screenshot(page, "02_email_entered")

        # Click "Next" — Google uses either a button#identifierNext or type=submit
        next_selectors = [
            '#identifierNext',
            'button:has-text("Next")',
            'input[type="submit"]',
            'button[type="submit"]',
        ]
        for sel in next_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    logger.info("Clicked Next with selector: %s", sel)
                    break
            except Exception:
                continue

        # Wait for navigation — Google will redirect to Entra for @tdsb.ca
        logger.info("Waiting for redirect to TDSB Entra SSO...")
        try:
            # Wait for either Entra page or a password field
            await page.wait_for_function(
                """() => {
                    return window.location.hostname.includes('microsoftonline') ||
                           window.location.hostname.includes('login.microsoft') ||
                           window.location.hostname.includes('login.live') ||
                           window.location.hostname.includes('tdsb') ||
                           document.querySelector('input[type="password"]') !== null;
                }""",
                timeout=20000,
            )
        except Exception as e:
            logger.warning("Redirect wait: %s — current URL: %s", e, page.url)

        await self._screenshot(page, "03_after_google_next")
        logger.info("After clicking Next — URL: %s", page.url)

    # ─── Google-specific Entra handler (generous timeouts) ───────────

    async def _handle_entra_login_google(self, page: Page):
        """
        Handle Entra login after Google redirect.

        The BssoInterrupt page can take several seconds to auto-submit.
        We use generous timeouts here because Google auth is the first
        login and there's no existing Entra session to reuse.
        """
        source = "google_classroom"
        logger.info("Handling Entra login (Google) — URL: %s", page.url)
        await self._screenshot(page, "04_entra_start_google")

        try:
            # ── 1. Wait for real Entra form (past BssoInterrupt) ──
            logger.info("Waiting for Entra login form (BssoInterrupt may take a few seconds)…")
            username_field = None
            try:
                loc = page.locator('input[name="loginfmt"]')
                await loc.wait_for(state="visible", timeout=30000)
                username_field = loc
                logger.info("Standard Entra username field found")
            except Exception:
                for sel in ['input[name="UserName"]', 'input[name="login"]', 'input[type="email"]']:
                    try:
                        loc = page.locator(sel)
                        await loc.first.wait_for(state="visible", timeout=5000)
                        username_field = loc.first
                        logger.info("Found username field with fallback: %s", sel)
                        break
                    except Exception:
                        continue

            if username_field is None:
                await self._screenshot(page, "04_entra_no_username_google")
                logger.error("No username field found on Entra (Google) — URL: %s", page.url)
                return

            await self._screenshot(page, "05_entra_username_visible_google")

            # ── 2. Enter username and click Next ──
            await username_field.fill(self.username)
            logger.info("Entered username on Entra")
            next_btn = page.locator("#idSIButton9")
            await next_btn.click()
            logger.info("Clicked Next on Entra username page")
            await page.wait_for_timeout(2000)
            await self._screenshot(page, "05_entra_after_next_google")

            # ── 3. Wait for password field ──
            logger.info("Waiting for password field…")
            passwd_loc = page.locator('input[name="passwd"]')
            try:
                await passwd_loc.first.wait_for(state="visible", timeout=15000)
            except Exception:
                for sel in ['input[name="Password"]', 'input[name="password"]', 'input[type="password"]:visible']:
                    try:
                        loc = page.locator(sel)
                        await loc.first.wait_for(state="visible", timeout=5000)
                        passwd_loc = loc
                        break
                    except Exception:
                        continue
                else:
                    await self._screenshot(page, "06_no_password_google")
                    logger.error("No password field (Google) — URL: %s", page.url)
                    return

            await passwd_loc.first.fill(self.password)
            logger.info("Entered password on Entra")
            await self._screenshot(page, "06_password_entered_google")

            # ── 4. Click Sign In ──
            signin_btn = page.locator("#idSIButton9")
            await signin_btn.click()
            logger.info("Clicked Sign In on Entra")
            await page.wait_for_timeout(3000)
            await self._screenshot(page, "07_after_signin_google")
            logger.info("After sign-in — URL: %s", page.url)

            # ── 5. Handle "Stay signed in?" (generous timeouts for Google) ──
            await self._handle_stay_signed_in(page, wait_timeout=8000, post_click_wait=3000)

        except Exception as e:
            await self._screenshot(page, "08_entra_error_google")
            logger.error("Entra login error (Google): %s — URL: %s", e, page.url)

    # ─── Brightspace Entra handler (fast, with SSO auto-complete) ─────

    async def _handle_entra_login(self, page: Page, source: str = ""):
        """
        Handle Entra login for Brightspace.

        Since Google Classroom already established an Entra session, SSO may
        auto-complete and redirect straight to Brightspace with no login form.
        We race the login form against the destination URL.
        """
        logger.info("Handling Entra login (%s) — URL: %s", source, page.url)
        await self._screenshot(page, f"04_entra_start_{source}")

        try:
            # ── 1. Race: login form vs SSO auto-complete ──
            logger.info("Waiting for Entra form or SSO auto-complete…")
            username_field = None
            try:
                await page.wait_for_function(
                    """() => {
                        const el = document.querySelector('input[name="loginfmt"]');
                        if (el && el.offsetParent !== null) return true;
                        const h = window.location.hostname;
                        if (h.includes('elearningontario') || h.includes('classroom.google'))
                            return true;
                        return false;
                    }""",
                    timeout=15000,
                )
                if "elearningontario" in page.url or "classroom.google" in page.url:
                    logger.info("SSO auto-completed — already on destination: %s", page.url)
                    return
                loc = page.locator('input[name="loginfmt"]')
                username_field = loc
                logger.info("Standard Entra username field found")
            except Exception:
                # Check if we redirected during the wait
                if "elearningontario" in page.url:
                    logger.info("SSO auto-completed during fallback — URL: %s", page.url)
                    return
                for sel in ['input[name="UserName"]', 'input[name="login"]', 'input[type="email"]']:
                    try:
                        loc = page.locator(sel)
                        await loc.first.wait_for(state="visible", timeout=3000)
                        username_field = loc.first
                        logger.info("Found username field with fallback: %s", sel)
                        break
                    except Exception:
                        continue

            if username_field is None:
                # Final check — maybe we landed on Brightspace while in fallback
                if "elearningontario" in page.url:
                    logger.info("SSO auto-completed — URL: %s", page.url)
                    return
                await self._screenshot(page, f"04_entra_no_username_{source}")
                logger.error("No username field found on Entra (%s) — URL: %s", source, page.url)
                return

            await self._screenshot(page, f"05_entra_username_visible_{source}")

            # ── 2. Enter username and click Next ──
            await username_field.fill(self.username)
            logger.info("Entered username on Entra")
            next_btn = page.locator("#idSIButton9")
            await next_btn.click()
            logger.info("Clicked Next on Entra username page")
            await page.wait_for_timeout(1000)
            await self._screenshot(page, f"05_entra_after_next_{source}")

            # ── 3. Wait for password field ──
            logger.info("Waiting for password field…")
            passwd_loc = page.locator('input[name="passwd"]')
            try:
                await passwd_loc.first.wait_for(state="visible", timeout=15000)
            except Exception:
                for sel in ['input[name="Password"]', 'input[name="password"]', 'input[type="password"]:visible']:
                    try:
                        loc = page.locator(sel)
                        await loc.first.wait_for(state="visible", timeout=5000)
                        passwd_loc = loc
                        break
                    except Exception:
                        continue
                else:
                    await self._screenshot(page, f"06_no_password_field_{source}")
                    logger.error("No password field (%s) — URL: %s", source, page.url)
                    return

            await passwd_loc.first.fill(self.password)
            logger.info("Entered password on Entra")
            await self._screenshot(page, f"06_password_entered_{source}")

            # ── 4. Click Sign In ──
            signin_btn = page.locator("#idSIButton9")
            await signin_btn.click()
            logger.info("Clicked Sign In on Entra")
            await page.wait_for_timeout(1000)
            await self._screenshot(page, f"07_after_signin_{source}")
            logger.info("After sign-in — URL: %s", page.url)

            # ── 5. Handle "Stay signed in?" ──
            await self._handle_stay_signed_in(page)

        except Exception as e:
            await self._screenshot(page, f"08_entra_error_{source}")
            logger.error("Entra login error (%s): %s — URL: %s", source, e, page.url)

    async def _handle_stay_signed_in(
        self, page: Page, wait_timeout: int = 3000, post_click_wait: int = 1000
    ):
        """Handle the 'Stay signed in?' / 'Don't show this again' prompt."""
        try:
            # Entra "Stay signed in?" has id=idSIButton9 for "Yes"
            stay_yes = page.locator(
                '#idSIButton9, '
                'input[type="submit"][value="Yes"], '
                'button:has-text("Yes")'
            )
            await stay_yes.first.wait_for(state="visible", timeout=wait_timeout)
            await stay_yes.first.click()
            logger.info("Clicked 'Yes' on Stay signed in prompt")
            await page.wait_for_timeout(post_click_wait)
            await self._screenshot(page, "09_after_stay_signed_in")
        except Exception:
            # Prompt didn't appear — that's fine
            logger.debug("No 'Stay signed in' prompt detected")

    async def _wait_for_google_classroom(self, page: Page):
        """Wait for Google Classroom main page to load after SSO."""
        logger.info("Waiting for Google Classroom to load...")
        try:
            # Wait for the URL to contain classroom.google.com
            await page.wait_for_url(
                "**/classroom.google.com/**", timeout=45000
            )
            await page.wait_for_load_state("load", timeout=30000)
            await page.wait_for_timeout(3000)
            await self._screenshot(page, "10_google_classroom_loaded")
            logger.info("Google Classroom loaded: %s", page.url)
        except Exception as e:
            await self._screenshot(page, "10_google_classroom_fail")
            logger.warning(
                "Google Classroom wait issue: %s — final URL: %s", e, page.url
            )

    # ─── Brightspace Login ──────────────────────────────────────────────

    async def login_brightspace(self) -> BrowserContext:
        """
        Log into Brightspace via TDSB SSO.
        Flow: Brightspace landing → click "Staff And Students Login"
              → TDSB SSO (Entra) → login → redirect back to Brightspace.
        """
        self._bs_context = await self._browser.new_context(**self._new_context_args())
        page = await self._bs_context.new_page()

        logger.info("Navigating to Brightspace...")
        await page.goto(
            self.BRIGHTSPACE_URL, wait_until="domcontentloaded", timeout=60000
        )
        await page.wait_for_load_state("load", timeout=30000)
        await self._screenshot(page, "20_brightspace_start")
        logger.info("Brightspace start page: %s", page.url)

        # The landing page has a "Staff And Students Login" button that must
        # be clicked before we get redirected to Entra SSO.
        await self._handle_brightspace_landing(page)

        # Handle Entra login — but if we already have an Entra session from
        # Google Classroom, SSO may auto-complete and skip the login form.
        # Check whether we've already landed on Brightspace first.
        already_on_bs = "elearningontario.ca" in page.url and "/d2l/" in page.url
        if already_on_bs:
            logger.info("Already on Brightspace (SSO auto-completed) — skipping Entra login")
        else:
            await self._handle_entra_login(page, source="brightspace")

        # Wait for Brightspace to load
        await self._wait_for_brightspace(page)

        logger.info("Brightspace login complete — url: %s", page.url)
        return self._bs_context

    async def _handle_brightspace_landing(self, page: Page):
        """Click 'Staff And Students Login' on the Brightspace landing page."""
        try:
            # Look for the green "Staff And Students Login" button
            btn_selectors = [
                'a:has-text("Staff And Students Login")',
                'button:has-text("Staff And Students Login")',
                'a:has-text("Staff and Students")',
                'a:has-text("Staff")',
            ]
            clicked = False
            for sel in btn_selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click()
                        logger.info("Clicked Brightspace login button: %s", sel)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await self._screenshot(page, "20_no_staff_button")
                logger.warning(
                    "Could not find 'Staff And Students Login' — URL: %s", page.url
                )
                return

            # Wait for the redirect to Entra SSO
            logger.info("Waiting for Entra SSO redirect after clicking login...")
            try:
                await page.wait_for_function(
                    """() => {
                        return window.location.hostname.includes('microsoftonline') ||
                               window.location.hostname.includes('login.microsoft') ||
                               window.location.hostname.includes('login.live') ||
                               document.querySelector('input[type="password"]') !== null ||
                               document.querySelector('input[name="loginfmt"]') !== null;
                    }""",
                    timeout=20000,
                )
            except Exception as e:
                logger.warning("Entra redirect wait: %s — URL: %s", e, page.url)

            await page.wait_for_timeout(2000)
            await self._screenshot(page, "20_after_staff_login_click")
            logger.info("After staff login click — URL: %s", page.url)

        except Exception as e:
            await self._screenshot(page, "20_landing_error")
            logger.warning("Brightspace landing handling error: %s", e)

    async def _wait_for_brightspace(self, page: Page):
        """Wait for Brightspace homepage to load."""
        try:
            # If not already on Brightspace, wait for the redirect
            if "elearningontario.ca" not in page.url:
                await page.wait_for_url(
                    "**/elearningontario.ca/**", timeout=30000
                )
            # domcontentloaded is enough — 'load' hangs on Brightspace's
            # heavy async resource loading.
            await page.wait_for_load_state("domcontentloaded", timeout=15000)

            # Dismiss the "Your browser is looking a little retro" modal
            # that Brightspace shows for older Chrome user-agents.
            await self._dismiss_brightspace_browser_warning(page)

            await self._screenshot(page, "21_brightspace_loaded")
            logger.info("Brightspace loaded: %s", page.url)
        except Exception as e:
            await self._screenshot(page, "21_brightspace_fail")
            logger.warning(
                "Brightspace wait issue: %s — final URL: %s", e, page.url
            )

    async def _dismiss_brightspace_browser_warning(self, page: Page):
        """Dismiss the 'Your browser is looking a little retro' dialog if present."""
        try:
            got_it = page.locator(
                'button:has-text("Got It"), '
                'button:has-text("Got it"), '
                'a:has-text("Got It"), '
                'a:has-text("Got it")'
            )
            if await got_it.count() > 0 and await got_it.first.is_visible():
                await got_it.first.click()
                logger.info("Dismissed 'browser retro' warning dialog")
                await page.wait_for_timeout(1000)
        except Exception:
            pass  # No dialog — that's fine

    # ─── Convenience ────────────────────────────────────────────────────

    @property
    def gc_context(self) -> BrowserContext | None:
        return self._gc_context

    @property
    def bs_context(self) -> BrowserContext | None:
        return self._bs_context
