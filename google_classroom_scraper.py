"""
Google Classroom Scraper.

Scrapes class list and assignments from Google Classroom using Playwright.
Google Classroom is a heavily JS-rendered SPA, so we use browser automation
to interact with it.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Optional
from dateutil import parser as dateparser
from playwright.async_api import BrowserContext, Page

from models import (
    ClassInfo, Assignment, Platform, AssignmentStatus, ItemType
)

logger = logging.getLogger(__name__)

# Directory to save debug screenshots
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "debug_screenshots")

# Default semester classes (overridden by .env / constructor arg)
DEFAULT_SEMESTER_CLASSES = ["ENG", "GLE", "PPL", "History"]


def _matches_semester_class(class_name: str, semester_classes: list[str] | None = None) -> bool:
    """Check if a class name matches one of the semester courses."""
    classes = semester_classes or DEFAULT_SEMESTER_CLASSES
    name_upper = class_name.upper()
    for code in classes:
        if code.upper() in name_upper:
            return True
    return False


def _get_short_code(class_name: str, semester_classes: list[str] | None = None) -> str:
    """Extract a short code from the class name."""
    classes = semester_classes or DEFAULT_SEMESTER_CLASSES
    name_upper = class_name.upper()
    for code in classes:
        if code.upper() in name_upper:
            return code.upper()
    return class_name[:10]


class GoogleClassroomScraper:
    """Scrapes Google Classroom for classes and assignments."""

    BASE_URL = "https://classroom.google.com"

    def __init__(self, context: BrowserContext, semester_classes: list[str] | None = None):
        self.semester_classes = semester_classes or DEFAULT_SEMESTER_CLASSES
        self.context = context
        self.classes: list[ClassInfo] = []
        self.assignments: list[Assignment] = []

    async def scrape_all(self) -> tuple[list[ClassInfo], list[Assignment]]:
        """Main entry: scrape classes then assignments for each matching class."""
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Navigate to class list
        await page.goto(f"{self.BASE_URL}/h", wait_until="domcontentloaded", timeout=30000)

        # Wait for at least one class card link to be rendered by JS
        try:
            await page.wait_for_selector('a[href*="/c/"]', state="visible", timeout=30000)
            logger.info("Class card links detected in DOM")
        except Exception:
            logger.warning("Timed out waiting for class card links — proceeding anyway")

        # Let remaining cards finish rendering
        await page.wait_for_timeout(5000)

        # Always save a screenshot of the class list page for diagnostics
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        try:
            ss_path = os.path.join(SCREENSHOT_DIR, "gc_class_list.png")
            await page.screenshot(path=ss_path, full_page=True)
            print(f"  [debug] Screenshot saved: {ss_path}")
        except Exception as e:
            print(f"  [debug] Screenshot failed: {e}")

        # Get all classes
        all_classes = await self._scrape_class_list(page)
        print(f"  [debug] Found {len(all_classes)} total classes on Google Classroom:")
        for c in all_classes:
            print(f"    - '{c.name}'  (url={c.url})")

        # Filter to semester classes
        self.classes = [c for c in all_classes if _matches_semester_class(c.name, self.semester_classes)]
        print(f"  [debug] Matched {len(self.classes)} semester classes (filter={self.semester_classes}):")
        for c in self.classes:
            print(f"    - '{c.name}'")

        # If no filtered matches, use all classes (user can filter later)
        if not self.classes:
            logger.warning("No semester class matches found, using all classes")
            self.classes = all_classes

        # Scrape assignments for each class
        for cls in self.classes:
            try:
                class_assignments = await self._scrape_class_assignments(page, cls)
                self.assignments.extend(class_assignments)
                logger.info(
                    "Found %d items for '%s'",
                    len(class_assignments), cls.name
                )
            except Exception as e:
                logger.error("Error scraping class '%s': %s", cls.name, e)

        return self.classes, self.assignments

    async def _scroll_to_load_all(self, page: Page, max_scrolls: int = 10):
        """Scroll the page to the bottom to trigger lazy-loaded content."""
        for _ in range(max_scrolls):
            previous_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == previous_height:
                break
        # Scroll back to top
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)

    async def _scrape_class_list(self, page: Page) -> list[ClassInfo]:
        """Scrape the list of classes from the Google Classroom homepage.

        Google Classroom renders many ``<a href="/c/…">`` links per class
        (sidebar, card header, card image, etc.).  The sidebar links look like::

            "G\\nGLE - Learning Strategies D\\n109/209/309/409"

        where the first line is a single-letter icon and the *second* line is
        the real class name.  Card-header links typically omit the icon letter.

        We group all links by their course ID (from the URL) and pick the
        best text for each unique course.
        """
        # ── Collect every /c/ link ──
        links = await page.locator('a[href*="/c/"]').all()

        # Map courseId → list of (full_url, raw_text)
        course_texts: dict[str, list[tuple[str, str]]] = {}
        for link in links:
            try:
                href = await link.get_attribute("href") or ""
                # Try inner_text first, fall back to textContent
                text = (await link.inner_text()).strip()
                if not text:
                    text = (await link.text_content() or "").strip()
                if not text or "/c/" not in href:
                    continue
                # Normalise URL
                if href.startswith("/"):
                    href = f"{self.BASE_URL}{href}"
                # Extract course id segment (/c/<id> or /c/<id>/…)
                match = re.search(r"/c/([^/]+)", href)
                if not match:
                    continue
                cid = match.group(1)
                course_texts.setdefault(cid, []).append((href, text))
            except Exception as e:
                logger.debug("Error reading class link: %s", e)

        # Debug: dump all raw course texts
        for cid, entries in course_texts.items():
            for url, txt in entries:
                print(f"    [raw] cid={cid}  text={txt!r:.80}")

        # ── Pick the best (longest) text per course and extract name ──
        classes = []
        for cid, entries in course_texts.items():
            # Sort by text length descending — longer text has more info
            entries.sort(key=lambda e: len(e[1]), reverse=True)
            best_url, best_text = entries[0]
            # Strip the URL down to the class page (no /sp/… suffix)
            class_url = re.sub(r"/sp/.+$", "", best_url)

            # Parse name: skip any single-char first line (icon letter)
            lines = [ln.strip() for ln in best_text.split("\n") if ln.strip()]
            if len(lines) >= 2 and len(lines[0]) <= 2:
                name = lines[1]  # second line is the real class name
                section = lines[2] if len(lines) > 2 else ""
            else:
                name = lines[0] if lines else cid
                section = lines[1] if len(lines) > 1 else ""

            classes.append(ClassInfo(
                name=name,
                platform=Platform.GOOGLE_CLASSROOM,
                url=class_url,
                teacher=section,  # section code in "teacher" field for display
                short_code=_get_short_code(name, self.semester_classes),
            ))

        # Fallback: try to get classes from the page content
        if not classes:
            classes = await self._scrape_class_list_fallback(page)

        return classes

    async def _scrape_class_list_fallback(self, page: Page) -> list[ClassInfo]:
        """Fallback class list scraping using broader selectors."""
        classes = []
        try:
            # Try getting all major clickable elements that look like courses
            # Google Classroom uses various selectors across versions
            all_links = await page.locator('a[data-courseid], a[href*="classroom.google.com/c/"]').all()
            for link in all_links:
                try:
                    href = await link.get_attribute("href") or ""
                    text = (await link.inner_text()).strip()
                    if text:
                        classes.append(ClassInfo(
                            name=text.split("\n")[0].strip(),
                            platform=Platform.GOOGLE_CLASSROOM,
                            url=href if href.startswith("http") else f"{self.BASE_URL}{href}",
                            short_code=_get_short_code(text, self.semester_classes),
                        ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Fallback class scraping failed: %s", e)

        # Ultimate fallback: parse page HTML
        if not classes:
            try:
                content = await page.content()
                # Look for course links in HTML
                course_pattern = r'href="(/c/[^"]+)"[^>]*>([^<]+)'
                matches = re.findall(course_pattern, content)
                for href, name in matches:
                    name = name.strip()
                    if name:
                        classes.append(ClassInfo(
                            name=name,
                            platform=Platform.GOOGLE_CLASSROOM,
                            url=f"{self.BASE_URL}{href}",
                            short_code=_get_short_code(name, self.semester_classes),
                        ))
            except Exception as e:
                logger.debug("HTML fallback failed: %s", e)

        return classes

    async def _scrape_class_assignments(
        self, page: Page, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape assignments for a specific class."""
        assignments: list[Assignment] = []

        # Navigate to the class page
        await page.goto(cls.url, wait_until="load", timeout=30000)
        await page.wait_for_timeout(2000)

        # The Classwork tab already uses data-stream-item-id containers
        # which capture assignments, quizzes, etc.
        classwork_assignments = await self._scrape_classwork_tab(page, cls)
        if classwork_assignments:
            assignments.extend(classwork_assignments)

        return assignments

    async def _scrape_classwork_tab(
        self, page: Page, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape the Classwork tab for assignments.

        Google Classroom's classwork page wraps each item in a
        ``<div data-stream-item-id="…">`` container.  Inside:

        * ``<a aria-label='Assignment: "Planning Log"' …>`` — type + title
        * ``<div class="JvYRu …">`` — "Teacher posted … : Title"
        * ``<div class="MXd8B …">`` — "Due Feb 12" (may be absent)
        """
        assignments: list[Assignment] = []

        # Click on "Classwork" tab
        try:
            classwork_tab = page.locator(
                'a:has-text("Classwork"), '
                'a[aria-label*="Classwork"], '
                'a[href*="/cw/"], '
                'a[data-tab-id="5"]'
            )
            if await classwork_tab.count() > 0:
                await classwork_tab.first.click()
                await page.wait_for_load_state("load", timeout=15000)
                await page.wait_for_timeout(2000)
            else:
                class_id = cls.url.rstrip("/").split("/")[-1]
                await page.goto(
                    f"{self.BASE_URL}/c/{class_id}/a/not-turned-in/all",
                    wait_until="load", timeout=30000,
                )
                await page.wait_for_timeout(2000)
        except Exception as e:
            logger.debug("Could not navigate to Classwork for '%s': %s", cls.name, e)
            return assignments

        # ── Parse items using data-stream-item-id containers ──
        # Use [data-stream-item-type] to avoid matching nested child divs
        # that also carry data-stream-item-id but not data-stream-item-type.
        try:
            containers = await page.locator(
                "div[data-stream-item-id][data-stream-item-type]"
            ).all()
            for container in containers:
                try:
                    assignment = await self._parse_stream_item(container, cls)
                    if assignment:
                        assignments.append(assignment)
                except Exception as e:
                    logger.debug("Error parsing stream item: %s", e)
        except Exception as e:
            logger.debug("Error querying stream items: %s", e)

        # Fallback: parse raw HTML
        if not assignments:
            assignments = await self._parse_classwork_html(page, cls)

        return assignments

    async def _parse_stream_item(
        self, container, cls: ClassInfo
    ) -> Optional[Assignment]:
        """Parse a ``div[data-stream-item-id]`` element from the Classwork tab."""
        # 1. Extract title + type from <a aria-label="…">
        title = ""
        item_type = ItemType.ASSIGNMENT
        link = container.locator("a[aria-label]")
        href = ""
        if await link.count() > 0:
            aria = (await link.first.get_attribute("aria-label")) or ""
            href = (await link.first.get_attribute("href")) or ""
            # aria-label looks like: 'Assignment: "Planning Log"'
            m = re.match(r'(Assignment|Quiz|Material|Question):\s*"(.+)"', aria)
            if m:
                kind, title = m.group(1), m.group(2)
                kind_lower = kind.lower()
                if "quiz" in kind_lower or "question" in kind_lower:
                    item_type = ItemType.QUIZ
                elif "material" in kind_lower:
                    item_type = ItemType.MATERIAL

        # 2. Fallback: description line "Teacher posted … : Title"
        if not title:
            desc_loc = container.locator(".JvYRu, .qoXqmb")
            if await desc_loc.count() > 0:
                desc_text = (await desc_loc.first.inner_text()).strip()
                # "Rachel Kaufman posted a new assignment: Planning Log"
                colon_idx = desc_text.rfind(":")
                if colon_idx != -1:
                    title = desc_text[colon_idx + 1:].strip()
                else:
                    title = desc_text

        if not title:
            return None  # skip items with no extractable title

        # 3. Due date from ".MXd8B" div
        due_date = None
        due_date_str = ""
        due_loc = container.locator(".MXd8B")
        if await due_loc.count() > 0:
            raw = (await due_loc.first.inner_text()).strip()
            due_date_str = raw
            m = re.search(r"(?:due|Due)\s+(.+)", raw, re.IGNORECASE)
            if m:
                try:
                    due_date = dateparser.parse(m.group(1), fuzzy=True)
                except Exception:
                    pass

        # 4. Determine status from surrounding text
        full_text = (await container.inner_text()).strip().lower()
        status = AssignmentStatus.ASSIGNED
        if "missing" in full_text:
            status = AssignmentStatus.MISSING
        elif "turned in" in full_text or "done" in full_text:
            return None  # skip completed items
        elif "late" in full_text:
            status = AssignmentStatus.LATE

        url = href if href.startswith("http") else (
            f"{self.BASE_URL}{href}" if href else ""
        )

        return Assignment(
            title=title,
            course_name=cls.name,
            platform=Platform.GOOGLE_CLASSROOM,
            item_type=item_type,
            status=status,
            due_date=due_date,
            due_date_str=due_date_str,
            url=url,
        )

    async def _parse_classwork_html(
        self, page: Page, cls: ClassInfo
    ) -> list[Assignment]:
        """Parse classwork from raw HTML as a fallback."""
        assignments = []
        try:
            content = await page.content()
            # Look for assignment-like elements
            # Google Classroom embeds assignment data in specific patterns
            assignment_pattern = r'href="(/c/[^/]+/(?:a|sa)/[^"]+)"[^>]*>([^<]+)'
            matches = re.findall(assignment_pattern, content)

            for href, title in matches:
                title = title.strip()
                if title and len(title) > 2:
                    assignments.append(Assignment(
                        title=title,
                        course_name=cls.name,
                        platform=Platform.GOOGLE_CLASSROOM,
                        status=AssignmentStatus.ASSIGNED,
                        url=f"{self.BASE_URL}{href}",
                    ))
        except Exception as e:
            logger.debug("HTML classwork parsing failed: %s", e)

        return assignments

    # _scrape_stream and _scrape_todo_page removed — classwork tab covers all items

    async def scrape_global_todo(self) -> list[Assignment]:
        """Scrape the global To-do page for all incomplete assignments.

        The page shows ``data-stream-item-id`` containers just like the
        classwork tab **unless** the student has nothing outstanding, in
        which case the body contains 'Nothing on your to-do list'.
        """
        items: list[Assignment] = []
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        try:
            await page.goto(
                f"{self.BASE_URL}/u/0/a/not-turned-in/all",
                wait_until="load", timeout=30000,
            )
            await page.wait_for_timeout(3000)

            body_text = await page.inner_text("body")
            if "nothing on your to-do list" in body_text.lower():
                logger.info("Global to-do page is empty — no outstanding work.")
                return items

            logger.info("Global to-do page loaded, parsing items...")

            # Prefer structured containers
            containers = await page.locator("div[data-stream-item-id]").all()
            if containers:
                for container in containers:
                    try:
                        # Reuse the same parser; create a pseudo ClassInfo
                        # with course name extracted from the item text.
                        a = await self._parse_global_todo_item(container)
                        if a:
                            items.append(a)
                    except Exception as e:
                        logger.debug("Error parsing global stream item: %s", e)
            else:
                # Fallback: look for detail links only (not nav links)
                detail_links = await page.locator(
                    'a[href*="/details"]'
                ).all()
                for link in detail_links:
                    try:
                        aria = (await link.get_attribute("aria-label")) or ""
                        href = (await link.get_attribute("href")) or ""
                        m = re.match(
                            r'(Assignment|Quiz|Material|Question):\s*"(.+)"',
                            aria,
                        )
                        if m:
                            title = m.group(2)
                            url = (
                                href if href.startswith("http")
                                else f"{self.BASE_URL}{href}"
                            )
                            items.append(Assignment(
                                title=title,
                                course_name="Unknown Class",
                                platform=Platform.GOOGLE_CLASSROOM,
                                status=AssignmentStatus.NOT_SUBMITTED,
                                url=url,
                            ))
                    except Exception:
                        continue

        except Exception as e:
            logger.error("Global to-do scraping error: %s", e)

        return items

    async def _parse_global_todo_item(self, container) -> Optional[Assignment]:
        """Parse a single item on the global to-do page."""
        title = ""
        item_type = ItemType.ASSIGNMENT
        href = ""

        link = container.locator("a[aria-label]")
        if await link.count() > 0:
            aria = (await link.first.get_attribute("aria-label")) or ""
            href = (await link.first.get_attribute("href")) or ""
            m = re.match(r'(Assignment|Quiz|Material|Question):\s*"(.+)"', aria)
            if m:
                kind, title = m.group(1), m.group(2)
                if "quiz" in kind.lower() or "question" in kind.lower():
                    item_type = ItemType.QUIZ
                elif "material" in kind.lower():
                    item_type = ItemType.MATERIAL

        if not title:
            desc_loc = container.locator(".JvYRu, .qoXqmb")
            if await desc_loc.count() > 0:
                desc_text = (await desc_loc.first.inner_text()).strip()
                colon_idx = desc_text.rfind(":")
                title = desc_text[colon_idx + 1:].strip() if colon_idx != -1 else desc_text

        if not title:
            return None

        # Course name — sometimes in a secondary line
        course_name = "Unknown Class"
        full_text = (await container.inner_text()).strip()
        for code in self.semester_classes:
            if code.upper() in full_text.upper():
                course_name = code
                break

        # Due date
        due_date = None
        due_date_str = ""
        due_loc = container.locator(".MXd8B")
        if await due_loc.count() > 0:
            raw = (await due_loc.first.inner_text()).strip()
            due_date_str = raw
            m = re.search(r"(?:due|Due)\s+(.+)", raw, re.IGNORECASE)
            if m:
                try:
                    due_date = dateparser.parse(m.group(1), fuzzy=True)
                except Exception:
                    pass

        url = href if href.startswith("http") else (
            f"{self.BASE_URL}{href}" if href else ""
        )

        return Assignment(
            title=title,
            course_name=course_name,
            platform=Platform.GOOGLE_CLASSROOM,
            item_type=item_type,
            status=AssignmentStatus.NOT_SUBMITTED,
            due_date=due_date,
            due_date_str=due_date_str,
            url=url,
        )
