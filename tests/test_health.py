"""Daily credential self-check (plan Section 8)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import google_calendar  # noqa: E402
from bot.errors import E, setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class VerifyTestCase(unittest.TestCase):
    """verify() returns the failure instead of raising, so the job can decide."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.secrets = self.root / "client_secret.json"
        self.secrets.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_token_reports_not_connected(self) -> None:
        failure = google_calendar.verify(self.root / "absent.json", self.secrets)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, E.TOKEN_EXPIRED)
        self.assertIn("google_auth", failure.message)

    def test_corrupt_token_is_reported_not_crashed(self) -> None:
        token = self.root / "token.json"
        token.write_text("this is not json", encoding="utf-8")
        failure = google_calendar.verify(token, self.secrets)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, E.TOKEN_EXPIRED)

    def test_token_missing_required_fields_is_reported(self) -> None:
        token = self.root / "token.json"
        token.write_text(json.dumps({"nonsense": True}), encoding="utf-8")
        failure = google_calendar.verify(token, self.secrets)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, E.TOKEN_EXPIRED)

    def test_loud_codes_render_unmissably(self) -> None:
        """E301/E302 must not read like an ordinary error (Section 8)."""
        failure = google_calendar.verify(self.root / "absent.json", self.secrets)
        self.assertIn("ACTION NEEDED", failure.user_message())
        self.assertIn(E.TOKEN_EXPIRED, failure.user_message())


class TokenAlertConfigTestCase(unittest.TestCase):
    """Migration 0002 adds the scheduling and once-a-day bookkeeping."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_migration_0002_applied(self) -> None:
        self.assertEqual(database.current_version(self.conn), 2)

    def test_check_time_default(self) -> None:
        self.assertEqual(database.get_config(self.conn, "token_check_time"), "08:15")

    def test_alert_marker_starts_empty(self) -> None:
        self.assertEqual(
            database.get_config(self.conn, "token_alerted_on", "unset"), "unset"
        )

    def test_migration_does_not_clobber_an_existing_value(self) -> None:
        """ON CONFLICT DO NOTHING: re-running must not reset a tuned value."""
        database.set_config(self.conn, "token_check_time", "09:00")
        self.conn.executescript(
            (Path("db/migrations/0002_token_check_config.sql")).read_text(encoding="utf-8")
        )
        self.assertEqual(database.get_config(self.conn, "token_check_time"), "09:00")


if __name__ == "__main__":
    unittest.main()
