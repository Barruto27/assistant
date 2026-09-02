"""Migration runner behaviour (plan Section 2 + principle 5: fail loudly).

A half-applied migration is the one database failure that quietly corrupts
everything downstream, so it gets its own tests.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot.errors import AssistantError, E, setup_logging
from db import database

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class MigrationRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.conn = database.connect(self.root / "test.sqlite3")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _write(self, name: str, sql: str) -> None:
        (self.migrations / name).write_text(sql, encoding="utf-8")

    def test_applies_in_ascending_order(self) -> None:
        self._write("0002_add_column.sql", "ALTER TABLE widgets ADD COLUMN colour TEXT;")
        self._write("0001_create.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")

        self.assertEqual(database.migrate(self.conn, self.migrations), [1, 2])
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(widgets)").fetchall()
        }
        self.assertEqual(columns, {"id", "colour"})

    def test_failed_migration_rolls_back_entirely(self) -> None:
        self._write("0001_create.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
        self._write(
            "0002_broken.sql",
            "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO does_not_exist VALUES (1);",
        )

        with self.assertRaises(AssistantError) as ctx:
            database.migrate(self.conn, self.migrations)
        self.assertEqual(ctx.exception.code, E.DB_MIGRATION)

        # 0001 stays applied; 0002 leaves nothing behind.
        self.assertEqual(database.current_version(self.conn), 1)
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertIn("widgets", tables)
        self.assertNotIn("gadgets", tables, "the partial migration should have rolled back")

    def test_rejects_badly_named_file(self) -> None:
        self._write("add_stuff.sql", "SELECT 1;")
        with self.assertRaises(AssistantError) as ctx:
            database.migrate(self.conn, self.migrations)
        self.assertEqual(ctx.exception.code, E.DB_MIGRATION)

    def test_rejects_duplicate_version_numbers(self) -> None:
        self._write("0001_one.sql", "SELECT 1;")
        self._write("0001_two.sql", "SELECT 1;")
        with self.assertRaises(AssistantError) as ctx:
            database.migrate(self.conn, self.migrations)
        self.assertEqual(ctx.exception.code, E.DB_MIGRATION)


if __name__ == "__main__":
    unittest.main()
