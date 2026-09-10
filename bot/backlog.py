"""Priority and backlog triage (plan Section 9).

"Stay in the now." Work that went overdue weeks ago and never mattered much
should stop appearing every morning. Left alone it accumulates until the brief
is mostly a list of things Kaan has already decided not to do, at which point
he stops reading the parts that matter.

Three rules, in the plan's words:

*   Overdue past a configurable threshold moves to ``stale`` and drops out of
    the daily view.
*   **Priority 1 is exempt.** An exam stays visible however late it is; that is
    the whole point of the priority.
*   Nothing is ever deleted. Stale work is still there, still queryable, and
    archiving needs Kaan to say so.

Attendance marks are demoted immediately once past, since a missed lecture
cannot be made up and nothing is achieved by carrying it forward.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.errors import AssistantError, E, logger
from bot.formatting import pct
from db.database import get_config, transaction

DEFAULT_THRESHOLD_DAYS = 7


@dataclass(frozen=True)
class Demotion:
    task_id: int
    title: str
    course: str | None
    due_date: str | None
    reason: str


def threshold_days(conn: sqlite3.Connection) -> int:
    raw = get_config(conn, "backlog_threshold_days", str(DEFAULT_THRESHOLD_DAYS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("backlog_threshold_days is %r; using the default", raw)
        return DEFAULT_THRESHOLD_DAYS


def find_demotable(conn: sqlite3.Connection, today: date) -> list[Demotion]:
    """What has gone quiet enough to fall out of the daily view."""
    cutoff = (today - timedelta(days=threshold_days(conn))).isoformat()
    rows = conn.execute(
        "SELECT id, title, course, due_date, priority, attendance FROM tasks "
        "WHERE status IN ('not_started', 'in_progress') "
        "AND due_date IS NOT NULL "
        "AND priority != 1 "                       # exams stay, however late
        "AND (due_date < ? OR (attendance = 1 AND due_date < ?)) "
        "ORDER BY due_date",
        (cutoff, today.isoformat()),
    ).fetchall()

    demotions = []
    for row in rows:
        if row["attendance"] and row["due_date"] < today.isoformat():
            reason = "attendance mark for a lecture that has passed"
        else:
            reason = f"overdue by more than {threshold_days(conn)} days"
        demotions.append(
            Demotion(row["id"], row["title"], row["course"], row["due_date"], reason)
        )
    return demotions


def demote(conn: sqlite3.Connection, today: date | None = None) -> list[Demotion]:
    """Move qualifying work to stale. Returns what moved."""
    day = today or date.today()
    demotions = find_demotable(conn, day)
    if not demotions:
        return []

    try:
        with transaction(conn):
            conn.executemany(
                "UPDATE tasks SET status = 'stale' WHERE id = ?",
                [(d.task_id,) for d in demotions],
            )
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't move overdue work to the backlog.", cause=exc
        ) from exc

    logger.info("Moved %d task(s) to the backlog", len(demotions))
    return demotions


def backlog(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything currently set aside. Still here, just not in the way."""
    return conn.execute(
        "SELECT id, title, course, due_date, weight_pct, priority, attendance "
        "FROM tasks WHERE status = 'stale' "
        "ORDER BY COALESCE(due_date, '9999-12-31')"
    ).fetchall()


def restore(conn: sqlite3.Connection, task_id: int) -> bool:
    """Pull one item back into the daily view."""
    try:
        with transaction(conn):
            cursor = conn.execute(
                "UPDATE tasks SET status = 'not_started' "
                "WHERE id = ? AND status = 'stale'",
                (task_id,),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't bring that back.", cause=exc
        ) from exc


def archive(conn: sqlite3.Connection, task_ids: list[int]) -> int:
    """Archive, never delete. The plan is explicit that nothing is destroyed."""
    if not task_ids:
        return 0
    placeholders = ", ".join("?" for _ in task_ids)
    try:
        with transaction(conn):
            cursor = conn.execute(
                f"UPDATE tasks SET status = 'archived' WHERE id IN ({placeholders}) "
                "AND status = 'stale'",
                tuple(task_ids),
            )
            return cursor.rowcount
    except sqlite3.Error as exc:
        raise AssistantError(E.DB_WRITE, "Couldn't archive those.", cause=exc) from exc


def render(rows: list[sqlite3.Row]) -> str:
    """The /backlog reply."""
    if not rows:
        return "Backlog is empty — nothing has fallen behind."

    lines = [f"{len(rows)} item(s) set aside:", ""]
    for row in rows:
        bits = [row["due_date"] or "no date", row["title"]]
        if row["course"]:
            bits.append(row["course"])
        if row["weight_pct"] is not None:
            bits.append(f"{pct(row['weight_pct'])}%")
        if row["attendance"]:
            bits.append("attendance")
        lines.append(f"  #{row['id']} · " + " · ".join(bits))
    lines.append("")
    lines.append("Say \"bring back #12\" to restore one, or \"archive the backlog\" to file them.")
    return "\n".join(lines)


def weekly_nudge_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Whether to mention the backlog in today's brief.

    Weekly, not daily: the plan calls for a low-visibility surface. A daily
    count of things he has already decided not to do is exactly the nagging
    this feature exists to prevent.
    """
    if not backlog(conn):
        return False
    last = get_config(conn, "backlog_nudged_on", "")
    if not last:
        return True
    try:
        previous = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (now.date() - previous).days >= 7
