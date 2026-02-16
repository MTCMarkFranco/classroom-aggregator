"""
Debug script: step through Google Classroom login and dump HTML at each stage.
"""
import asyncio, os
from playwright.async_api import async_playwright

USERNAME = "343934782@tdsb.ca"
PASSWORD = "butter12"
OUT = os.path.join(os.path.dirname(__file__), "debug_html")
os.makedirs(OUT, exist_ok=True)

GOOGLE_LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin"
    "?continue=https://classroom.google.com/u/0/h"
    "&passive=true"
)


async def dump(page, label):
    """Save screenshot + HTML for a step."""
    html = await page.content()
    with open(os.path.join(OUT, f"{label}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    await page.screenshot(path=os.path.join(OUT, f"{label}.png"), full_page=True)
    print(f"[{label}] URL: {page.url}")
    print(f"  → saved {label}.html + {label}.png")


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = await ctx.new_page()

    # ── Step 1: Go to Google sign-in ──
    print("\n=== STEP 1: Navigate to Google sign-in ===")
    await page.goto(GOOGLE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await dump(page, "01_google_signin")

    # ── Step 2: Fill email ──
    print("\n=== STEP 2: Fill email ===")
    selectors = ['input[type="email"]', 'input#identifierId', 'input[name="identifier"]']
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.fill(USERNAME)
                print(f"  Filled with: {sel}")
                break
        except Exception:
            continue
    await dump(page, "02_email_filled")

    # ── Step 3: Click Next ──
    print("\n=== STEP 3: Click Next ===")
    for sel in ['#identifierNext', 'button:has-text("Next")', 'button[type="submit"]']:
        try:
            btn = page.locator(sel)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                print(f"  Clicked: {sel}")
                break
        except Exception:
            continue

    # ── Step 4: Wait for page to transition ──
    print("\n=== STEP 4: Wait for navigation after Next ===")
    # Give the page time — don't use wait_for_function, just let navigation happen
    for i in range(20):
        await page.wait_for_timeout(2000)
        try:
            url = page.url
        except Exception:
            url = "(page navigating)"
        print(f"  [{i*2}s] URL: {url}")
        try:
            await dump(page, f"04_wait_{i:02d}")
        except Exception as e:
            print(f"  (could not dump: {e})")
        # Stop waiting once we're on a stable page with login elements
        try:
            pw_field = page.locator('input[name="passwd"], input[type="password"]')
            if await pw_field.count() > 0 and await pw_field.first.is_visible():
                print("  → Password field visible!")
                break
        except Exception:
            pass
        # Or we already made it to classroom
        if "classroom.google.com" in str(url):
            print("  → Already at Classroom!")
            break

    # ── Step 5: Check where we are and look for login elements ──
    print("\n=== STEP 5: Current page analysis ===")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        await dump(page, "05_current_page")
    except Exception as e:
        print(f"  (dump failed: {e})")
    
    try:
        html = await page.content()
        print(f"  Page title: {await page.title()}")
        print(f"  URL: {page.url}")
        print(f"  HTML length: {len(html)}")
    except Exception as e:
        print(f"  Cannot read content: {e}")
    
    # Check for known elements
    checks = [
        ('input[name="loginfmt"]', "Entra username field"),
        ('input[name="passwd"]', "Entra password field"),
        ('input[type="password"]', "Any password field"),
        ('input[type="email"]', "Any email field"),
        ('input[type="submit"]', "Submit button"),
        ('#idSIButton9', "Entra Next/Yes button"),
    ]
    for sel, desc in checks:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            visible = False
            if count > 0:
                visible = await loc.first.is_visible()
            print(f"  {sel}: count={count}, visible={visible} ({desc})")
        except Exception as e:
            print(f"  {sel}: error — {e}")

    # ── Step 6: If we see Entra login, enter password ──
    print("\n=== STEP 6: Try to enter password if on Entra ===")
    pw_selectors = ['input[name="passwd"]', 'input[name="Password"]', 'input[type="password"]']
    for sel in pw_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.fill(PASSWORD)
                print(f"  Filled password with: {sel}")
                # Click sign in
                for btn_sel in ['input[type="submit"]', '#idSIButton9', 'button[type="submit"]']:
                    try:
                        btn = page.locator(btn_sel)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            print(f"  Clicked sign-in: {btn_sel}")
                            break
                    except Exception:
                        continue
                break
        except Exception:
            continue

    # ── Step 7: Wait and see what happens after sign-in ──
    print("\n=== STEP 7: Post sign-in wait ===")
    for i in range(15):
        await page.wait_for_timeout(2000)
        url = page.url
        print(f"  [{i*2}s] URL: {url}")
        await dump(page, f"07_post_signin_{i:02d}")
        if "classroom.google.com" in url:
            print("  → Reached Classroom!")
            break
        # Handle stay signed in prompt
        try:
            stay = page.locator('#idSIButton9, input[type="submit"][value="Yes"]')
            if await stay.count() > 0 and await stay.first.is_visible():
                text = await stay.first.get_attribute("value") or "button"
                print(f"  → Found stay-signed-in prompt ({text}), clicking...")
                await stay.first.click()
        except Exception:
            pass

    await dump(page, "08_final")
    print(f"\n=== DONE — Final URL: {page.url} ===")
    print(f"HTML dumps saved in: {OUT}")

    # Keep browser open for 10s so user can see
    await page.wait_for_timeout(10000)

    try:
        await ctx.close()
    except Exception:
        pass
    try:
        await browser.close()
    except Exception:
        pass
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
