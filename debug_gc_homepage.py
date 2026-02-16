"""
Quick debug: log into GC, dump the homepage HTML, then exit.
Re-uses the working TDSBAuth from auth.py.
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(__file__))

from auth import TDSBAuth

async def main():
    auth = TDSBAuth("343934782@tdsb.ca", "butter12", debug=True)
    await auth.start_browser(headless=False)
    try:
        ctx = await auth.login_google_classroom()
        page = ctx.pages[0]
        # Give the Classroom page a moment to fully render
        await page.wait_for_timeout(5000)

        # Save the homepage HTML
        html = await page.content()
        out = os.path.join(os.path.dirname(__file__), "debug_html", "gc_homepage.html")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved {len(html)} chars → {out}")

        # Also dump text of every a[href*="/c/"] link for analysis
        links = await page.locator('a[href*="/c/"]').all()
        for i, link in enumerate(links):
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            bb = await link.bounding_box()
            print(f"  [{i}] href={href}  text={text!r}  box={bb}")

    finally:
        await auth.close()

asyncio.run(main())
