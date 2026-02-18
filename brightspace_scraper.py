"""
Brightspace (D2L) Scraper.

Scrapes class list, assignments, announcements, and upcoming events from
Brightspace / elearningontario.ca using Playwright browser automation.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional
from dateutil import parser as dateparser
from playwright.async_api import BrowserContext, Page

from models import (
    ClassInfo, Assignment, Platform, AssignmentStatus, ItemType
)

logger = logging.getLogger(__name__)

# Default semester classes (overridden by .env / constructor arg)
DEFAULT_SEMESTER_CLASSES = ["ENG", "GLE", "PPL", "History"]


def _matches_semester_class(class_name: str, semester_classes: list[str] | None = None) -> bool:
    classes = semester_classes or DEFAULT_SEMESTER_CLASSES
    name_upper = class_name.upper()
    for code in classes:
        if code.upper() in name_upper:
            return True
    return False


def _get_short_code(class_name: str, semester_classes: list[str] | None = None) -> str:
    classes = semester_classes or DEFAULT_SEMESTER_CLASSES
    name_upper = class_name.upper()
    for code in classes:
        if code.upper() in name_upper:
            return code.upper()
    return class_name[:10]


class BrightspaceScraper:
    """Scrapes Brightspace D2L for classes and assignments."""

    BASE_URL = "https://tdsb.elearningontario.ca"

    def __init__(self, context: BrowserContext, semester_classes: list[str] | None = None):
        self.semester_classes = semester_classes or DEFAULT_SEMESTER_CLASSES
        self.context = context
        self.classes: list[ClassInfo] = []
        self.assignments: list[Assignment] = []

    async def _dismiss_browser_warning(self, page):
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
            pass

    async def _scroll_to_load_all(self, page: Page, max_scrolls: int = 10):
        """Scroll the page to the bottom to trigger lazy-loaded content."""
        for _ in range(max_scrolls):
            previous_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == previous_height:
                break
        # Scroll back to top
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)

    async def scrape_all(self) -> tuple[list[ClassInfo], list[Assignment]]:
        """Main entry: scrape classes then assignments for each."""
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Navigate to Brightspace home
        await page.goto(f"{self.BASE_URL}/d2l/home", wait_until="load", timeout=30000)
        await page.wait_for_timeout(2000)

        # Dismiss "Your browser is looking a little retro" dialog if present
        await self._dismiss_browser_warning(page)

        # Scroll down to load any lazy-loaded course tiles
        await self._scroll_to_load_all(page)

        # Get classes
        all_classes = await self._scrape_class_list(page)
        logger.info("Found %d total classes on Brightspace", len(all_classes))

        # Filter to semester classes
        self.classes = [c for c in all_classes if _matches_semester_class(c.name, self.semester_classes)]
        logger.info("Matched %d semester classes on Brightspace", len(self.classes))

        if not self.classes:
            logger.warning("No semester class matches found on Brightspace, using all classes")
            self.classes = all_classes

        # Scrape data for each class
        for cls in self.classes:
            try:
                # Assignments
                cls_assignments = await self._scrape_class_assignments(page, cls)
                self.assignments.extend(cls_assignments)

                # Announcements
                announcements = await self._scrape_class_announcements(page, cls)
                self.assignments.extend(announcements)

                logger.info(
                    "Found %d items for '%s'",
                    len(cls_assignments) + len(announcements), cls.name
                )
            except Exception as e:
                logger.error("Error scraping Brightspace class '%s': %s", cls.name, e)

        # Also try the global "Work To Do" widget
        try:
            global_todo = await self._scrape_work_to_do(page)
            # Add items that aren't duplicates
            existing = {a.title for a in self.assignments}
            for item in global_todo:
                if item.title not in existing:
                    self.assignments.append(item)
        except Exception as e:
            logger.debug("Global work-to-do scraping: %s", e)

        return self.classes, self.assignments

    async def _scrape_class_list(self, page: Page) -> list[ClassInfo]:
        """Scrape the list of enrolled courses from Brightspace homepage."""
        classes = []

        # Brightspace D2L shows courses in various ways:
        # 1. Course cards/tiles on the homepage widget
        # 2. "My Courses" dropdown/panel
        # 3. Full course listing page

        # Method 1: Try the course cards on the homepage
        try:
            # D2L course cards typically have links to /d2l/home/<courseId>
            course_links = await page.locator(
                'a[href*="/d2l/home/"], '
                'a[href*="/d2l/le/content/"], '
                'd2l-card a, '
                '.d2l-card a, '
                '.course-card a, '
                'a.d2l-link[href*="/d2l/"]'
            ).all()

            seen_urls = set()
            for link in course_links:
                try:
                    href = await link.get_attribute("href") or ""
                    text = (await link.inner_text()).strip()

                    if not text or not href:
                        continue

                    full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    # Filter out non-course links
                    if "/d2l/home/" not in href and "/d2l/le/" not in href:
                        continue

                    classes.append(ClassInfo(
                        name=text.split("\n")[0].strip(),
                        platform=Platform.BRIGHTSPACE,
                        url=full_url,
                        short_code=_get_short_code(text, self.semester_classes),
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Course cards scraping: %s", e)

        # Method 2: Try the enrollment API endpoint (D2L has REST-like internal APIs)
        if not classes:
            classes = await self._scrape_classes_via_api(page)

        # Method 3: Try the "My Courses" page
        if not classes:
            classes = await self._scrape_my_courses_page(page)

        return classes

    async def _scrape_classes_via_api(self, page: Page) -> list[ClassInfo]:
        """Try to get courses from Brightspace's internal API."""
        classes = []
        try:
            # D2L has internal API endpoints for enrollment
            response = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('/d2l/api/lp/1.0/enrollments/myenrollments/?sortBy=-PinDate', {
                            credentials: 'include'
                        });
                        if (resp.ok) {
                            return await resp.json();
                        }
                    } catch(e) {}
                    return null;
                }
            """)

            if response and "Items" in response:
                for item in response["Items"]:
                    org_unit = item.get("OrgUnit", {})
                    name = org_unit.get("Name", "")
                    org_id = org_unit.get("Id", "")
                    if name and org_id:
                        classes.append(ClassInfo(
                            name=name,
                            platform=Platform.BRIGHTSPACE,
                            url=f"{self.BASE_URL}/d2l/home/{org_id}",
                            short_code=_get_short_code(name, self.semester_classes),
                        ))
        except Exception as e:
            logger.debug("D2L API enrollment fetch: %s", e)
        return classes

    async def _scrape_my_courses_page(self, page: Page) -> list[ClassInfo]:
        """Navigate to the full course listing page."""
        classes = []
        try:
            await page.goto(
                f"{self.BASE_URL}/d2l/le/manageCourses/search/6606",
                wait_until="load", timeout=30000
            )
            await page.wait_for_timeout(2000)

            links = await page.locator('a[href*="/d2l/home/"]').all()
            seen = set()
            for link in links:
                try:
                    href = await link.get_attribute("href") or ""
                    text = (await link.inner_text()).strip()
                    full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                    if text and full_url not in seen:
                        seen.add(full_url)
                        classes.append(ClassInfo(
                            name=text.split("\n")[0].strip(),
                            platform=Platform.BRIGHTSPACE,
                            url=full_url,
                            short_code=_get_short_code(text, self.semester_classes),
                        ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("My Courses page scraping: %s", e)
        return classes

    async def _scrape_class_assignments(
        self, page: Page, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape assignments for a specific Brightspace class."""
        assignments = []

        # Extract course ID from URL
        course_id = self._extract_course_id(cls.url)
        if not course_id:
            logger.warning("Could not extract course ID for '%s'", cls.name)
            return assignments

        # Method 1: Try D2L API for assignments
        api_assignments = await self._fetch_assignments_api(page, course_id, cls)
        if api_assignments:
            return api_assignments

        # Method 2: Navigate to the assignments page
        try:
            await page.goto(
                f"{self.BASE_URL}/d2l/lms/dropbox/user/folders_list.d2l?ou={course_id}",
                wait_until="load", timeout=30000
            )
            await page.wait_for_timeout(2000)

            # Parse assignment list
            rows = await page.locator(
                'table tr, .d2l-datalist-item, '
                'div[class*="assignment"], '
                'a[href*="dropbox"]'
            ).all()

            for row in rows:
                try:
                    text = (await row.inner_text()).strip()
                    if not text or len(text) < 3:
                        continue

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0]

                    # Skip headers and system rows
                    if title.lower() in ("name", "assignment", "due date", "status"):
                        continue

                    # Try to find due date
                    due_date = None
                    due_date_str = ""
                    for line in lines:
                        try:
                            if re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', line, re.I):
                                due_date = dateparser.parse(line, fuzzy=True)
                                due_date_str = line
                                break
                        except Exception:
                            continue

                    # Determine status
                    status = AssignmentStatus.NOT_SUBMITTED
                    text_lower = text.lower()
                    if "submitted" in text_lower or "completed" in text_lower:
                        continue  # Skip completed
                    if "overdue" in text_lower or "past due" in text_lower:
                        status = AssignmentStatus.MISSING

                    assignments.append(Assignment(
                        title=title,
                        course_name=cls.name,
                        platform=Platform.BRIGHTSPACE,
                        item_type=ItemType.ASSIGNMENT,
                        status=status,
                        due_date=due_date,
                        due_date_str=due_date_str,
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Assignment page scraping for '%s': %s", cls.name, e)

        # Method 3: Try quizzes page
        try:
            quiz_assignments = await self._scrape_quizzes(page, course_id, cls)
            assignments.extend(quiz_assignments)
        except Exception:
            pass

        return assignments

    async def _fetch_assignments_api(
        self, page: Page, course_id: str, cls: ClassInfo
    ) -> list[Assignment]:
        """Try fetching assignments via D2L's internal API."""
        assignments = []
        try:
            response = await page.evaluate(f"""
                async () => {{
                    try {{
                        const resp = await fetch('/d2l/api/le/1.0/{course_id}/content/completions/myProgress/', {{
                            credentials: 'include'
                        }});
                        if (resp.ok) return await resp.json();
                    }} catch(e) {{}}

                    // Try dropbox (assignments) API
                    try {{
                        const resp = await fetch('/d2l/api/le/1.0/{course_id}/dropbox/folders/', {{
                            credentials: 'include'
                        }});
                        if (resp.ok) return {{ type: 'dropbox', data: await resp.json() }};
                    }} catch(e) {{}}

                    return null;
                }}
            """)

            if response:
                if isinstance(response, dict) and response.get("type") == "dropbox":
                    for folder in response.get("data", []):
                        name = folder.get("Name", "")
                        due = folder.get("DueDate", "")
                        if name:
                            due_date = None
                            if due:
                                try:
                                    due_date = dateparser.parse(due)
                                except Exception:
                                    pass

                            assignments.append(Assignment(
                                title=name,
                                course_name=cls.name,
                                platform=Platform.BRIGHTSPACE,
                                item_type=ItemType.ASSIGNMENT,
                                status=AssignmentStatus.NOT_SUBMITTED,
                                due_date=due_date,
                                due_date_str=due or "",
                            ))
        except Exception as e:
            logger.debug("D2L API assignment fetch for '%s': %s", cls.name, e)

        return assignments

    async def _scrape_quizzes(
        self, page: Page, course_id: str, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape quizzes for a course."""
        quizzes = []
        try:
            await page.goto(
                f"{self.BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={course_id}",
                wait_until="load", timeout=20000
            )
            await page.wait_for_timeout(1500)

            rows = await page.locator('table tr, .d2l-datalist-item').all()
            for row in rows:
                try:
                    text = (await row.inner_text()).strip()
                    if not text or len(text) < 3:
                        continue
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0]
                    if title.lower() in ("name", "quiz", "date", "status"):
                        continue

                    text_lower = text.lower()
                    if "completed" in text_lower or "submitted" in text_lower:
                        continue

                    quizzes.append(Assignment(
                        title=title,
                        course_name=cls.name,
                        platform=Platform.BRIGHTSPACE,
                        item_type=ItemType.QUIZ,
                        status=AssignmentStatus.NOT_SUBMITTED,
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Quiz scraping for '%s': %s", cls.name, e)
        return quizzes

    async def _scrape_class_announcements(
        self, page: Page, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape announcements for a Brightspace class.

        The D2L news page (``/d2l/lms/news/main.d2l``) contains a search
        form and a list of announcements.  We first try the API, then fall
        back to HTML parsing with a blocklist to avoid scraping form
        labels like "Show Search Options".
        """
        announcements: list[Assignment] = []
        course_id = self._extract_course_id(cls.url)
        if not course_id:
            return announcements

        # ── Try the D2L news API first ──
        try:
            api_result = await page.evaluate(f"""
                async () => {{
                    try {{
                        const resp = await fetch(
                            '/d2l/api/le/1.0/{course_id}/news/',
                            {{ credentials: 'include' }}
                        );
                        if (resp.ok) return await resp.json();
                    }} catch(e) {{}}
                    return null;
                }}
            """)
            if api_result and isinstance(api_result, list):
                for item in api_result[:10]:
                    title = item.get("Title", "").strip()
                    body = (item.get("Body", {}).get("Text", "") or "").strip()
                    if title:
                        announcements.append(Assignment(
                            title=title,
                            course_name=cls.name,
                            platform=Platform.BRIGHTSPACE,
                            item_type=ItemType.ANNOUNCEMENT,
                            status=AssignmentStatus.ASSIGNED,
                            description=body[:200] if body else "",
                        ))
                if announcements:
                    return announcements
        except Exception as e:
            logger.debug("D2L news API for '%s': %s", cls.name, e)

        # ── Fallback: HTML scraping with strict filtering ──
        # Blocklist of D2L form/UI labels that are NOT announcements
        _UI_JUNK = {
            "show search options", "hide search options", "search in",
            "headlinecontent", "posted in", "search", "filter",
            "sort", "show", "hide", "actions", "select all",
            "news", "announcements", "no items to display",
        }

        try:
            await page.goto(
                f"{self.BASE_URL}/d2l/lms/news/main.d2l?ou={course_id}",
                wait_until="load", timeout=20000,
            )
            await page.wait_for_timeout(1500)

            # Target only datalist items (actual announcements)
            items = await page.locator(".d2l-datalist-item").all()
            if not items:
                # Broader fallback — but we'll filter aggressively
                items = await page.locator(
                    'div[class*="news-item"], '
                    'div[class*="d2l-msg-container"]'
                ).all()

            for item in items[:10]:
                try:
                    text = (await item.inner_text()).strip()
                    if not text or len(text) < 8:
                        continue

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0]

                    # Skip if the title is a known UI element
                    if title.lower().strip() in _UI_JUNK:
                        continue
                    # Skip very short titles (likely labels)
                    if len(title) < 5:
                        continue

                    posted_date_str = ""
                    for line in lines:
                        if re.search(
                            r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b',
                            line, re.I,
                        ):
                            posted_date_str = line
                            break

                    announcements.append(Assignment(
                        title=title,
                        course_name=cls.name,
                        platform=Platform.BRIGHTSPACE,
                        item_type=ItemType.ANNOUNCEMENT,
                        status=AssignmentStatus.ASSIGNED,
                        posted_date_str=posted_date_str,
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Announcements scraping for '%s': %s", cls.name, e)

        return announcements

    async def _scrape_work_to_do(self, page: Page) -> list[Assignment]:
        """Scrape the global 'Work To Do' widget on the Brightspace homepage."""
        items = []
        try:
            await page.goto(f"{self.BASE_URL}/d2l/home", wait_until="load", timeout=20000)
            await page.wait_for_timeout(2000)

            # Look for "Work To Do" widget
            widget = page.locator(
                'div:has-text("Work To Do"), '
                'd2l-widget:has-text("Work To Do"), '
                'section:has-text("Work To Do")'
            )

            if await widget.count() > 0:
                widget_el = widget.first
                text = (await widget_el.inner_text()).strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                for line in lines:
                    if line.lower() in ("work to do", "upcoming", ""):
                        continue
                    items.append(Assignment(
                        title=line,
                        course_name="",
                        platform=Platform.BRIGHTSPACE,
                        item_type=ItemType.ASSIGNMENT,
                        status=AssignmentStatus.NOT_SUBMITTED,
                    ))

            # Also try the calendar/upcoming events
            upcoming = await self._scrape_upcoming_events(page)
            items.extend(upcoming)

        except Exception as e:
            logger.debug("Work To Do scraping: %s", e)
        return items

    async def _scrape_upcoming_events(self, page: Page) -> list[Assignment]:
        """Scrape upcoming events from the Brightspace calendar widget."""
        events = []
        try:
            # Look for calendar or upcoming events widget
            calendar_widget = page.locator(
                'div:has-text("Upcoming Events"), '
                'd2l-widget:has-text("Calendar"), '
                'div[class*="calendar"]'
            )

            if await calendar_widget.count() > 0:
                text = (await calendar_widget.first.inner_text()).strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                for line in lines:
                    if line.lower() in ("upcoming events", "calendar", ""):
                        continue
                    events.append(Assignment(
                        title=line,
                        course_name="",
                        platform=Platform.BRIGHTSPACE,
                        item_type=ItemType.EVENT,
                        status=AssignmentStatus.UPCOMING,
                    ))
        except Exception as e:
            logger.debug("Upcoming events scraping: %s", e)
        return events

    def _extract_course_id(self, url: str) -> str:
        """Extract the course/org unit ID from a Brightspace URL."""
        # URLs are like /d2l/home/12345 or ?ou=12345
        match = re.search(r'/d2l/home/(\d+)', url)
        if match:
            return match.group(1)
        match = re.search(r'ou=(\d+)', url)
        if match:
            return match.group(1)
        # Try just the last numeric segment
        match = re.search(r'/(\d+)/?$', url)
        if match:
            return match.group(1)
        return ""
