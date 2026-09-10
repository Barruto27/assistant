"""Test mode (from real use).

Testing the bot against the live database left a fabricated reminder due to
fire that evening and two real tasks marked done, one an attendance mark for a
lecture that hadn't happened. Nothing distinguished those rows from Kaan's own.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo, testmode  # noqa: E402
from bot.errors import AssistantError, setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 10, 9, 0)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "t.sqlite3"
        self.conn = database.connect(self.db_path)
        database.migrate(self.conn)
        self.real = repo.add_task(self.conn, title="Real essay", due_date="2026-09-20")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def status(self, task_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]

    def counts(self) -> tuple[int, int]:
        return (
            self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"],
            self.conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"],
        )


class LifecycleTestCase(Base):
    def test_off_by_default(self) -> None:
        self.assertFalse(testmode.is_on(self.conn))
        self.assertEqual(testmode.banner(self.conn), "")

    def test_starting_sets_the_flag_and_snapshot(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        self.assertTrue(testmode.is_on(self.conn))
        self.assertTrue(testmode.snapshot_path(self.db_path).exists())
        self.assertIn("TEST MODE", testmode.banner(self.conn))

    def test_starting_twice_is_refused(self) -> None:
        """A second snapshot would overwrite the only way back."""
        testmode.start(self.conn, self.db_path, NOW)
        with self.assertRaises(AssistantError):
            testmode.start(self.conn, self.db_path, NOW)

    def test_stopping_when_off_is_refused(self) -> None:
        with self.assertRaises(AssistantError):
            testmode.stop(self.conn, self.db_path)

    def test_snapshot_is_removed_after_rollback(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        testmode.stop(self.conn, self.db_path)
        self.assertFalse(testmode.snapshot_path(self.db_path).exists())

    def test_a_missing_snapshot_does_not_trap_him_in_test_mode(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        testmode.snapshot_path(self.db_path).unlink()
        with self.assertRaises(AssistantError):
            testmode.stop(self.conn, self.db_path)
        self.assertFalse(testmode.is_on(self.conn), "must not be left stuck on")


class RollbackTestCase(Base):
    def test_inserts_are_discarded(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        repo.add_task(self.conn, title="FAKE", due_date="2026-09-11")
        repo.add_reminder(self.conn, text="FAKE", fire_at="2026-09-10 21:00:00")
        self.assertEqual(self.counts(), (2, 1))

        testmode.stop(self.conn, self.db_path)
        self.assertEqual(self.counts(), (1, 0))

    def test_updates_are_reverted(self) -> None:
        """The one tagging a row couldn't fix: marking real work done."""
        testmode.start(self.conn, self.db_path, NOW)
        repo.update_task(self.conn, self.real, status="done")
        self.assertEqual(self.status(self.real), "done")

        testmode.stop(self.conn, self.db_path)
        self.assertEqual(self.status(self.real), "not_started")

    def test_deletes_are_reverted(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        with database.transaction(self.conn):
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (self.real,))
        self.assertEqual(self.counts()[0], 0)

        testmode.stop(self.conn, self.db_path)
        self.assertEqual(self.counts()[0], 1)

    def test_goals_and_notes_are_reverted(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        repo.set_goal(self.conn, text="FAKE goal", tier="weekly", now=NOW)
        repo.save_note(self.conn, text="FAKE note")
        testmode.stop(self.conn, self.db_path)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"], 0
        )

    def test_flag_is_cleared_by_the_rollback(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        testmode.stop(self.conn, self.db_path)
        self.assertFalse(testmode.is_on(self.conn))
        self.assertIsNone(testmode.started_at(self.conn))

    def test_the_connection_survives_the_restore(self) -> None:
        """The restore happens under a live connection, as it does in the bot."""
        testmode.start(self.conn, self.db_path, NOW)
        repo.add_task(self.conn, title="FAKE", due_date="2026-09-11")
        testmode.stop(self.conn, self.db_path)
        repo.add_task(self.conn, title="After", due_date="2026-09-12")
        titles = {r["title"] for r in self.conn.execute("SELECT title FROM tasks")}
        self.assertEqual(titles, {"Real essay", "After"})

    def test_summary_describes_what_went(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        repo.add_task(self.conn, title="FAKE", due_date="2026-09-11")
        repo.add_reminder(self.conn, text="FAKE", fire_at="2026-09-10 21:00:00")
        summary = testmode.stop(self.conn, self.db_path)
        self.assertIn("task", summary.describe())
        self.assertIn("reminder", summary.describe())

    def test_summary_when_nothing_happened(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        self.assertEqual(
            testmode.stop(self.conn, self.db_path).describe(), "nothing was changed"
        )

    def test_snapshot_is_a_valid_database(self) -> None:
        testmode.start(self.conn, self.db_path, NOW)
        snap = sqlite3.connect(testmode.snapshot_path(self.db_path))
        try:
            self.assertEqual(
                snap.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            snap.close()


if __name__ == "__main__":
    unittest.main()
