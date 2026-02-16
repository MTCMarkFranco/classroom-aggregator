"""
Google Classroom Scraper.

Scrapes class list and assignments from Google Classroom using Selenium.
Google Classroom is a heavily JS-rendered SPA, so we use browser automation
to interact with it.
"""

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

    # ─── Main entry ─────────────────────────────────────────────────────

    def scrape_all(self) -> tuple[list[ClassInfo], list[Assignment]]:
        """Main entry: scrape classes then assignments for each matching class."""
        # Navigate to class list
        self.driver.get(f"{self.BASE_URL}/h")
        self._wait_for_load()
        time.sleep(3)

        # Get all classes
        all_classes = self._scrape_class_list()
        logger.info("Found %d total classes on Google Classroom", len(all_classes))

        # Filter to semester classes
        self.classes = [
            c for c in all_classes
            if _matches_semester_class(c.name, self.semester_classes)
        ]
        logger.info("Matched %d semester classes on Google Classroom", len(self.classes))

        # If no filtered matches, use all classes
        if not self.classes:
            logger.warning("No semester class matches found, using all classes")
            self.classes = all_classes

        # Scrape assignments for each class
        for cls in self.classes:
            try:
                class_assignments = self._scrape_class_assignments(cls)
                self.assignments.extend(class_assignments)
                logger.info(
                    "Found %d items for '%s'",
                    len(class_assignments), cls.name,
                )
            except Exception as e:
                logger.error("Error scraping class '%s': %s", cls.name, e)

        return self.classes, self.assignments

    # ─── Class list ─────────────────────────────────────────────────────

    def _scrape_class_list(self) -> list[ClassInfo]:
        """Scrape the list of classes from the Google Classroom homepage.

        Google Classroom renders many ``<a href="/c/…">`` links per class
        (sidebar, card header, card image, etc.).  The sidebar links look like::

            "G\\nGLE - Learning Strategies D\\n109/209/309/409"

        We group all links by their course ID (from the URL) and pick the
        best text for each unique course.
        """
        links = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/c/"]')

        # Map courseId → list of (full_url, raw_text)
        course_texts: dict[str, list[tuple[str, str]]] = {}
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                text = link.text.strip()
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

        # Pick the best (longest) text per course and extract name
        classes = []
        for cid, entries in course_texts.items():
            entries.sort(key=lambda e: len(e[1]), reverse=True)
            best_url, best_text = entries[0]
            class_url = re.sub(r"/sp/.+$", "", best_url)

            # Parse name: skip any single-char first line (icon letter)
            lines = [ln.strip() for ln in best_text.split("\n") if ln.strip()]
            if len(lines) >= 2 and len(lines[0]) <= 2:
                name = lines[1]
                section = lines[2] if len(lines) > 2 else ""
            else:
                name = lines[0] if lines else cid
                section = lines[1] if len(lines) > 1 else ""

            classes.append(ClassInfo(
                name=name,
                platform=Platform.GOOGLE_CLASSROOM,
                url=class_url,
                teacher=section,
                short_code=_get_short_code(name, self.semester_classes),
            ))

        # Fallback
        if not classes:
            classes = self._scrape_class_list_fallback()

        return classes

    def _scrape_class_list_fallback(self) -> list[ClassInfo]:
        """Fallback class list scraping using broader selectors."""
        classes = []
        try:
            all_links = self.driver.find_elements(
                By.CSS_SELECTOR,
                'a[data-courseid], a[href*="classroom.google.com/c/"]',
            )
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.text.strip()
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
                content = self.driver.page_source
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

    # ─── Per-class assignments ──────────────────────────────────────────

    def _scrape_class_assignments(self, cls: ClassInfo) -> list[Assignment]:
        """Scrape assignments for a specific class."""
        assignments: list[Assignment] = []

        self.driver.get(cls.url)
        self._wait_for_load()
        time.sleep(2)

        classwork_assignments = self._scrape_classwork_tab(cls)
        if classwork_assignments:
            assignments.extend(classwork_assignments)

        return assignments

    def _scrape_classwork_tab(self, cls: ClassInfo) -> list[Assignment]:
        """Scrape the Classwork tab for assignments."""
        assignments: list[Assignment] = []

        # Click on "Classwork" tab
        try:
            tab = None
            # Try CSS selectors first
            for sel in [
                'a[aria-label*="Classwork"]',
                'a[href*="/cw/"]',
                'a[data-tab-id="5"]',
            ]:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    tab = elements[0]
                    break

            # Try text-based match
            if not tab:
                elements = self.driver.find_elements(
                    By.XPATH, '//a[contains(., "Classwork")]'
                )
                if elements:
                    tab = elements[0]

            if tab:
                tab.click()
                self._wait_for_load(timeout=15)
                time.sleep(2)
            else:
                class_id = cls.url.rstrip("/").split("/")[-1]
                self.driver.get(
                    f"{self.BASE_URL}/c/{class_id}/a/not-turned-in/all"
                )
                self._wait_for_load()
                time.sleep(2)
        except Exception as e:
            logger.debug("Could not navigate to Classwork for '%s': %s", cls.name, e)
            return assignments

        # Parse items using data-stream-item-id containers
        try:
            containers = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div[data-stream-item-id][data-stream-item-type]",
            )
            for container in containers:
                try:
                    assignment = self._parse_stream_item(container, cls)
                    if assignment:
                        assignments.append(assignment)
                except Exception as e:
                    logger.debug("Error parsing stream item: %s", e)
        except Exception as e:
            logger.debug("Error querying stream items: %s", e)

        # Fallback: parse raw HTML
        if not assignments:
            assignments = self._parse_classwork_html(cls)

        return assignments

    def _parse_stream_item(
        self, container: WebElement, cls: ClassInfo
    ) -> Optional[Assignment]:
        """Parse a ``div[data-stream-item-id]`` element from the Classwork tab."""
        title = ""
        item_type = ItemType.ASSIGNMENT
        href = ""

        links = container.find_elements(By.CSS_SELECTOR, "a[aria-label]")
        if links:
            aria = links[0].get_attribute("aria-label") or ""
            href = links[0].get_attribute("href") or ""
            m = re.match(r'(Assignment|Quiz|Material|Question):\s*"(.+)"', aria)
            if m:
                kind, title = m.group(1), m.group(2)
                kind_lower = kind.lower()
                if "quiz" in kind_lower or "question" in kind_lower:
                    item_type = ItemType.QUIZ
                elif "material" in kind_lower:
                    item_type = ItemType.MATERIAL

        # Fallback: description line "Teacher posted … : Title"
        if not title:
            desc_elements = container.find_elements(
                By.CSS_SELECTOR, ".JvYRu, .qoXqmb"
            )
            if desc_elements:
                desc_text = desc_elements[0].text.strip()
                colon_idx = desc_text.rfind(":")
                if colon_idx != -1:
                    title = desc_text[colon_idx + 1:].strip()
                else:
                    title = desc_text

        if not title:
            return None

        # Due date from ".MXd8B" div
        due_date = None
        due_date_str = ""
        due_elements = container.find_elements(By.CSS_SELECTOR, ".MXd8B")
        if due_elements:
            raw = due_elements[0].text.strip()
            due_date_str = raw
            m = re.search(r"(?:due|Due)\s+(.+)", raw, re.IGNORECASE)
            if m:
                try:
                    due_date = dateparser.parse(m.group(1), fuzzy=True)
                except Exception:
                    pass

        # Determine status from surrounding text
        full_text = container.text.strip().lower()
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

    def _parse_classwork_html(self, cls: ClassInfo) -> list[Assignment]:
        """Parse classwork from raw HTML as a fallback."""
        assignments = []
        try:
            content = self.driver.page_source
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

    # ─── Global To-do ───────────────────────────────────────────────────

    def scrape_global_todo(self) -> list[Assignment]:
        """Scrape the global To-do page for all incomplete assignments."""
        items: list[Assignment] = []

        try:
            self.driver.get(f"{self.BASE_URL}/u/0/a/not-turned-in/all")
            self._wait_for_load()
            time.sleep(3)

            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            if "nothing on your to-do list" in body_text.lower():
                logger.info("Global to-do page is empty — no outstanding work.")
                return items

            logger.info("Global to-do page loaded, parsing items...")

            # Prefer structured containers
            containers = self.driver.find_elements(
                By.CSS_SELECTOR, "div[data-stream-item-id]"
            )
            if containers:
                for container in containers:
                    try:
                        a = self._parse_global_todo_item(container)
                        if a:
                            items.append(a)
                    except Exception as e:
                        logger.debug("Error parsing global stream item: %s", e)
            else:
                # Fallback: look for detail links only
                detail_links = self.driver.find_elements(
                    By.CSS_SELECTOR, 'a[href*="/details"]'
                )
                for link in detail_links:
                    try:
                        aria = link.get_attribute("aria-label") or ""
                        href_val = link.get_attribute("href") or ""
                        m = re.match(
                            r'(Assignment|Quiz|Material|Question):\s*"(.+)"',
                            aria,
                        )
                        if m:
                            title = m.group(2)
                            url = (
                                href_val if href_val.startswith("http")
                                else f"{self.BASE_URL}{href_val}"
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

    def _parse_global_todo_item(
        self, container: WebElement
    ) -> Optional[Assignment]:
        """Parse a single item on the global to-do page."""
        title = ""
        item_type = ItemType.ASSIGNMENT
        href = ""

        links = container.find_elements(By.CSS_SELECTOR, "a[aria-label]")
        if links:
            aria = links[0].get_attribute("aria-label") or ""
            href = links[0].get_attribute("href") or ""
            m = re.match(r'(Assignment|Quiz|Material|Question):\s*"(.+)"', aria)
            if m:
                kind, title = m.group(1), m.group(2)
                if "quiz" in kind.lower() or "question" in kind.lower():
                    item_type = ItemType.QUIZ
                elif "material" in kind.lower():
                    item_type = ItemType.MATERIAL

        if not title:
            desc_elements = container.find_elements(
                By.CSS_SELECTOR, ".JvYRu, .qoXqmb"
            )
            if desc_elements:
                desc_text = desc_elements[0].text.strip()
                colon_idx = desc_text.rfind(":")
                title = desc_text[colon_idx + 1:].strip() if colon_idx != -1 else desc_text

        if not title:
            return None

        # Course name — sometimes in a secondary line
        course_name = "Unknown Class"
        full_text = container.text.strip()
        for code in self.semester_classes:
            if code.upper() in full_text.upper():
                course_name = code
                break

        # Due date
        due_date = None
        due_date_str = ""
        due_elements = container.find_elements(By.CSS_SELECTOR, ".MXd8B")
        if due_elements:
            raw = due_elements[0].text.strip()
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
