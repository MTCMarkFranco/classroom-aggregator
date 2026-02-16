"""
Debug: log into GC, dump classwork + to-do HTML, then exit.
Captures:
  - gc_class_page.html  (first class's main page)
  - gc_classwork.html   (classwork tab)
  - gc_todo.html        (global to-do /a/not-turned-in/all)
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(__file__))
from auth import TDSBAuth

OUTDIR = os.path.join(os.path.dirname(__file__), "debug_html")

async def dump(page, name):
    html = await page.content()
    path = os.path.join(OUTDIR, f"{name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [{name}] {len(html)} chars  URL: {page.url}")
    return html

async def main():
    os.makedirs(OUTDIR, exist_ok=True)
    auth = TDSBAuth("343934782@tdsb.ca", "butter12", debug=True)
    await auth.start_browser(headless=False)
    try:
        ctx = await auth.login_google_classroom()
        page = ctx.pages[0]
        await page.wait_for_timeout(3000)

        # 1. Homepage (already loaded)
        await dump(page, "gc_homepage")

        # 2. First class page
        links = await page.locator('a[href*="/c/"]').all()
        first_class_url = None
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if "/c/" in href and text and len(text) > 5:
                if href.startswith("/"):
                    href = f"https://classroom.google.com{href}"
                first_class_url = href
                print(f"\n  Navigating to class: {text[:60]}  ({href})")
                break

        if first_class_url:
            await page.goto(first_class_url, wait_until="load", timeout=30000)
            await page.wait_for_timeout(3000)
            await dump(page, "gc_class_page")

            # Try clicking Classwork tab
            cw_tab = page.locator('a:has-text("Classwork"), a[aria-label*="Classwork"]')
            if await cw_tab.count() > 0:
                await cw_tab.first.click()
                await page.wait_for_timeout(3000)
                await dump(page, "gc_classwork")

                # Dump all assignment links
                items = await page.locator('a[href*="/a/"], a[href*="/sa/"]').all()
                print(f"\n  Classwork assignment links ({len(items)}):")
                for i, it in enumerate(items):
                    href = await it.get_attribute("href") or ""
                    text = (await it.inner_text()).strip()
                    print(f"    [{i}] href={href}  text={text!r}")

        # 3. Global to-do page
        print("\n  Navigating to global to-do...")
        await page.goto(
            "https://classroom.google.com/u/0/a/not-turned-in/all",
            wait_until="load", timeout=30000,
        )
        await page.wait_for_timeout(3000)
        await dump(page, "gc_todo")

        # Dump body inner text (first 2000 chars)
        body_text = await page.inner_text("body")
        print(f"\n  To-do body text (first 2000 chars):\n---")
        print(body_text[:2000])
        print("---")

        # Dump all a[href*="/a/"] links on the to-do page
        todo_links = await page.locator('a[href*="/a/"]').all()
        print(f"\n  To-do assignment links ({len(todo_links)}):")
        for i, link in enumerate(todo_links):
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            bb = await link.bounding_box()
            print(f"    [{i}] href={href}  text={text!r}  box={bb}")

    finally:
        await auth.close()

asyncio.run(main())
