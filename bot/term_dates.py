"""Read the academic calendar out of Google Calendar (plan Section 3).

The last item on the onboarding checklist: semester start and end, reading week,
and the exam period into ``config``. Kaan already keeps these in his calendar,
so reading them there beats retyping them — and beats me assuming them, which
is how reading week got recorded as Oct 10-17 when the calendar says Oct 10-16.

Only all-day, non-recurring events are considered. Every academic date in his
calendar is one, and lectures are not.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from bot.errors import logger
from bot.google_calendar import CalendarEvent
from db.database import set_config

#: config key -> (pattern, which end of the event to take).
#: Ordered most specific first: "winter reading week" must not be matched by the
#: pattern for the fall one.
RULES: list[tuple[str, str, str]] = [
    ("semester_start_date", r"\bclasses?\s+start\b", "start"),
    ("semester_end_date", r"\bclasses?\s+end\b", "end"),
    ("reading_week_start", r"^(?!winter).*\breading\s+week\b", "start"),
    ("reading_week_end", r"^(?!winter).*\breading\s+week\b", "end"),
    ("exam_period_start", r"\bexam\s+days?\b|\bexam\s+period\b", "start"),
    ("exam_period_end", r"\bexam\s+days?\b|\bexam\s+period\b", "end"),
]


@dataclass(frozen=True)
class Found:
    key: str
    value: str
    source: str


def _span(event: CalendarEvent) -> tuple[date, date]:
    """Inclusive start and end.

    Google gives all-day events an exclusive end date: a single day on the 9th
    ends on the 10th. Storing that unadjusted would put reading week a day long
    and the exam period a day past its last exam.
    """
    start = event.start if not isinstance(event.start, datetime) else event.start.date()
    end = event.end
    if end is None:
        return start, start
    end = end if not isinstance(end, datetime) else end.date()
    if event.all_day:
        end = end - timedelta(days=1)
    return start, max(start, end)


def find(events: Iterable[CalendarEvent], *, term_year: int | None = None) -> list[Found]:
    """Match academic dates. Returns only what was actually found."""
    candidates = [
        e for e in events
        if e.all_day and not e.recurring and e.summary and e.summary.strip()
    ]

    found: list[Found] = []
    for key, pattern, which in RULES:
        regex = re.compile(pattern, re.IGNORECASE)
        for event in candidates:
            title = " ".join(event.summary.split())
            if not regex.search(title):
                continue
            start, end = _span(event)
            if term_year is not None and start.year not in (term_year, term_year + 1):
                continue
            value = start if which == "start" else end
            found.append(Found(key, value.isoformat(), title))
            break
    return found


def apply(conn: sqlite3.Connection, found: list[Found]) -> list[Found]:
    """Store what was found. Returns the entries that actually changed."""
    from db.database import get_config

    changed = []
    for entry in found:
        if get_config(conn, entry.key) != entry.value:
            set_config(conn, entry.key, entry.value)
            changed.append(entry)
    if changed:
        logger.info("Term dates updated: %s", ", ".join(e.key for e in changed))
    return changed


def render(found: list[Found], changed: list[Found]) -> str:
    """The receipt: what was read, and from which calendar entry."""
    if not found:
        return (
            "I couldn't find any academic dates in your calendar. I look for "
            "all-day events named like \"classes start\", \"classes end\", "
            "\"reading week\", or \"exam days\"."
        )

    changed_keys = {e.key for e in changed}
    lines = ["Term dates from your calendar:", ""]
    for entry in found:
        mark = " (updated)" if entry.key in changed_keys else ""
        lines.append(f"  {entry.key}: {entry.value}{mark}")
        lines.append(f"      from \"{entry.source}\"")

    missing = [key for key, _, _ in RULES if key not in {e.key for e in found}]
    if missing:
        lines.append("")
        lines.append("Not found: " + ", ".join(sorted(set(missing))))
    return "\n".join(lines)
