"""Test mode: try things out, then throw the results away.

Written after testing the bot against Kaan's live data left a fabricated
reminder due to fire that evening and two real tasks marked done — including an
attendance mark for a lecture that had not happened yet. The next brief would
have been confidently wrong, and there was no way to tell which rows came from
testing and which were his.

The approach is a whole-database snapshot rather than tagging rows, because
tagging only covers inserts. Marking an existing task done is an *update*, and
undoing that needs the previous value, which means journalling every write in
every path — a lot of plumbing to get wrong. A snapshot covers inserts,
updates, and deletes identically, and the database is small enough that copying
it costs nothing.

Snapshot and restore both go through SQLite's own backup API, so they work on a
database the bot currently has open. Copying the file underneath a live
connection is what corrupted this database once already.

The tradeoff, stated plainly wherever it is turned on: anything real entered
while test mode is on is discarded too.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bot.errors import AssistantError, E, logger
from db.database import get_config, set_config

SNAPSHOT_NAME = "test-mode-snapshot.sqlite3"


def snapshot_path(db_path: Path) -> Path:
    return Path(db_path).parent / SNAPSHOT_NAME


def is_on(conn: sqlite3.Connection) -> bool:
    return get_config(conn, "test_mode", "0") == "1"


def started_at(conn: sqlite3.Connection) -> str | None:
    return get_config(conn, "test_mode_started_at", "") or None


@dataclass
class Summary:
    """What changed while test mode was on, for the receipt."""

    tasks: int = 0
    reminders: int = 0
    goals: int = 0
    notes: int = 0

    @property
    def total(self) -> int:
        return self.tasks + self.reminders + self.goals + self.notes

    def describe(self) -> str:
        if not self.total:
            return "nothing was changed"
        parts = [
            f"{n} {label}"
            for n, label in (
                (self.tasks, "task change(s)"),
                (self.reminders, "reminder(s)"),
                (self.goals, "goal(s)"),
                (self.notes, "note(s)"),
            )
            if n
        ]
        return ", ".join(parts)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
        "goals": conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0],
        "notes": conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        "done": conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'done'"
        ).fetchone()[0],
    }


def start(conn: sqlite3.Connection, db_path: Path, now: datetime) -> None:
    """Snapshot the database so everything from here can be undone."""
    if is_on(conn):
        raise AssistantError(
            E.DB_WRITE, "Test mode is already on. /testoff to discard and exit."
        )

    target = snapshot_path(db_path)
    target.unlink(missing_ok=True)
    destination = None
    try:
        # The backup API reads a consistent copy from a live connection; copying
        # the file underneath one is what corrupted this database before.
        #
        # Closed explicitly rather than with a context manager: `with` on a
        # sqlite3 connection manages a transaction, not the connection, so the
        # file would stay open and could not be removed afterwards.
        destination = sqlite3.connect(target)
        conn.backup(destination)
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't snapshot the database, so test mode is off.",
            cause=exc,
        ) from exc
    finally:
        if destination is not None:
            destination.close()

    set_config(conn, "test_mode", "1")
    set_config(conn, "test_mode_started_at", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Test mode ON; snapshot at %s", target)


def stop(conn: sqlite3.Connection, db_path: Path) -> Summary:
    """Restore the snapshot, discarding everything done since."""
    if not is_on(conn):
        raise AssistantError(E.DB_WRITE, "Test mode isn't on.")

    source = snapshot_path(db_path)
    if not source.exists():
        # Refuse rather than leave him in test mode with no way back.
        set_config(conn, "test_mode", "0")
        raise AssistantError(
            E.DB_WRITE,
            "The snapshot is missing, so I can't roll back. Test mode is off, "
            "but anything from the session is still in the database.",
        )

    before = _counts(conn)
    origin = None
    try:
        origin = sqlite3.connect(source)
        after_counts_source = _counts(origin)
        origin.backup(conn)
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't restore the snapshot.", cause=exc
        ) from exc
    finally:
        if origin is not None:
            origin.close()

    summary = Summary(
        tasks=abs(before["tasks"] - after_counts_source["tasks"])
        + abs(before["done"] - after_counts_source["done"]),
        reminders=abs(before["reminders"] - after_counts_source["reminders"]),
        goals=abs(before["goals"] - after_counts_source["goals"]),
        notes=abs(before["notes"] - after_counts_source["notes"]),
    )

    # The restore overwrote config too, so these are already back to '0'.
    set_config(conn, "test_mode", "0")
    set_config(conn, "test_mode_started_at", "")
    source.unlink(missing_ok=True)
    logger.info("Test mode OFF; discarded %s", summary.describe())
    return summary


def banner(conn: sqlite3.Connection) -> str:
    """Prefix for every reply while testing, so nothing is mistaken for real."""
    return "🧪 TEST MODE — " if is_on(conn) else ""
