"""Database reads and writes for the message pipeline (plan Section 4).

Every function here raises ``AssistantError`` with a Section 8 code on failure,
so no caller has to interpret a raw ``sqlite3`` exception and no write can fail
silently (principle 5).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from bot.errors import AssistantError, E
from db.database import get_config, transaction

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _write(conn: sqlite3.Connection, sql: str, params: tuple, what: str) -> int:
    """Run one INSERT/UPDATE, returning lastrowid. Raises E401 on failure."""
    try:
        with transaction(conn):
            cursor = conn.execute(sql, params)
            return cursor.lastrowid
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, f"Couldn't save {what}.", cause=exc, trigger=sql
        ) from exc


def _read(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_READ, "Couldn't read that from the database.", cause=exc, trigger=sql
        ) from exc


# ---------------------------------------------------------------------------
# Courses and semester timing
# ---------------------------------------------------------------------------


def course_codes(conn: sqlite3.Connection) -> list[str]:
    return [row["code"] for row in _read(conn, "SELECT code FROM courses ORDER BY code")]


def week_number(conn: sqlite3.Connection, when: date | None = None) -> int | None:
    """Weeks since the configured semester start, 1-indexed. None if unset."""
    raw = get_config(conn, "semester_start_date")
    if not raw:
        return None
    try:
        start = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise AssistantError(
            E.DB_READ,
            f"semester_start_date is {raw!r}, which isn't a YYYY-MM-DD date.",
            cause=exc,
        ) from exc

    today = when or date.today()
    if today < start:
        return None
    return (today - start).days // 7 + 1


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def add_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    type: str = "other",
    course: str | None = None,
    due_date: str | None = None,
    tentative: bool = False,
    weight_pct: float | None = None,
    priority: int = 2,
    notes: str | None = None,
    week: int | None = None,
    source: str = "text",
) -> int:
    return _write(
        conn,
        "INSERT INTO tasks (title, type, course, due_date, tentative, weight_pct, "
        "priority, notes, week_number, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            title,
            type,
            course,
            due_date,
            int(bool(tentative)),
            weight_pct,
            priority,
            notes,
            week,
            source,
        ),
        "that task",
    )


#: Words that carry no matching signal in a task reference.
_STOPWORDS = frozenset(
    {"the", "a", "an", "my", "for", "of", "on", "in", "to", "and", "that", "this"}
)


def find_tasks(
    conn: sqlite3.Connection, query: str, course: str | None = None
) -> list[sqlite3.Row]:
    """Match a loose spoken reference against title *and* course, newest first.

    Every meaningful word must appear somewhere in "title course", rather than
    the whole phrase having to appear in the title. Kaan says "finished the psyc
    test" for a task titled "Test" in PSYC 3040: matching the phrase against the
    title alone finds nothing, because the course words are in a different
    column.

    Deliberately dumb beyond that: the router decides what to do with 0, 1, or
    several matches rather than this guessing at the right one.
    """
    haystack = "(title || ' ' || COALESCE(course, ''))"
    sql = "SELECT * FROM tasks WHERE status != 'archived'"
    params: list[Any] = []

    tokens = [
        word
        for word in query.strip().split()
        if len(word) > 1 and word.lower() not in _STOPWORDS
    ]
    # An all-stopword query (or an empty one) falls back to the raw string, so a
    # deliberate search for something like "a" still behaves predictably.
    for token in tokens or [query.strip()]:
        sql += f" AND {haystack} LIKE ? COLLATE NOCASE"
        params.append(f"%{token}%")

    if course:
        sql += " AND course = ? COLLATE NOCASE"
        params.append(course)
    sql += " ORDER BY COALESCE(due_date, '9999-12-31'), id DESC"
    return _read(conn, sql, tuple(params))


def update_task(conn: sqlite3.Connection, task_id: int, **changes: Any) -> None:
    """Apply named column changes. updated_at is handled by a DB trigger."""
    allowed = {"status", "due_date", "priority", "title", "course", "notes", "weight_pct"}
    fields = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not fields:
        raise AssistantError(E.MISSING_FIELD, "Nothing to change on that task.")

    assignments = ", ".join(f"{name} = ?" for name in fields)
    _write(
        conn,
        f"UPDATE tasks SET {assignments} WHERE id = ?",
        (*fields.values(), task_id),
        "that change",
    )


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    rows = _read(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def add_reminder(conn: sqlite3.Connection, *, text: str, fire_at: str) -> int:
    return _write(
        conn,
        "INSERT INTO reminders (text, fire_at) VALUES (?, ?)",
        (text, fire_at),
        "that reminder",
    )


def due_reminders(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return _read(
        conn,
        "SELECT * FROM reminders WHERE sent = 0 AND fire_at <= ? ORDER BY fire_at",
        (now.strftime(TS_FORMAT),),
    )


def mark_reminder_sent(conn: sqlite3.Connection, reminder_id: int) -> None:
    _write(
        conn,
        "UPDATE reminders SET sent = 1 WHERE id = ?",
        (reminder_id,),
        "the reminder status",
    )


# ---------------------------------------------------------------------------
# Gym, goals, notes
# ---------------------------------------------------------------------------


def set_gym_split(conn: sqlite3.Connection, *, day_of_week: int, split_name: str) -> None:
    _write(
        conn,
        "INSERT INTO gym (day_of_week, split_name) VALUES (?, ?) "
        "ON CONFLICT(day_of_week) DO UPDATE SET split_name = excluded.split_name",
        (day_of_week, split_name),
        "that gym split",
    )


def gym_split_for(conn: sqlite3.Connection, day_of_week: int) -> str | None:
    rows = _read(
        conn, "SELECT split_name FROM gym WHERE day_of_week = ?", (day_of_week,)
    )
    return rows[0]["split_name"] if rows else None


def _goal_expiry(tier: str, now: datetime) -> str:
    if tier == "daily":
        return now.strftime("%Y-%m-%d")
    if tier == "weekly":
        # End of the current week, Sunday.
        return (now + timedelta(days=6 - now.weekday())).strftime("%Y-%m-%d")
    # Monthly: last day of the current month.
    first_next = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (first_next - timedelta(days=1)).strftime("%Y-%m-%d")


def set_goal(
    conn: sqlite3.Connection, *, text: str, tier: str, now: datetime | None = None
) -> int:
    moment = now or datetime.now()
    return _write(
        conn,
        "INSERT INTO goals (text, tier, expires_at) VALUES (?, ?, ?)",
        (text, tier, _goal_expiry(tier, moment)),
        "that goal",
    )


def active_goals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _read(
        conn,
        "SELECT * FROM goals WHERE status = 'active' "
        "ORDER BY CASE tier WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1 ELSE 2 END",
    )


def save_note(
    conn: sqlite3.Connection, *, text: str, tags: list[str] | None = None
) -> int:
    joined = ",".join(t.strip().lower() for t in tags if t.strip()) if tags else None
    return _write(
        conn,
        "INSERT INTO notes (text, tags) VALUES (?, ?)",
        (text, joined),
        "that note",
    )

def stalled_goals(
    conn: sqlite3.Connection, now: datetime, *, quiet_days: int = 5
) -> list[sqlite3.Row]:
    """Weekly and monthly goals with no reported movement for a while.

    Daily goals are excluded: a daily goal that saw no progress is just an
    ordinary day, and saying so every morning is the nagging Section 11 warns
    against. Only the longer horizons are worth a nudge, and only occasionally.
    """
    cutoff = (now - timedelta(days=quiet_days)).strftime(TS_FORMAT)
    return _read(
        conn,
        "SELECT * FROM goals WHERE status = 'active' AND tier IN ('weekly', 'monthly') "
        "AND COALESCE(last_progress_at, created_at) < ? "
        "AND (nudged_at IS NULL OR nudged_at < ?) "
        "ORDER BY tier, id",
        (cutoff, cutoff),
    )


def mark_goal_nudged(conn: sqlite3.Connection, goal_id: int, now: datetime) -> None:
    """Record the nudge so it happens once, not every morning."""
    _write(
        conn,
        "UPDATE goals SET nudged_at = ? WHERE id = ?",
        (now.strftime(TS_FORMAT), goal_id),
        "the goal nudge",
    )


def record_goal_progress(conn: sqlite3.Connection, goal_id: int, now: datetime) -> None:
    """Movement resets both the staleness clock and the nudge."""
    _write(
        conn,
        "UPDATE goals SET last_progress_at = ?, nudged_at = NULL WHERE id = ?",
        (now.strftime(TS_FORMAT), goal_id),
        "the goal progress",
    )


def missed_daily_goals(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    """Yesterday's daily goals that were never closed out.

    Section 11 allows exactly one soft mention, so missed_mentioned gates it.
    """
    return _read(
        conn,
        "SELECT * FROM goals WHERE tier = 'daily' AND status = 'active' "
        "AND missed_mentioned = 0 AND expires_at IS NOT NULL AND expires_at < ?",
        (today.isoformat(),),
    )


def mark_goal_missed_mentioned(conn: sqlite3.Connection, goal_id: int) -> None:
    _write(
        conn,
        "UPDATE goals SET missed_mentioned = 1 WHERE id = ?",
        (goal_id,),
        "the goal",
    )
