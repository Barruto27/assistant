"""Read the academic calendar out of Google Calendar (plan Section 3).

Kaan keeps these dates in his calendar, so reading them there beats retyping
them — and beats me assuming them, which is how reading week got recorded as
Oct 10-17 when both his calendar and York's registrar say Oct 10-16.

Two kinds of date come out of this:

*   **Term boundaries** go to ``config``: semester start and end, reading week,
    the exam period. Week numbering depends on them.
*   **Enrolment deadlines** — add, drop, withdraw — become priority 1 tasks.
    They are dates rather than work, but missing one has consequences that no
    amount of effort afterwards can undo, which is exactly what priority 1 is
    for: visible even when overdue.

Only all-day, non-recurring events are considered. Every academic date is one;
lectures are not.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from bot.errors import logger
from bot.google_calendar import CalendarEvent
from db.database import get_config, set_config, transaction

#: config key -> (pattern, which end of the event to take).
#: Ordered most specific first: the winter reading week must not be matched by
#: the pattern for the fall one.
RULES: list[tuple[str, str, str]] = [
    ("semester_start_date", r"\bclasses?\s+start\b", "start"),
    ("semester_end_date", r"\bclasses?\s+end\b", "end"),
    ("reading_week_start", r"^(?!winter).*\breading\s+week\b", "start"),
    ("reading_week_end", r"^(?!winter).*\breading\s+week\b", "end"),
    ("exam_period_start", r"\bexam\s+days?\b|\bexam\s+period\b|\bexaminations\b", "start"),
    ("exam_period_end", r"\bexam\s+days?\b|\bexam\s+period\b|\bexaminations\b", "end"),
]

#: Enrolment deadlines, tracked as tasks. (title, pattern, which end).
#: The titles are fixed so re-running matches on them and never duplicates.
DEADLINES: list[tuple[str, str, str]] = [
    ("Last day to add a course", r"\badd a course\b|\blast (date|day) to add\b", "start"),
    (
        "Last day to drop a course (no grade)",
        r"\bdrop\b.*\bwithout receiving a grade\b|\bdrop deadline\b",
        "start",
    ),
    (
        "Last day to withdraw (W on transcript)",
        r"\bwithdraw\b|\breceive a w\b",
        "end",
    ),
]

HUMAN = {
    "semester_start_date": "Classes start",
    "semester_end_date": "Classes end",
    "reading_week_start": "Reading week",
    "reading_week_end": "Reading week ends",
    "exam_period_start": "Exams",
    "exam_period_end": "Exams end",
}


@dataclass(frozen=True)
class Found:
    key: str
    value: str
    source: str


@dataclass(frozen=True)
class Deadline:
    title: str
    due: str
    source: str


def _span(event: CalendarEvent) -> tuple[date, date]:
    """Inclusive start and end.

    Google gives all-day events an exclusive end date: a single day on the 9th
    ends on the 10th. Stored unadjusted that puts reading week a day long and
    the exam period a day past its last exam.
    """
    start = event.start if not isinstance(event.start, datetime) else event.start.date()
    end = event.end
    if end is None:
        return start, start
    end = end if not isinstance(end, datetime) else end.date()
    if event.all_day:
        end = end - timedelta(days=1)
    return start, max(start, end)


def _candidates(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    return [
        e for e in events
        if e.all_day and not e.recurring and e.summary and e.summary.strip()
    ]


def _choose(matches: list[tuple[date, date, str]], which: str, reference: date):
    """Pick the match belonging to the term ``reference`` falls in.

    A calendar holding a full academic year contains two "classes start"
    entries, so this has to be anchored to today. Taking the first in iteration
    order picked the previous January's winter term and reported week 36.
    """
    if which == "start":
        past = [m for m in matches if m[0] <= reference]
        chosen = max(past, key=lambda m: m[0]) if past else min(matches, key=lambda m: m[0])
        return chosen[0], chosen[2]
    future = [m for m in matches if m[1] >= reference]
    chosen = min(future, key=lambda m: m[1]) if future else max(matches, key=lambda m: m[1])
    return chosen[1], chosen[2]


def _match(
    candidates: list[CalendarEvent], pattern: str, term_year: int | None
) -> list[tuple[date, date, str]]:
    regex = re.compile(pattern, re.IGNORECASE)
    matches = []
    for event in candidates:
        title = " ".join(event.summary.split())
        if not regex.search(title):
            continue
        start, end = _span(event)
        if term_year is not None and start.year not in (term_year - 1, term_year, term_year + 1):
            continue
        matches.append((start, end, title))
    return matches


def find(
    events: Iterable[CalendarEvent],
    *,
    today: date | None = None,
    term_year: int | None = None,
) -> list[Found]:
    """Term boundaries for the term ``today`` falls in."""
    reference = today or date.today()
    candidates = _candidates(events)

    found: list[Found] = []
    for key, pattern, which in RULES:
        matches = _match(candidates, pattern, term_year)
        if not matches:
            continue
        value, source = _choose(matches, which, reference)
        found.append(Found(key, value.isoformat(), source))
    return found


def find_deadlines(
    events: Iterable[CalendarEvent],
    *,
    today: date | None = None,
    term_year: int | None = None,
) -> list[Deadline]:
    """Enrolment deadlines for the current term."""
    reference = today or date.today()
    candidates = _candidates(events)

    deadlines: list[Deadline] = []
    for title, pattern, which in DEADLINES:
        matches = _match(candidates, pattern, term_year)
        if not matches:
            continue
        value, source = _choose(matches, which, reference)
        deadlines.append(Deadline(title, value.isoformat(), source))
    return deadlines


def apply(conn: sqlite3.Connection, found: list[Found]) -> list[Found]:
    """Store term boundaries. Returns what actually changed."""
    changed = []
    for entry in found:
        if get_config(conn, entry.key) != entry.value:
            set_config(conn, entry.key, entry.value)
            changed.append(entry)
    if changed:
        logger.info("Term dates updated: %s", ", ".join(e.key for e in changed))
    return changed


def apply_deadlines(conn: sqlite3.Connection, deadlines: list[Deadline]) -> list[Deadline]:
    """Record deadlines as priority 1 tasks. Returns what changed.

    Matched on the fixed title so re-running updates a moved date rather than
    adding a second copy. Priority 1 keeps them visible even once past, since
    the backlog rule exempts that priority — a missed drop deadline is exactly
    the thing that should not quietly disappear.
    """
    changed = []
    with transaction(conn):
        for deadline in deadlines:
            row = conn.execute(
                "SELECT id, due_date FROM tasks WHERE title = ? AND source = 'seed'",
                (deadline.title,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks (title, type, due_date, priority, status, "
                    "source, notes) VALUES (?, 'other', ?, 1, 'not_started', 'seed', ?)",
                    (deadline.title, deadline.due, f"York enrolment deadline ({deadline.source})"),
                )
                changed.append(deadline)
            elif row["due_date"] != deadline.due:
                conn.execute(
                    "UPDATE tasks SET due_date = ? WHERE id = ?", (deadline.due, row["id"])
                )
                changed.append(deadline)
    if changed:
        logger.info("Enrolment deadlines updated: %d", len(changed))
    return changed


def _pretty(iso: str) -> str:
    try:
        return f"{datetime.strptime(iso, '%Y-%m-%d'):%b %-d}"
    except ValueError:
        try:
            return f"{datetime.strptime(iso, '%Y-%m-%d'):%b %d}".replace(" 0", " ")
        except ValueError:
            return iso


def render(
    found: list[Found],
    changed: list[Found],
    deadlines: list[Deadline] | None = None,
    *,
    today: date | None = None,
    week: int | None = None,
) -> str:
    """A readable summary, not a dump of config keys."""
    if not found and not deadlines:
        return (
            "I couldn't find any academic dates in your calendar. I look for "
            'all-day events named like "classes start", "classes end", '
            '"reading week", or "exam days".'
        )

    values = {f.key: f.value for f in found}
    changed_keys = {c.key for c in changed}
    reference = today or date.today()
    lines: list[str] = []

    def span(start_key: str, end_key: str, label: str) -> None:
        start, end = values.get(start_key), values.get(end_key)
        if not start and not end:
            return
        mark = " *" if {start_key, end_key} & changed_keys else ""
        if start and end:
            lines.append(f"  {label:<14} {_pretty(start)} – {_pretty(end)}{mark}")
        else:
            lines.append(f"  {label:<14} {_pretty(start or end)}{mark}")

    lines.append("Term")
    span("semester_start_date", "semester_end_date", "Classes")
    span("reading_week_start", "reading_week_end", "Reading week")
    span("exam_period_start", "exam_period_end", "Exams")

    if deadlines:
        lines.append("")
        lines.append("Enrolment deadlines")
        for deadline in sorted(deadlines, key=lambda d: d.due):
            due = date.fromisoformat(deadline.due)
            days = (due - reference).days
            if days > 0:
                when = f"in {days} day{'s' if days != 1 else ''}"
            elif days == 0:
                when = "today"
            else:
                when = "passed"
            lines.append(f"  {_pretty(deadline.due):<8} {deadline.title}  ({when})")

    missing = [k for k, _, _ in RULES if k not in values]
    if missing:
        lines.append("")
        lines.append("Couldn't find: " + ", ".join(HUMAN.get(k, k) for k in missing))

    if week is not None:
        lines.append("")
        lines.append(f"Today is week {week}.")
    if changed_keys:
        lines.append("(* changed just now)")
    return "\n".join(lines)
