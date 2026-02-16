"""
Data models for the Classroom Aggregator.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Platform(Enum):
    GOOGLE_CLASSROOM = "Google Classroom"
    BRIGHTSPACE = "Brightspace"


class AssignmentStatus(Enum):
    NOT_SUBMITTED = "Not Submitted"
    MISSING = "Missing"
    LATE = "Late"
    ASSIGNED = "Assigned"
    UPCOMING = "Upcoming"
    UNKNOWN = "Unknown"


class ItemType(Enum):
    ASSIGNMENT = "Assignment"
    ANNOUNCEMENT = "Announcement"
    MATERIAL = "Material"
    QUIZ = "Quiz"
    DISCUSSION = "Discussion"
    EVENT = "Event"


@dataclass
class ClassInfo:
    """Represents a class/course."""
    name: str
    platform: Platform
    url: str
    teacher: str = ""
    short_code: str = ""  # e.g. ENG, GLE, PPL, History


@dataclass
class Assignment:
    """Represents an assignment or work item."""
    title: str
    course_name: str
    platform: Platform
    item_type: ItemType = ItemType.ASSIGNMENT
    status: AssignmentStatus = AssignmentStatus.NOT_SUBMITTED
    due_date: Optional[datetime] = None
    due_date_str: str = ""
    description: str = ""
    url: str = ""
    points: str = ""
    posted_date: Optional[datetime] = None
    posted_date_str: str = ""

    @property
    def is_overdue(self) -> bool:
        if self.due_date and self.due_date < datetime.now():
            return True
        return False

    @property
    def display_due(self) -> str:
        if self.due_date:
            return self.due_date.strftime("%b %d, %Y %I:%M %p")
        return self.due_date_str or "No due date"

    @property
    def display_posted(self) -> str:
        if self.posted_date:
            return self.posted_date.strftime("%b %d, %Y")
        return self.posted_date_str or ""
