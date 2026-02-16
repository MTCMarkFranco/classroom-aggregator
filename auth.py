"""
TDSB Authentication Handler.

TDSB uses a hybrid authentication model:
- Microsoft Entra (Azure AD) for identity (username@tdsb.ca)
- Google Workspace for Education (federated via Entra) for Google Classroom
- Brightspace uses TDSB SSO which goes through Entra

This module handles the SSO login flows for both platforms using Selenium
browser automation, since we cannot register an Entra app for API access.
"""

import logging
import os
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)

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
        self._driver: webdriver.Chrome = None  # type: ignore[assignment]  # set in start_browser()
        self._tmp_profile: str | None = None

    # ─── Browser lifecycle ──────────────────────────────────────────────

    def start_browser(self, headless: bool = False):
        """Launch the browser using Selenium WebDriver."""
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--lang=en-US")
        # Force a fresh profile so existing login cookies are not reused
        self._tmp_profile = tempfile.mkdtemp(prefix="classroom_chrome_")
        options.add_argument(f"--user-data-dir={self._tmp_profile}")
        # Disable Windows SSO / Primary Refresh Token so the OS doesn't
        # inject the machine's corporate Microsoft account automatically.
        options.add_argument("--disable-features=WebAccountManager")
        options.add_argument("--auth-server-allowlist=_")
        options.add_argument("--auth-negotiate-delegate-allowlist=_")
        # Suppress automation info bars
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        try:
            # Default: let Selenium Manager find Chrome + chromedriver
            self._driver = webdriver.Chrome(options=options)
            logger.info("Launched Chrome (headless=%s)", headless)
        except WebDriverException:
            # Fallback: try system Chromium (Raspberry Pi)
            try:
                options.binary_location = "/usr/bin/chromium-browser"
                service = Service("/usr/bin/chromedriver")
                self._driver = webdriver.Chrome(service=service, options=options)
                logger.info("Launched system Chromium (headless=%s)", headless)
            except WebDriverException:
                # Another common Chromium path
                options.binary_location = "/usr/bin/chromium"
                service = Service("/usr/bin/chromedriver")
                self._driver = webdriver.Chrome(service=service, options=options)
                logger.info("Launched Chromium at /usr/bin/chromium (headless=%s)", headless)

        self._driver.set_script_timeout(30)
        self._driver.implicitly_wait(0)  # We use explicit waits

        if self.debug:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def close(self):
        """Clean up browser resources."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception as e:
                logger.debug("Closing browser: %s", e)
        # Remove temporary Chrome profile
        if self._tmp_profile and os.path.isdir(self._tmp_profile):
            import shutil
            try:
                shutil.rmtree(self._tmp_profile, ignore_errors=True)
            except Exception:
                pass
        logger.info("Browser closed")

    # ─── Helpers ────────────────────────────────────────────────────────

    def _screenshot(self, name: str):
        """Save a debug screenshot if debug mode is on."""
        if self.debug and self._driver:
            try:
                path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
                self._driver.save_screenshot(path)
                logger.debug("Screenshot saved: %s", path)
            except Exception as e:
                logger.debug("Screenshot failed (%s): %s", name, e)

    def _wait_for_page_load(self, timeout: float = 30):
        """Wait for the page to finish loading (document.readyState == complete)."""
        try:
            WebDriverWait(self._driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            logger.debug("Page load wait timed out")

    def _find_visible(self, css_selector: str, timeout: float = 10):
        """Wait for and return a visible element matching *css_selector*, or ``None``."""
        try:
            return WebDriverWait(self._driver, timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, css_selector))
            )
        except TimeoutException:
            return None

    def _find_clickable(self, css_selector: str, timeout: float = 10):
        """Wait for and return a clickable element, or ``None``."""
        try:
            return WebDriverWait(self._driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, css_selector))
            )
        except TimeoutException:
            return None

    def _find_by_text(self, tag: str, text: str, timeout: float = 5):
        """Find an element by *tag* whose subtree contains *text*."""
        try:
            if "'" not in text:
                xpath = f"//{tag}[contains(., '{text}')]"
            elif '"' not in text:
                xpath = f'//{tag}[contains(., "{text}")]'
            else:
                # Escape with concat()
                parts = text.split("'")
                concat = ", \"'\", ".join(f"'{p}'" for p in parts)
                xpath = f"//{tag}[contains(., concat({concat}))]"
            return WebDriverWait(self._driver, timeout).until(
                EC.visibility_of_element_located((By.XPATH, xpath))
            )
        except TimeoutException:
            return None

    # ─── Google Classroom Login ─────────────────────────────────────────

    def login_google_classroom(self) -> webdriver.Chrome:
        """
        Log into Google Classroom via TDSB SSO.

        Flow:
          1. accounts.google.com/ServiceLogin  → shows email input
          2. Enter @tdsb.ca email              → Google detects federated domain
          3. Redirect to Microsoft Entra (login.microsoftonline.com)
          4. Enter password on Entra           → Entra authenticates
          5. Redirect back through Google      → lands on classroom.google.com
        """
        # Sign out of any existing Microsoft Entra session first so the
        # OS/corporate account doesn't get auto-selected by BssoInterrupt.
        logger.info("Clearing any existing Microsoft Entra session…")
        try:
            self._driver.get(
                "https://login.microsoftonline.com/common/oauth2/v2.0/logout"
                "?post_logout_redirect_uri=https://accounts.google.com"
            )
            time.sleep(3)
        except Exception as e:
            logger.debug("Entra logout pre-step: %s", e)

        logger.info("Navigating to Google sign-in page...")
        self._driver.get(self.GOOGLE_LOGIN_URL)
        self._wait_for_page_load()
        self._screenshot("01_google_signin_page")
        logger.info("Google sign-in page loaded: %s", self._driver.current_url)

        # Step 2: Enter email on the Google sign-in form
        self._handle_google_sign_in()

        # Step 3+4: Handle Entra login (Google needs generous timeouts)
        self._handle_entra_login_google()

        # Step 5: Wait for Google Classroom to fully load
        self._wait_for_google_classroom()

        logger.info("Google Classroom login complete — url: %s", self._driver.current_url)
        return self._driver

    def _handle_google_sign_in(self):
        """Handle the Google sign-in / account chooser page."""
        logger.info("Handling Google sign-in — current URL: %s", self._driver.current_url)

        email_selectors = [
            'input[type="email"]',
            'input#identifierId',
            'input[name="identifier"]',
        ]

        filled = False
        for sel in email_selectors:
            el = self._find_visible(sel, timeout=10)
            if el:
                el.clear()
                el.send_keys(self.username)
                logger.info("Filled email with selector: %s", sel)
                filled = True
                break

        if not filled:
            self._screenshot("02_email_not_found")
            logger.warning(
                "Could not find email input on Google page. URL: %s",
                self._driver.current_url,
            )
            # Check if there's a "Use another account" or "Sign in" button
            try:
                for text in ["Use another account", "Sign in"]:
                    btn = self._find_by_text("div", text, timeout=3)
                    if not btn:
                        btn = self._find_by_text("button", text, timeout=2)
                    if btn:
                        btn.click()
                        time.sleep(2)
                        break
                # Retry email entry
                for sel in email_selectors:
                    el = self._find_visible(sel, timeout=5)
                    if el:
                        el.clear()
                        el.send_keys(self.username)
                        filled = True
                        break
            except Exception:
                pass

        if not filled:
            self._screenshot("02_email_still_not_found")
            logger.error("FAILED to enter email — page URL: %s", self._driver.current_url)
            return

        self._screenshot("02_email_entered")

        # Click "Next"
        next_selectors = [
            "#identifierNext",
            'input[type="submit"]',
            'button[type="submit"]',
        ]
        clicked = False
        for sel in next_selectors:
            btn = self._find_clickable(sel, timeout=3)
            if btn:
                btn.click()
                logger.info("Clicked Next with selector: %s", sel)
                clicked = True
                break

        if not clicked:
            btn = self._find_by_text("button", "Next", timeout=3)
            if btn:
                btn.click()
                logger.info("Clicked Next via text match")

        # Wait for redirect to Entra SSO
        logger.info("Waiting for redirect to TDSB Entra SSO...")
        try:
            WebDriverWait(self._driver, 20).until(
                lambda d: (
                    any(
                        x in d.current_url
                        for x in ["microsoftonline", "login.microsoft", "login.live", "tdsb"]
                    )
                    or len(d.find_elements(By.CSS_SELECTOR, 'input[type="password"]')) > 0
                )
            )
        except TimeoutException:
            logger.warning("Redirect wait timeout — URL: %s", self._driver.current_url)

        self._screenshot("03_after_google_next")
        logger.info("After clicking Next — URL: %s", self._driver.current_url)

    # ─── Google-specific Entra handler (generous timeouts) ───────────

    def _handle_entra_login_google(self):
        """
        Handle Entra login after Google redirect.

        The BssoInterrupt page can take several seconds to auto-submit.
        We use generous timeouts here because Google auth is the first
        login and there's no existing Entra session to reuse.
        """
        logger.info("Handling Entra login (Google) — URL: %s", self._driver.current_url)
        self._screenshot("04_entra_start_google")

        try:
            # 0. Check for AADSTS error (wrong account auto-selected by Windows SSO)
            self._handle_entra_wrong_account_error()

            # 1. Wait for real Entra form (past BssoInterrupt)
            logger.info(
                "Waiting for Entra login form (BssoInterrupt may take a few seconds)…"
            )
            username_field = self._find_visible('input[name="loginfmt"]', timeout=30)
            if not username_field:
                for sel in [
                    'input[name="UserName"]',
                    'input[name="login"]',
                    'input[type="email"]',
                ]:
                    username_field = self._find_visible(sel, timeout=5)
                    if username_field:
                        logger.info("Found username field with fallback: %s", sel)
                        break

            if username_field is None:
                self._screenshot("04_entra_no_username_google")
                logger.error(
                    "No username field found on Entra (Google) — URL: %s",
                    self._driver.current_url,
                )
                return

            self._screenshot("05_entra_username_visible_google")

            # 2. Enter username and click Next
            username_field.clear()
            username_field.send_keys(self.username)
            logger.info("Entered username on Entra")
            btn = self._find_clickable("#idSIButton9", timeout=5)
            if btn:
                btn.click()
            logger.info("Clicked Next on Entra username page")
            time.sleep(2)
            self._screenshot("05_entra_after_next_google")

            # 3. Wait for password field
            logger.info("Waiting for password field…")
            passwd_field = self._find_visible('input[name="passwd"]', timeout=15)
            if not passwd_field:
                for sel in [
                    'input[name="Password"]',
                    'input[name="password"]',
                    'input[type="password"]',
                ]:
                    passwd_field = self._find_visible(sel, timeout=5)
                    if passwd_field:
                        break
                if not passwd_field:
                    self._screenshot("06_no_password_google")
                    logger.error(
                        "No password field (Google) — URL: %s",
                        self._driver.current_url,
                    )
                    return

            passwd_field.clear()
            passwd_field.send_keys(self.password)
            logger.info("Entered password on Entra")
            self._screenshot("06_password_entered_google")

            # 4. Click Sign In
            btn = self._find_clickable("#idSIButton9", timeout=5)
            if btn:
                btn.click()
            logger.info("Clicked Sign In on Entra")
            time.sleep(3)
            self._screenshot("07_after_signin_google")
            logger.info("After sign-in — URL: %s", self._driver.current_url)

            # 5. Handle "Stay signed in?" (generous timeouts for Google)
            self._handle_stay_signed_in(wait_timeout=8, post_click_wait=3)

        except Exception as e:
            self._screenshot("08_entra_error_google")
            logger.error(
                "Entra login error (Google): %s — URL: %s",
                e,
                self._driver.current_url,
            )

    def _handle_entra_wrong_account_error(self):
        """
        Detect and recover from AADSTS50178 / other errors where Windows SSO
        auto-submitted with the wrong Microsoft account (e.g. a corporate
        marfra@microsoft.com instead of the TDSB student account).
        """
        page_text = self._driver.page_source or ""
        if "AADSTS" in page_text or "does not exist in tenant" in page_text:
            logger.warning("Detected Entra wrong-account error — recovering…")
            self._screenshot("04_entra_wrong_account")

            # Try clicking any "sign in with a different account" links
            for text in [
                "Sign in with a different account",
                "sign in with another account",
                "Use a different account",
                "Sign out and sign in",
                "Sign out",
            ]:
                link = self._find_by_text("a", text, timeout=2)
                if link:
                    link.click()
                    logger.info("Clicked '%s' to recover from wrong-account", text)
                    time.sleep(3)
                    self._screenshot("04_entra_after_recovery_click")
                    return

            # Fallback: navigate directly to Entra logout, then restart
            logger.info("No recovery link found — signing out of Entra explicitly")
            self._driver.get(
                "https://login.microsoftonline.com/common/oauth2/v2.0/logout"
                "?post_logout_redirect_uri=https://accounts.google.com"
            )
            time.sleep(3)

    # ─── Brightspace Entra handler (fast, with SSO auto-complete) ─────

    def _handle_entra_login(self, source: str = ""):
        """
        Handle Entra login for Brightspace.

        Since Google Classroom already established an Entra session, SSO may
        auto-complete and redirect straight to Brightspace with no login form.
        We race the login form against the destination URL.
        """
        logger.info("Handling Entra login (%s) — URL: %s", source, self._driver.current_url)
        self._screenshot(f"04_entra_start_{source}")

        try:
            # 1. Race: login form vs SSO auto-complete
            logger.info("Waiting for Entra form or SSO auto-complete…")
            username_field = None
            try:
                WebDriverWait(self._driver, 15).until(
                    lambda d: (
                        # loginfmt field is visible
                        any(
                            e.is_displayed()
                            for e in d.find_elements(
                                By.CSS_SELECTOR, 'input[name="loginfmt"]'
                            )
                        )
                        # Or we've already arrived at the destination
                        or "elearningontario" in d.current_url
                        or "classroom.google" in d.current_url
                    )
                )
                if (
                    "elearningontario" in self._driver.current_url
                    or "classroom.google" in self._driver.current_url
                ):
                    logger.info(
                        "SSO auto-completed — already on destination: %s",
                        self._driver.current_url,
                    )
                    return
                username_field = self._find_visible('input[name="loginfmt"]', timeout=2)
                logger.info("Standard Entra username field found")
            except TimeoutException:
                if "elearningontario" in self._driver.current_url:
                    logger.info(
                        "SSO auto-completed during fallback — URL: %s",
                        self._driver.current_url,
                    )
                    return
                for sel in [
                    'input[name="UserName"]',
                    'input[name="login"]',
                    'input[type="email"]',
                ]:
                    username_field = self._find_visible(sel, timeout=3)
                    if username_field:
                        logger.info("Found username field with fallback: %s", sel)
                        break

            if username_field is None:
                if "elearningontario" in self._driver.current_url:
                    logger.info("SSO auto-completed — URL: %s", self._driver.current_url)
                    return
                self._screenshot(f"04_entra_no_username_{source}")
                logger.error(
                    "No username field found on Entra (%s) — URL: %s",
                    source,
                    self._driver.current_url,
                )
                return

            self._screenshot(f"05_entra_username_visible_{source}")

            # 2. Enter username and click Next
            username_field.clear()
            username_field.send_keys(self.username)
            logger.info("Entered username on Entra")
            btn = self._find_clickable("#idSIButton9", timeout=5)
            if btn:
                btn.click()
            logger.info("Clicked Next on Entra username page")
            time.sleep(1)
            self._screenshot(f"05_entra_after_next_{source}")

            # 3. Wait for password field
            logger.info("Waiting for password field…")
            passwd_field = self._find_visible('input[name="passwd"]', timeout=15)
            if not passwd_field:
                for sel in [
                    'input[name="Password"]',
                    'input[name="password"]',
                    'input[type="password"]',
                ]:
                    passwd_field = self._find_visible(sel, timeout=5)
                    if passwd_field:
                        break
                if not passwd_field:
                    self._screenshot(f"06_no_password_field_{source}")
                    logger.error(
                        "No password field (%s) — URL: %s",
                        source,
                        self._driver.current_url,
                    )
                    return

            passwd_field.clear()
            passwd_field.send_keys(self.password)
            logger.info("Entered password on Entra")
            self._screenshot(f"06_password_entered_{source}")

            # 4. Click Sign In
            btn = self._find_clickable("#idSIButton9", timeout=5)
            if btn:
                btn.click()
            logger.info("Clicked Sign In on Entra")
            time.sleep(1)
            self._screenshot(f"07_after_signin_{source}")
            logger.info("After sign-in — URL: %s", self._driver.current_url)

            # 5. Handle "Stay signed in?"
            self._handle_stay_signed_in()

        except Exception as e:
            self._screenshot(f"08_entra_error_{source}")
            logger.error(
                "Entra login error (%s): %s — URL: %s",
                source,
                e,
                self._driver.current_url,
            )

    def _handle_stay_signed_in(
        self, wait_timeout: float = 3, post_click_wait: float = 1
    ):
        """Handle the 'Stay signed in?' / 'Don't show this again' prompt."""
        try:
            selectors = [
                "#idSIButton9",
                'input[type="submit"][value="Yes"]',
            ]
            for sel in selectors:
                btn = self._find_clickable(sel, timeout=wait_timeout)
                if btn:
                    btn.click()
                    logger.info("Clicked 'Yes' on Stay signed in prompt")
                    time.sleep(post_click_wait)
                    self._screenshot("09_after_stay_signed_in")
                    return

            # Also try text-based
            btn = self._find_by_text("button", "Yes", timeout=wait_timeout)
            if btn:
                btn.click()
                logger.info("Clicked 'Yes' on Stay signed in prompt (via text)")
                time.sleep(post_click_wait)
                self._screenshot("09_after_stay_signed_in")
                return
        except Exception:
            logger.debug("No 'Stay signed in' prompt detected")

    def _wait_for_google_classroom(self):
        """Wait for Google Classroom main page to load after SSO."""
        logger.info("Waiting for Google Classroom to load...")
        try:
            WebDriverWait(self._driver, 45).until(
                EC.url_contains("classroom.google.com")
            )
            self._wait_for_page_load(timeout=30)
            time.sleep(3)
            self._screenshot("10_google_classroom_loaded")
            logger.info("Google Classroom loaded: %s", self._driver.current_url)
        except TimeoutException:
            self._screenshot("10_google_classroom_fail")
            logger.warning(
                "Google Classroom wait issue — final URL: %s",
                self._driver.current_url,
            )

    # ─── Brightspace Login ──────────────────────────────────────────────

    def login_brightspace(self) -> webdriver.Chrome:
        """
        Log into Brightspace via TDSB SSO.
        Flow: Brightspace landing → click "Staff And Students Login"
              → TDSB SSO (Entra) → login → redirect back to Brightspace.
        """
        logger.info("Navigating to Brightspace...")
        self._driver.get(self.BRIGHTSPACE_URL)
        self._wait_for_page_load()
        self._screenshot("20_brightspace_start")
        logger.info("Brightspace start page: %s", self._driver.current_url)

        # Click the "Staff And Students Login" button
        self._handle_brightspace_landing()

        # Handle Entra login (SSO may auto-complete from Google session)
        already_on_bs = (
            "elearningontario.ca" in self._driver.current_url
            and "/d2l/" in self._driver.current_url
        )
        if already_on_bs:
            logger.info("Already on Brightspace (SSO auto-completed) — skipping Entra login")
        else:
            self._handle_entra_login(source="brightspace")

        # Wait for Brightspace to load
        self._wait_for_brightspace()

        logger.info("Brightspace login complete — url: %s", self._driver.current_url)
        return self._driver

    def _handle_brightspace_landing(self):
        """Click 'Staff And Students Login' on the Brightspace landing page."""
        try:
            btn_texts = [
                "Staff And Students Login",
                "Staff and Students",
                "Staff",
            ]
            clicked = False
            for text in btn_texts:
                for tag in ["a", "button"]:
                    btn = self._find_by_text(tag, text, timeout=3)
                    if btn:
                        try:
                            if btn.is_displayed():
                                btn.click()
                                logger.info(
                                    "Clicked Brightspace login button: <%s> '%s'",
                                    tag,
                                    text,
                                )
                                clicked = True
                                break
                        except Exception:
                            continue
                if clicked:
                    break

            if not clicked:
                self._screenshot("20_no_staff_button")
                logger.warning(
                    "Could not find 'Staff And Students Login' — URL: %s",
                    self._driver.current_url,
                )
                return

            # Wait for redirect to Entra SSO
            logger.info("Waiting for Entra SSO redirect after clicking login...")
            try:
                WebDriverWait(self._driver, 20).until(
                    lambda d: (
                        any(
                            x in d.current_url
                            for x in ["microsoftonline", "login.microsoft", "login.live"]
                        )
                        or len(d.find_elements(By.CSS_SELECTOR, 'input[type="password"]')) > 0
                        or len(d.find_elements(By.CSS_SELECTOR, 'input[name="loginfmt"]')) > 0
                    )
                )
            except TimeoutException:
                logger.warning(
                    "Entra redirect wait timeout — URL: %s", self._driver.current_url
                )

            time.sleep(2)
            self._screenshot("20_after_staff_login_click")
            logger.info("After staff login click — URL: %s", self._driver.current_url)

        except Exception as e:
            self._screenshot("20_landing_error")
            logger.warning("Brightspace landing handling error: %s", e)

    def _wait_for_brightspace(self):
        """Wait for Brightspace homepage to load."""
        try:
            if "elearningontario.ca" not in self._driver.current_url:
                WebDriverWait(self._driver, 30).until(
                    EC.url_contains("elearningontario.ca")
                )
            # domcontentloaded is enough — 'load' hangs on Brightspace's
            # heavy async resource loading.
            self._wait_for_page_load(timeout=15)

            # Dismiss the "Your browser is looking a little retro" modal
            self._dismiss_brightspace_browser_warning()

            self._screenshot("21_brightspace_loaded")
            logger.info("Brightspace loaded: %s", self._driver.current_url)
        except TimeoutException:
            self._screenshot("21_brightspace_fail")
            logger.warning(
                "Brightspace wait issue — final URL: %s", self._driver.current_url
            )

    def _dismiss_brightspace_browser_warning(self):
        """Dismiss the 'Your browser is looking a little retro' dialog if present."""
        try:
            for text in ["Got It", "Got it"]:
                for tag in ["button", "a"]:
                    btn = self._find_by_text(tag, text, timeout=2)
                    if btn:
                        try:
                            if btn.is_displayed():
                                btn.click()
                                logger.info("Dismissed 'browser retro' warning dialog")
                                time.sleep(1)
                                return
                        except Exception:
                            continue
        except Exception:
            pass  # No dialog — that's fine

    # ─── Convenience ────────────────────────────────────────────────────

    @property
    def driver(self) -> webdriver.Chrome | None:
        return self._driver
