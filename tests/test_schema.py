"""Schema shakedown (plan Section 2 definition of done).

Confirms the schema holds the shapes we expect and rejects the ones we don't.
Run from the repo root::

    python -m unittest discover tests
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.errors import setup_logging
from db import database

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # -- migrations ---------------------------------------------------------

    def test_migrations_apply_and_are_idempotent(self) -> None:
        # Tied to the files on disk rather than a hardcoded number, so adding a
        # migration doesn't break this test for no reason.
        latest = max(n for n, _ in database._discover_migrations())
        self.assertEqual(database.current_version(self.conn), latest)
        self.assertEqual(database.migrate(self.conn), [], "re-running should be a no-op")
        self.assertEqual(database.current_version(self.conn), latest)

    def test_all_planned_tables_exist(self) -> None:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row["name"] for row in rows}
        self.assertLessEqual(
            {
                "tasks", "reminders", "gym", "notes", "known_senders",
                "goals", "courses", "course_weeks", "config", "schema_migrations",
            },
            names,
        )

    # -- tasks --------------------------------------------------------------

    def test_task_roundtrip_with_defaults(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (title, type, course, due_date, weight_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Test 2", "test", "PSYC 3040", "2026-04-13", 20.0),
            )
        row = self.conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["title"], "Test 2")
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["status"], "not_started")
        self.assertEqual(row["tentative"], 0)
        self.assertEqual(row["source"], "text")
        self.assertIsNotNone(row["created_at"])

    def test_task_rejects_bad_enum_values(self) -> None:
        for column, value in (
            ("priority", 4),
            ("status", "sorta_done"),
            ("type", "vibes"),
            ("source", "telepathy"),
        ):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        f"INSERT INTO tasks (title, {column}) VALUES (?, ?)",
                        ("bad row", value),
                    )

    def test_updated_at_trigger_fires_on_update(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute("INSERT INTO tasks (title) VALUES ('essay')")
        # Force a distinguishable starting value rather than sleeping a second.
        with database.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET updated_at = '2000-01-01 00:00:00'")
        with database.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'in_progress'")

        row = self.conn.execute("SELECT status, updated_at FROM tasks").fetchone()
        self.assertEqual(row["status"], "in_progress")
        self.assertNotEqual(row["updated_at"], "2000-01-01 00:00:00")

    # -- courses ------------------------------------------------------------

    def test_course_weeks_cascade_on_course_delete(self) -> None:
        with database.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO courses (code, name) VALUES ('PSYC 3040', 'Cognition')"
            )
            course_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO course_weeks (course_id, week_number, topic) "
                "VALUES (?, 3, 'Memory')",
                (course_id,),
            )
        with database.transaction(self.conn):
            self.conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))

        remaining = self.conn.execute("SELECT COUNT(*) AS n FROM course_weeks").fetchone()
        self.assertEqual(remaining["n"], 0, "foreign_keys pragma should cascade")

    def test_course_code_is_unique(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute("INSERT INTO courses (code) VALUES ('PSYC 3040')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO courses (code) VALUES ('PSYC 3040')")

    # -- gym / goals / reminders -------------------------------------------

    def test_gym_one_row_per_day(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute("INSERT INTO gym (day_of_week, split_name) VALUES (0, 'Push')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO gym (day_of_week, split_name) VALUES (0, 'Pull')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO gym (day_of_week, split_name) VALUES (7, 'Nope')")

    def test_goal_tier_is_constrained(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute("INSERT INTO goals (text, tier) VALUES ('read 20 pages', 'daily')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO goals (text, tier) VALUES ('x', 'yearly')")

    def test_pending_reminders_query(self) -> None:
        with database.transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO reminders (text, fire_at, sent) VALUES (?, ?, ?)",
                [
                    ("call mom", "2026-09-02 16:00:00", 0),
                    ("already sent", "2026-09-01 16:00:00", 1),
                ],
            )
        rows = self.conn.execute(
            "SELECT text FROM reminders WHERE sent = 0 AND fire_at <= ?",
            ("2026-09-02 16:01:00",),
        ).fetchall()
        self.assertEqual([r["text"] for r in rows], ["call mom"])

    # -- config -------------------------------------------------------------

    def test_config_defaults_seeded(self) -> None:
        self.assertEqual(database.get_config(self.conn, "brief_send_time"), "07:30")
        self.assertEqual(database.get_config(self.conn, "backlog_threshold_days"), "7")
        # Seeded-but-empty values fall back to the caller's default.
        self.assertEqual(
            database.get_config(self.conn, "semester_start_date", "unset"), "unset"
        )

    def test_config_set_is_an_upsert(self) -> None:
        database.set_config(self.conn, "semester_start_date", "2026-09-07")
        self.assertEqual(
            database.get_config(self.conn, "semester_start_date"), "2026-09-07"
        )
        database.set_config(self.conn, "semester_start_date", "2026-09-14")
        self.assertEqual(
            database.get_config(self.conn, "semester_start_date"), "2026-09-14"
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM config WHERE key = 'semester_start_date'"
        ).fetchone()
        self.assertEqual(count["n"], 1)

    def test_transaction_rolls_back_on_error(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.conn):
                self.conn.execute("INSERT INTO tasks (title) VALUES ('keeper')")
                self.conn.execute("INSERT INTO tasks (title, priority) VALUES ('bad', 9)")
        count = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        self.assertEqual(count["n"], 0, "the good insert should roll back too")


if __name__ == "__main__":
    unittest.main()
