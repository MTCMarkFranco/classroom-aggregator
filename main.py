"""
Classroom Assignment Aggregator - Main Application

Aggregates incomplete assignments from Google Classroom and Brightspace
for TDSB students. Uses browser automation for SSO login since
API/app registration is not available.

Usage:
    python main.py [--headless] [--debug]
"""

import asyncio
import argparse
import getpass
import logging
import os
import sys
from datetime import datetime
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule
from rich import box

from models import (
    ClassInfo, Assignment, Platform, AssignmentStatus, ItemType
)
from auth import TDSBAuth
from google_classroom_scraper import GoogleClassroomScraper
from brightspace_scraper import BrightspaceScraper

console = Console()

# Semester classes to look for
SEMESTER_CLASSES = {
    "ENG": "English",
    "GLE": "GLE",
    "PPL": "PPL (Gym)",
    "HISTORY": "History",
}


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy loggers unless in debug mode
    if not debug:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_credentials() -> tuple[str, str]:
    """Prompt user for TDSB credentials."""
    console.print()
    console.print(
        Panel(
            "[bold cyan]TDSB Classroom Assignment Aggregator[/bold cyan]\n\n"
            "This tool scrapes Google Classroom and Brightspace for\n"
            "incomplete assignments using browser automation.\n\n"
            "[dim]Your credentials are used only for login and are not stored.[/dim]",
            title="Welcome",
            border_style="cyan",
        )
    )
    console.print()

    username = console.input("[bold yellow]TDSB Username[/bold yellow] (e.g. 123456789@tdsb.ca): ")
    password = getpass.getpass("TDSB Password: ")
    return username.strip(), password.strip()


def display_classes(
    gc_classes: list[ClassInfo],
    bs_classes: list[ClassInfo],
):
    """Display discovered classes."""
    console.print()
    console.print(Rule("[bold cyan]Discovered Classes[/bold cyan]"))
    console.print()

    # Google Classroom classes
    if gc_classes:
        table = Table(
            title="Google Classroom",
            box=box.ROUNDED,
            title_style="bold green",
            show_lines=True,
        )
        table.add_column("Class", style="bold white")
        table.add_column("Code", style="cyan")
        for cls in gc_classes:
            table.add_row(cls.name, cls.short_code)
        console.print(table)
    else:
        console.print("[yellow]No classes found on Google Classroom[/yellow]")

    console.print()

    # Brightspace classes
    if bs_classes:
        table = Table(
            title="Brightspace",
            box=box.ROUNDED,
            title_style="bold blue",
            show_lines=True,
        )
        table.add_column("Class", style="bold white")
        table.add_column("Code", style="cyan")
        for cls in bs_classes:
            table.add_row(cls.name, cls.short_code)
        console.print(table)
    else:
        console.print("[yellow]No classes found on Brightspace[/yellow]")


def display_assignments(all_assignments: list[Assignment]):
    """Display all incomplete assignments in a formatted table."""
    console.print()
    console.print(Rule("[bold red]Incomplete Assignments & Work To Do[/bold red]"))
    console.print()

    if not all_assignments:
        console.print(
            Panel(
                "[bold green]No incomplete assignments found! All caught up! 🎉[/bold green]",
                border_style="green",
            )
        )
        return

    # Group by course
    by_course: dict[str, list[Assignment]] = defaultdict(list)
    for a in all_assignments:
        key = a.course_name or "General / Unknown"
        by_course[key].append(a)

    # Sort courses
    for course_name in sorted(by_course.keys()):
        assignments = by_course[course_name]

        # Sort within course: overdue first, then by due date
        assignments.sort(key=lambda a: (
            0 if a.status == AssignmentStatus.MISSING else
            1 if a.is_overdue else
            2 if a.due_date else 3,
            a.due_date or datetime.max,
        ))

        # Create table for this course
        table = Table(
            title=f"📚 {course_name}",
            box=box.ROUNDED,
            title_style="bold white",
            show_lines=True,
            padding=(0, 1),
        )
        table.add_column("#", style="dim", width=3)
        table.add_column("Type", style="cyan", width=12)
        table.add_column("Title", style="bold white", max_width=50)
        table.add_column("Status", width=15)
        table.add_column("Due Date", width=22)
        table.add_column("Platform", style="dim", width=18)

        for i, a in enumerate(assignments, 1):
            # Status styling
            if a.status == AssignmentStatus.MISSING:
                status_text = Text("⚠ MISSING", style="bold red")
            elif a.is_overdue:
                status_text = Text("⏰ OVERDUE", style="bold red")
            elif a.status == AssignmentStatus.LATE:
                status_text = Text("⚠ LATE", style="bold yellow")
            elif a.status == AssignmentStatus.NOT_SUBMITTED:
                status_text = Text("📝 Not Done", style="yellow")
            elif a.status == AssignmentStatus.UPCOMING:
                status_text = Text("📅 Upcoming", style="blue")
            else:
                status_text = Text("📋 Assigned", style="white")

            # Due date styling
            due_text = a.display_due
            if a.is_overdue:
                due_text = Text(due_text, style="bold red")
            elif "No due" in due_text:
                due_text = Text(due_text, style="dim")

            # Type icon
            type_map = {
                ItemType.ASSIGNMENT: "📝 Assignment",
                ItemType.QUIZ: "❓ Quiz",
                ItemType.ANNOUNCEMENT: "📢 Announce",
                ItemType.MATERIAL: "📖 Material",
                ItemType.DISCUSSION: "💬 Discuss",
                ItemType.EVENT: "📅 Event",
            }
            type_text = type_map.get(a.item_type, str(a.item_type.value))

            # Platform
            platform_text = "🟢 Google" if a.platform == Platform.GOOGLE_CLASSROOM else "🔵 Brightspace"

            table.add_row(
                str(i),
                type_text,
                a.title[:50],
                status_text,
                due_text if isinstance(due_text, Text) else str(due_text),
                platform_text,
            )

        console.print(table)
        console.print()


def display_summary(all_assignments: list[Assignment]):
    """Display a summary of all assignments."""
    console.print(Rule("[bold cyan]Summary[/bold cyan]"))
    console.print()

    total = len(all_assignments)
    missing = sum(1 for a in all_assignments if a.status == AssignmentStatus.MISSING)
    overdue = sum(1 for a in all_assignments if a.is_overdue and a.status != AssignmentStatus.MISSING)
    upcoming = sum(1 for a in all_assignments if a.status == AssignmentStatus.UPCOMING)
    not_submitted = sum(1 for a in all_assignments if a.status == AssignmentStatus.NOT_SUBMITTED)
    announcements = sum(1 for a in all_assignments if a.item_type == ItemType.ANNOUNCEMENT)

    gc_count = sum(1 for a in all_assignments if a.platform == Platform.GOOGLE_CLASSROOM)
    bs_count = sum(1 for a in all_assignments if a.platform == Platform.BRIGHTSPACE)

    summary_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary_table.add_column("Label", style="bold")
    summary_table.add_column("Count", justify="right")

    summary_table.add_row("Total items found", str(total))
    if missing:
        summary_table.add_row(Text("⚠ Missing", style="bold red"), Text(str(missing), style="bold red"))
    if overdue:
        summary_table.add_row(Text("⏰ Overdue", style="bold red"), Text(str(overdue), style="bold red"))
    summary_table.add_row("📝 Not submitted", str(not_submitted))
    summary_table.add_row("📅 Upcoming", str(upcoming))
    summary_table.add_row("📢 Announcements", str(announcements))
    summary_table.add_row("", "")
    summary_table.add_row("🟢 Google Classroom", str(gc_count))
    summary_table.add_row("🔵 Brightspace", str(bs_count))

    console.print(Panel(summary_table, title="Overview", border_style="cyan"))
    console.print()

    # Urgency message
    if missing or overdue:
        console.print(
            Panel(
                f"[bold red]⚠ ATTENTION: {missing + overdue} item(s) are missing or overdue![/bold red]\n"
                "[yellow]Please check these immediately.[/yellow]",
                border_style="red",
            )
        )
    elif not_submitted:
        console.print(
            Panel(
                f"[yellow]📝 There are {not_submitted} item(s) that still need to be completed.[/yellow]",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                "[bold green]✅ All caught up! No urgent items.[/bold green]",
                border_style="green",
            )
        )

    console.print()
    console.print(
        f"[dim]Report generated at {datetime.now().strftime('%B %d, %Y %I:%M %p')}[/dim]"
    )
    console.print()


async def run(
    username: str,
    password: str,
    headless: bool = False,
    debug: bool = False,
    semester_classes: list[str] | None = None,
):
    """Main async workflow."""
    auth = TDSBAuth(username, password, debug=debug)

    gc_classes: list[ClassInfo] = []
    bs_classes: list[ClassInfo] = []
    all_assignments: list[Assignment] = []

    try:
        # Start browser
        with console.status("[bold cyan]Launching browser...[/bold cyan]"):
            await auth.start_browser(headless=headless)

        # ── Google Classroom ────────────────────────────────────────
        console.print()
        console.print("[bold green]▶ Logging into Google Classroom...[/bold green]")
        try:
            with console.status("[bold green]Authenticating with TDSB SSO for Google Classroom...[/bold green]"):
                gc_context = await auth.login_google_classroom()

            console.print("[green]✓ Google Classroom login successful[/green]")

            with console.status("[bold green]Scraping Google Classroom...[/bold green]"):
                gc_scraper = GoogleClassroomScraper(gc_context, semester_classes=semester_classes)
                gc_classes, gc_assignments = await gc_scraper.scrape_all()

                # Also try the global to-do
                try:
                    global_todo = await gc_scraper.scrape_global_todo()
                    existing_titles = {a.title for a in gc_assignments}
                    for item in global_todo:
                        if item.title not in existing_titles:
                            gc_assignments.append(item)
                except Exception:
                    pass

            all_assignments.extend(gc_assignments)
            console.print(
                f"[green]✓ Found {len(gc_classes)} classes, "
                f"{len(gc_assignments)} items on Google Classroom[/green]"
            )

        except Exception as e:
            console.print(f"[red]✗ Google Classroom error: {e}[/red]")
            if debug:
                console.print_exception()

        # ── Brightspace ─────────────────────────────────────────────
        console.print()
        console.print("[bold blue]▶ Logging into Brightspace...[/bold blue]")
        try:
            with console.status("[bold blue]Authenticating with TDSB SSO for Brightspace...[/bold blue]"):
                bs_context = await auth.login_brightspace()

            console.print("[blue]✓ Brightspace login successful[/blue]")

            with console.status("[bold blue]Scraping Brightspace...[/bold blue]"):
                bs_scraper = BrightspaceScraper(bs_context, semester_classes=semester_classes)
                bs_classes, bs_assignments = await bs_scraper.scrape_all()

            all_assignments.extend(bs_assignments)
            console.print(
                f"[blue]✓ Found {len(bs_classes)} classes, "
                f"{len(bs_assignments)} items on Brightspace[/blue]"
            )

        except Exception as e:
            console.print(f"[red]✗ Brightspace error: {e}[/red]")
            if debug:
                console.print_exception()

        # ── Display Results ─────────────────────────────────────────
        display_classes(gc_classes, bs_classes)
        display_assignments(all_assignments)
        display_summary(all_assignments)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Fatal error: {e}[/bold red]")
        if debug:
            console.print_exception()
    finally:
        with console.status("[dim]Closing browser...[/dim]"):
            await auth.close()
        console.print("[dim]Done.[/dim]")


def main():
    parser = argparse.ArgumentParser(
        description="TDSB Classroom Assignment Aggregator"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no visible window)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=None,
        help="TDSB username (will prompt if not provided)",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=None,
        help="TDSB password (will prompt if not provided)",
    )
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # Get credentials: CLI args > .env > interactive prompt
    username = args.username or os.getenv("TDSB_USERNAME") or ""
    password = args.password or os.getenv("TDSB_PASSWORD") or ""

    if not username or not password:
        if username:
            password = getpass.getpass("TDSB Password: ")
        else:
            username, password = get_credentials()

    if not username or not password:
        console.print("[red]Username and password are required.[/red]")
        sys.exit(1)

    # Semester classes from .env (comma-separated) or default
    semester_env = os.getenv("SEMESTER_CLASSES", "")
    semester_classes = (
        [s.strip() for s in semester_env.split(",") if s.strip()]
        if semester_env
        else ["ENG", "GLE", "PPL", "History"]
    )

    # Headless mode: CLI flag overrides .env
    headless = args.headless or os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")

    # Run the aggregator
    asyncio.run(run(
        username, password,
        headless=headless, debug=args.debug,
        semester_classes=semester_classes,
    ))


if __name__ == "__main__":
    main()
