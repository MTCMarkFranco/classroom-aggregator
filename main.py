#!/usr/bin/env python3
"""
Classroom Aggregator – Main Entry Point

Aggregates incomplete assignments from Google Classroom and D2L Brightspace
for a TDSB student account, displaying a clean Rich-formatted summary.
"""

import argparse
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from auth import TDSBAuth
from models import Platform, AssignmentStatus, ItemType, Assignment
from google_classroom_scraper import GoogleClassroomScraper
from brightspace_scraper import BrightspaceScraper

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────

console = Console()

STATUS_COLORS = {
    AssignmentStatus.MISSING: "bold red",
    AssignmentStatus.LATE: "red",
    AssignmentStatus.NOT_SUBMITTED: "yellow",
    AssignmentStatus.ASSIGNED: "cyan",
    AssignmentStatus.UPCOMING: "blue",
    AssignmentStatus.UNKNOWN: "dim",
}

PLATFORM_COLORS = {
    Platform.GOOGLE_CLASSROOM: "green",
    Platform.BRIGHTSPACE: "blue",
}

TYPE_ICONS = {
    ItemType.ASSIGNMENT: "[bold]A[/bold]",
    ItemType.QUIZ: "[bold magenta]Q[/bold magenta]",
    ItemType.ANNOUNCEMENT: "[dim]N[/dim]",
    ItemType.MATERIAL: "[dim]M[/dim]",
    ItemType.DISCUSSION: "[bold cyan]D[/bold cyan]",
    ItemType.EVENT: "[bold blue]E[/bold blue]",
}


# ─── Display helpers ────────────────────────────────────────────────────

def display_classes(classes, platform_name: str):
    """Show a table of discovered classes for *platform_name*."""
    if not classes:
        console.print(f"  [dim]No classes found on {platform_name}.[/dim]")
        return

    table = Table(
        title=f"{platform_name} Classes",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Class", min_width=20)
    table.add_column("Code", width=10)

    for i, cls in enumerate(classes, 1):
        table.add_row(str(i), cls.name, cls.short_code)

    console.print(table)
    console.print()


def display_assignments(assignments: list[Assignment], title: str = "Assignments"):
    """Show a Rich table of assignment items."""
    if not assignments:
        console.print(f"  [dim]No items to display for {title}.[/dim]")
        return

    table = Table(
        title=title,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Type", width=4, justify="center")
    table.add_column("Course", min_width=10)
    table.add_column("Title", min_width=20)
    table.add_column("Status", min_width=10)
    table.add_column("Due", min_width=15)
    table.add_column("Platform", width=12)

    for a in assignments:
        icon = TYPE_ICONS.get(a.item_type, "")
        status_style = STATUS_COLORS.get(a.status, "")

        due_text = a.display_due
        if a.is_overdue:
            due_text = f"[bold red]{due_text} (OVERDUE)[/bold red]"

        platform_style = PLATFORM_COLORS.get(a.platform, "")

        table.add_row(
            icon,
            a.course_name or "—",
            a.title,
            f"[{status_style}]{a.status.value}[/{status_style}]" if status_style else a.status.value,
            due_text,
            f"[{platform_style}]{a.platform.value}[/{platform_style}]" if platform_style else a.platform.value,
        )

    console.print(table)
    console.print()


def display_summary(all_assignments: list[Assignment]):
    """Show a summary panel with counts by status and platform."""
    total = len(all_assignments)
    if total == 0:
        console.print(
            Panel(
                "[green bold]All clear! No outstanding assignments found.[/green bold]",
                title="Summary",
                border_style="green",
            )
        )
        return

    by_status: dict[AssignmentStatus, int] = {}
    by_platform: dict[Platform, int] = {}
    overdue_count = 0

    for a in all_assignments:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        by_platform[a.platform] = by_platform.get(a.platform, 0) + 1
        if a.is_overdue:
            overdue_count += 1

    lines = [f"[bold]Total items:[/bold] {total}"]
    if overdue_count:
        lines.append(f"[bold red]Overdue:[/bold red] {overdue_count}")

    lines.append("")
    lines.append("[bold]By status:[/bold]")
    for status, count in sorted(by_status.items(), key=lambda x: x[1], reverse=True):
        style = STATUS_COLORS.get(status, "")
        label = f"[{style}]{status.value}[/{style}]" if style else status.value
        lines.append(f"  {label}: {count}")

    lines.append("")
    lines.append("[bold]By platform:[/bold]")
    for platform, count in sorted(by_platform.items(), key=lambda x: x[1], reverse=True):
        style = PLATFORM_COLORS.get(platform, "")
        label = f"[{style}]{platform.value}[/{style}]" if style else platform.value
        lines.append(f"  {label}: {count}")

    border = "red" if overdue_count else "yellow"
    console.print(
        Panel("\n".join(lines), title="Summary", border_style=border)
    )


# ─── Core logic ─────────────────────────────────────────────────────────

def run(
    username: str,
    password: str,
    headless: bool = False,
    debug: bool = False,
    semester_classes: list[str] | None = None,
    skip_gc: bool = False,
    skip_bs: bool = False,
):
    """Run the aggregator: authenticate, scrape, display."""
    all_assignments: list[Assignment] = []

    auth = TDSBAuth(username=username, password=password, debug=debug)

    try:
        # ─── Launch browser ─────────────────────────────────────────
        with console.status("[bold green]Starting browser…"):
            auth.start_browser(headless=headless)
        console.print("[green]Browser launched.[/green]")

        # ─── Google Classroom ───────────────────────────────────────
        if not skip_gc:
            console.print()
            console.rule("[bold green]Google Classroom[/bold green]")

            with console.status("[bold green]Logging into Google Classroom…"):
                driver = auth.login_google_classroom()
            console.print("[green]Logged into Google Classroom.[/green]")

            gc_scraper = GoogleClassroomScraper(driver, semester_classes=semester_classes)

            with console.status("[bold green]Scraping Google Classroom…"):
                gc_classes, gc_assignments = gc_scraper.scrape_all()

            display_classes(gc_classes, "Google Classroom")

            # Filter to actionable items (assignments/quizzes only)
            gc_actionable = [
                a for a in gc_assignments
                if a.item_type in (ItemType.ASSIGNMENT, ItemType.QUIZ)
            ]
            display_assignments(gc_actionable, "Google Classroom – Incomplete Work")

            # Also try global to-do
            with console.status("[bold green]Checking Google Classroom To-Do…"):
                gc_todo = gc_scraper.scrape_global_todo()

            if gc_todo:
                existing_titles = {a.title for a in gc_actionable}
                new_todo = [a for a in gc_todo if a.title not in existing_titles]
                if new_todo:
                    display_assignments(new_todo, "Google Classroom – To-Do (Additional)")
                    gc_actionable.extend(new_todo)

            all_assignments.extend(gc_actionable)

        # ─── Brightspace ────────────────────────────────────────────
        if not skip_bs:
            console.print()
            console.rule("[bold blue]Brightspace[/bold blue]")

            with console.status("[bold blue]Logging into Brightspace…"):
                driver = auth.login_brightspace()
            console.print("[blue]Logged into Brightspace.[/blue]")

            bs_scraper = BrightspaceScraper(driver, semester_classes=semester_classes)

            with console.status("[bold blue]Scraping Brightspace…"):
                bs_classes, bs_assignments = bs_scraper.scrape_all()

            display_classes(bs_classes, "Brightspace")

            bs_actionable = [
                a for a in bs_assignments
                if a.item_type in (ItemType.ASSIGNMENT, ItemType.QUIZ, ItemType.ANNOUNCEMENT)
            ]
            display_assignments(bs_actionable, "Brightspace – Incomplete Work & Announcements")
            all_assignments.extend(bs_actionable)

        # ─── Combined summary ───────────────────────────────────────
        console.print()
        console.rule("[bold]Combined Summary[/bold]")

        # Sort: overdue first, then by due date
        all_assignments.sort(
            key=lambda a: (
                not a.is_overdue,
                a.due_date or datetime(2099, 12, 31),
            )
        )

        display_assignments(all_assignments, "All Incomplete / Outstanding Work")
        display_summary(all_assignments)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        logger.exception("Fatal error")
    finally:
        with console.status("[dim]Closing browser…"):
            auth.close()
        console.print("[dim]Done.[/dim]")


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Aggregate incomplete assignments from Google Classroom & Brightspace"
    )
    parser.add_argument(
        "--username", default=os.getenv("TDSB_USERNAME", ""),
        help="TDSB email (default: $TDSB_USERNAME from .env)",
    )
    parser.add_argument(
        "--password", default=os.getenv("TDSB_PASSWORD", ""),
        help="TDSB password (default: $TDSB_PASSWORD from .env)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        default=os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes"),
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug mode (screenshots, verbose logging)",
    )
    parser.add_argument(
        "--skip-gc", action="store_true",
        help="Skip Google Classroom scraping",
    )
    parser.add_argument(
        "--skip-bs", action="store_true",
        help="Skip Brightspace scraping",
    )
    parser.add_argument(
        "--classes",
        default=os.getenv("SEMESTER_CLASSES", "ENG,GLE,PPL,History"),
        help="Comma-separated semester class codes (default: $SEMESTER_CLASSES)",
    )

    args = parser.parse_args()

    if not args.username or not args.password:
        console.print(
            "[bold red]Error:[/bold red] Username and password are required.\n"
            "Set TDSB_USERNAME and TDSB_PASSWORD in .env or pass --username and --password."
        )
        sys.exit(1)

    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    semester_classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    console.print(
        Panel(
            f"[bold]Classroom Aggregator[/bold]\n"
            f"User: {args.username}\n"
            f"Classes: {', '.join(semester_classes)}\n"
            f"Headless: {args.headless} | Debug: {args.debug}",
            border_style="cyan",
        )
    )

    run(
        username=args.username,
        password=args.password,
        headless=args.headless,
        debug=args.debug,
        semester_classes=semester_classes,
        skip_gc=args.skip_gc,
        skip_bs=args.skip_bs,
    )


if __name__ == "__main__":
    main()
