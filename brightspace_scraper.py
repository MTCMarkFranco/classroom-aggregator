"""
Brightspace (D2L) Scraper.

Scrapes class list, assignments, announcements, and upcoming events from
Brightspace / elearningontario.ca using Selenium browser automation.
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from dateutil import parser as dateparser
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

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

    def __init__(self, driver: WebDriver, semester_classes: list[str] | None = None):
        self.semester_classes = semester_classes or DEFAULT_SEMESTER_CLASSES
        self.driver = driver
        self.classes: list[ClassInfo] = []
        self.assignments: list[Assignment] = []

    # ─── Helpers ────────────────────────────────────────────────────────

    def _wait_for_load(self, timeout: float = 30):
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass

    def _find_by_text(self, tag: str, text: str, timeout: float = 5):
        """Find an element by tag whose subtree contains text."""
        try:
            if "'" not in text:
                xpath = f"//{tag}[contains(., '{text}')]"
            elif '"' not in text:
                xpath = f'//{tag}[contains(., "{text}")]'
            else:
                xpath = f"//{tag}[contains(., '{text}')]"
            return WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located((By.XPATH, xpath))
            )
        except TimeoutException:
            return None

    def _fetch_json_api(self, url_path: str):
        """Use Selenium to fetch a D2L JSON API endpoint via async script.

        Returns the parsed JSON (dict/list) or ``None`` on failure.
        """
        try:
            result = self.driver.execute_async_script(
                """
                const callback = arguments[arguments.length - 1];
                fetch(arguments[0], {credentials: 'include'})
                    .then(r => r.ok ? r.json() : null)
                    .then(data => callback(data))
                    .catch(() => callback(null));
                """,
                url_path,
            )
            return result
        except Exception as e:
            logger.debug("Fetch API %s: %s", url_path, e)
            return None

    def _dismiss_browser_warning(self):
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
            pass

    # ─── Main entry ─────────────────────────────────────────────────────

    def scrape_all(self) -> tuple[list[ClassInfo], list[Assignment]]:
        """Main entry: scrape classes then assignments for each."""
        # Navigate to Brightspace home
        self.driver.get(f"{self.BASE_URL}/d2l/home")
        self._wait_for_load()
        time.sleep(2)

        # Dismiss "Your browser is looking a little retro" dialog
        self._dismiss_browser_warning()

        # Get classes
        all_classes = self._scrape_class_list()
        logger.info("Found %d total classes on Brightspace", len(all_classes))

        # Filter to semester classes
        self.classes = [
            c for c in all_classes
            if _matches_semester_class(c.name, self.semester_classes)
        ]
        logger.info("Matched %d semester classes on Brightspace", len(self.classes))

        if not self.classes:
            logger.warning("No semester class matches found on Brightspace, using all classes")
            self.classes = all_classes

        # Scrape data for each class
        for cls in self.classes:
            try:
                cls_assignments = self._scrape_class_assignments(cls)
                self.assignments.extend(cls_assignments)

                announcements = self._scrape_class_announcements(cls)
                self.assignments.extend(announcements)

                logger.info(
                    "Found %d items for '%s'",
                    len(cls_assignments) + len(announcements), cls.name,
                )
            except Exception as e:
                logger.error("Error scraping Brightspace class '%s': %s", cls.name, e)

        # Also try the global "Work To Do" widget
        try:
            global_todo = self._scrape_work_to_do()
            existing = {a.title for a in self.assignments}
            for item in global_todo:
                if item.title not in existing:
                    self.assignments.append(item)
        except Exception as e:
            logger.debug("Global work-to-do scraping: %s", e)

        return self.classes, self.assignments

    # ─── Class list ─────────────────────────────────────────────────────

    def _scrape_class_list(self) -> list[ClassInfo]:
        """Scrape the list of enrolled courses from Brightspace homepage."""
        classes = []

        # Method 1: Try the course cards on the homepage
        try:
            course_links = self.driver.find_elements(
                By.CSS_SELECTOR,
                'a[href*="/d2l/home/"], '
                'a[href*="/d2l/le/content/"], '
                'd2l-card a, '
                '.d2l-card a, '
                '.course-card a, '
                'a.d2l-link[href*="/d2l/"]',
            )

            seen_urls = set()
            for link in course_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.text.strip()

                    if not text or not href:
                        continue

                    full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

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

        # Method 2: Try the enrollment API endpoint
        if not classes:
            classes = self._scrape_classes_via_api()

        # Method 3: Try the "My Courses" page
        if not classes:
            classes = self._scrape_my_courses_page()

        return classes

    def _scrape_classes_via_api(self) -> list[ClassInfo]:
        """Try to get courses from Brightspace's internal API."""
        classes = []
        try:
            response = self._fetch_json_api(
                "/d2l/api/lp/1.0/enrollments/myenrollments/?sortBy=-PinDate"
            )
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

    def _scrape_my_courses_page(self) -> list[ClassInfo]:
        """Navigate to the full course listing page."""
        classes = []
        try:
            self.driver.get(f"{self.BASE_URL}/d2l/le/manageCourses/search/6606")
            self._wait_for_load()
            time.sleep(2)

            links = self.driver.find_elements(
                By.CSS_SELECTOR, 'a[href*="/d2l/home/"]'
            )
            seen = set()
            for link in links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.text.strip()
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

    # ─── Per-class assignments ──────────────────────────────────────────

    def _scrape_class_assignments(self, cls: ClassInfo) -> list[Assignment]:
        """Scrape assignments for a specific Brightspace class."""
        assignments = []

        course_id = self._extract_course_id(cls.url)
        if not course_id:
            logger.warning("Could not extract course ID for '%s'", cls.name)
            return assignments

        # Method 1: Try D2L API for assignments
        api_assignments = self._fetch_assignments_api(course_id, cls)
        if api_assignments:
            return api_assignments

        # Method 2: Navigate to the assignments page
        try:
            self.driver.get(
                f"{self.BASE_URL}/d2l/lms/dropbox/user/folders_list.d2l?ou={course_id}"
            )
            self._wait_for_load()
            time.sleep(2)

            rows = self.driver.find_elements(
                By.CSS_SELECTOR,
                'table tr, .d2l-datalist-item, '
                'div[class*="assignment"], '
                'a[href*="dropbox"]',
            )

            for row in rows:
                try:
                    text = row.text.strip()
                    if not text or len(text) < 3:
                        continue

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0]

                    if title.lower() in ("name", "assignment", "due date", "status"):
                        continue

                    # Try to find due date
                    due_date = None
                    due_date_str = ""
                    for line in lines:
                        try:
                            if re.search(
                                r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b',
                                line,
                                re.I,
                            ):
                                due_date = dateparser.parse(line, fuzzy=True)
                                due_date_str = line
                                break
                        except Exception:
                            continue

                    # Determine status
                    status = AssignmentStatus.NOT_SUBMITTED
                    text_lower = text.lower()
                    if "submitted" in text_lower or "completed" in text_lower:
                        continue
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
            quiz_assignments = self._scrape_quizzes(course_id, cls)
            assignments.extend(quiz_assignments)
        except Exception:
            pass

        return assignments

    def _fetch_assignments_api(
        self, course_id: str, cls: ClassInfo
    ) -> list[Assignment]:
        """Try fetching assignments via D2L's internal API."""
        assignments = []
        try:
            # Try completions first
            response = self._fetch_json_api(
                f"/d2l/api/le/1.0/{course_id}/content/completions/myProgress/"
            )

            # If that didn't work, try dropbox (assignments) API
            if not response:
                dropbox_data = self._fetch_json_api(
                    f"/d2l/api/le/1.0/{course_id}/dropbox/folders/"
                )
                if dropbox_data:
                    response = {"type": "dropbox", "data": dropbox_data}

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

    def _scrape_quizzes(
        self, course_id: str, cls: ClassInfo
    ) -> list[Assignment]:
        """Scrape quizzes for a course."""
        quizzes = []
        try:
            self.driver.get(
                f"{self.BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={course_id}"
            )
            self._wait_for_load()
            time.sleep(1.5)

            rows = self.driver.find_elements(
                By.CSS_SELECTOR, "table tr, .d2l-datalist-item"
            )
            for row in rows:
                try:
                    text = row.text.strip()
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

    # ─── Announcements ──────────────────────────────────────────────────

    def _scrape_class_announcements(self, cls: ClassInfo) -> list[Assignment]:
        """Scrape announcements for a Brightspace class."""
        announcements: list[Assignment] = []
        course_id = self._extract_course_id(cls.url)
        if not course_id:
            return announcements

        # Try the D2L news API first
        try:
            api_result = self._fetch_json_api(
                f"/d2l/api/le/1.0/{course_id}/news/"
            )
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

        # Fallback: HTML scraping with strict filtering
        _UI_JUNK = {
            "show search options", "hide search options", "search in",
            "headlinecontent", "posted in", "search", "filter",
            "sort", "show", "hide", "actions", "select all",
            "news", "announcements", "no items to display",
        }

        try:
            self.driver.get(
                f"{self.BASE_URL}/d2l/lms/news/main.d2l?ou={course_id}"
            )
            self._wait_for_load()
            time.sleep(1.5)

            # Target only datalist items (actual announcements)
            items = self.driver.find_elements(
                By.CSS_SELECTOR, ".d2l-datalist-item"
            )
            if not items:
                items = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    'div[class*="news-item"], div[class*="d2l-msg-container"]',
                )

            for item in items[:10]:
                try:
                    text = item.text.strip()
                    if not text or len(text) < 8:
                        continue

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0]

                    if title.lower().strip() in _UI_JUNK:
                        continue
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

    # ─── Work To Do / Upcoming ──────────────────────────────────────────

    def _scrape_work_to_do(self) -> list[Assignment]:
        """Scrape the global 'Work To Do' widget on the Brightspace homepage."""
        items = []
        try:
            self.driver.get(f"{self.BASE_URL}/d2l/home")
            self._wait_for_load()
            time.sleep(2)

            # Look for "Work To Do" widget via XPath (has-text equivalent)
            widgets = self.driver.find_elements(
                By.XPATH,
                '//div[contains(., "Work To Do")] | '
                '//d2l-widget[contains(., "Work To Do")] | '
                '//section[contains(., "Work To Do")]',
            )

            if widgets:
                widget_el = widgets[0]
                text = widget_el.text.strip()
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

            # Also try calendar/upcoming events
            upcoming = self._scrape_upcoming_events()
            items.extend(upcoming)

        except Exception as e:
            logger.debug("Work To Do scraping: %s", e)
        return items

    def _scrape_upcoming_events(self) -> list[Assignment]:
        """Scrape upcoming events from the Brightspace calendar widget."""
        events = []
        try:
            calendar_widgets = self.driver.find_elements(
                By.XPATH,
                '//div[contains(., "Upcoming Events")] | '
                '//d2l-widget[contains(., "Calendar")] | '
                '//div[contains(@class, "calendar")]',
            )
            if calendar_widgets:
                text = calendar_widgets[0].text.strip()
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

    # ─── Utilities ──────────────────────────────────────────────────────

    def _extract_course_id(self, url: str) -> str:
        """Extract the course/org unit ID from a Brightspace URL."""
        match = re.search(r'/d2l/home/(\d+)', url)
        if match:
            return match.group(1)
        match = re.search(r'ou=(\d+)', url)
        if match:
            return match.group(1)
        match = re.search(r'/(\d+)/?$', url)
        if match:
            return match.group(1)
        return ""
