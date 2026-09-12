"""Flagged email surviving past the message it appeared in.

A flag used to exist only inside one morning brief. On Sep 12 the 07:30 scan
flagged three emails, one of them a CMDS syllabus quiz that was actual work.
The evening check-in had no idea it had been raised - it was skipped entirely
as "nothing on today" - and three days later the email would have dropped out
of the scan window without ever being asked about again.

It also could not be recognised between scans, because the only thing stored
was the sentence the model wrote, and it writes a different one every time. The
same PSYC announcement came back three ways across three runs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import checkin, repository as repo  # noqa: E402
from bot.email_reader import FlaggedEmail  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 12, 21, 0)

QUIZ = FlaggedEmail(
    kind="new_work",
    summary="CMDS 1630 Syllabus Quiz is live under Week 1",
    course="CMDS 1630",
    message_id="<quiz@eclass.yorku.ca>",
)
# The same email, worded the way a later run worded it.
QUIZ_REWORDED = FlaggedEmail(
    kind="announcement",
    summary="Syllabus Quiz now open online for CMDS 1630 after a delay",
    course="CMDS 1630",
    message_id="<quiz@eclass.yorku.ca>",
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def ids_of(self, recorded) -> list[int]:
        return [row_id for _, row_id in recorded]


class RecognisedBetweenScansTestCase(Base):
    def test_the_same_email_is_new_only_once(self) -> None:
        self.assertEqual(len(repo.remember_flagged(self.conn, [QUIZ])), 1)
        self.assertEqual(len(repo.remember_flagged(self.conn, [QUIZ])), 0)

    def test_rewording_does_not_make_it_new_again(self) -> None:
        """The actual failure: the model never words it the same way twice."""
        repo.remember_flagged(self.conn, [QUIZ])
        self.assertEqual(len(repo.remember_flagged(self.conn, [QUIZ_REWORDED])), 0)

    def test_an_unidentified_flag_is_kept_rather_than_dropped(self) -> None:
        """Shown twice beats silently lost."""
        anon = FlaggedEmail(kind="announcement", summary="something", message_id="")
        self.assertEqual(len(repo.remember_flagged(self.conn, [anon])), 1)
        self.assertEqual(len(repo.remember_flagged(self.conn, [anon])), 1)

    def test_the_row_id_comes_back_so_callers_mark_what_they_showed(self) -> None:
        recorded = repo.remember_flagged(self.conn, [QUIZ])
        item, row_id = recorded[0]
        self.assertIs(item, QUIZ)
        self.assertIsInstance(row_id, int)


class RaisedTwiceThenDroppedTestCase(Base):
    """Once in the brief, once in the check-in, then it stops."""

    def setUp(self) -> None:
        super().setUp()
        self.ids = self.ids_of(repo.remember_flagged(self.conn, [QUIZ]))

    def test_it_starts_outstanding(self) -> None:
        self.assertEqual(len(repo.outstanding_flagged(self.conn)), 1)

    def test_still_outstanding_after_the_brief(self) -> None:
        repo.mark_flagged_raised(self.conn, self.ids)
        rows = repo.outstanding_flagged(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "raised")

    def test_gone_after_the_check_in(self) -> None:
        repo.mark_flagged_raised(self.conn, self.ids)
        repo.mark_flagged_raised(self.conn, self.ids)
        self.assertEqual(repo.outstanding_flagged(self.conn), [])

    def test_it_is_closed_not_deleted(self) -> None:
        repo.mark_flagged_raised(self.conn, self.ids)
        repo.mark_flagged_raised(self.conn, self.ids)
        row = self.conn.execute(
            "SELECT status, times_raised FROM flagged_emails"
        ).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["times_raised"], 2)

    def test_marking_nothing_is_harmless(self) -> None:
        repo.mark_flagged_raised(self.conn, [])
        self.assertEqual(len(repo.outstanding_flagged(self.conn)), 1)


class CheckinPicksItUpTestCase(Base):
    def test_a_quiet_night_with_mail_is_still_worth_asking(self) -> None:
        """Sep 11 was skipped as "nothing on today" with mail outstanding."""
        empty = checkin.gather(self.conn, NOW)
        self.assertFalse(checkin.has_anything_to_ask(empty))

        repo.remember_flagged(self.conn, [QUIZ])
        with_mail = checkin.gather(self.conn, NOW)
        self.assertTrue(checkin.has_anything_to_ask(with_mail))

    def test_the_mail_reaches_the_check_in_context(self) -> None:
        repo.remember_flagged(self.conn, [QUIZ])
        rendered = checkin.render_context(checkin.gather(self.conn, NOW))
        self.assertIn("Syllabus Quiz", rendered)
        self.assertIn("CMDS 1630", rendered)

    def test_it_is_not_presented_as_a_task(self) -> None:
        """No id, because there is no row for him to mark done."""
        repo.remember_flagged(self.conn, [QUIZ])
        rendered = checkin.render_context(checkin.gather(self.conn, NOW))
        self.assertIn("NOT SAVED", rendered)
        self.assertIn("not a task", rendered.lower())

    def test_closed_mail_stops_appearing(self) -> None:
        ids = self.ids_of(repo.remember_flagged(self.conn, [QUIZ]))
        repo.mark_flagged_raised(self.conn, ids)
        repo.mark_flagged_raised(self.conn, ids)
        rendered = checkin.render_context(checkin.gather(self.conn, NOW))
        self.assertNotIn("Syllabus Quiz", rendered)

    def test_a_date_from_the_mail_is_shown(self) -> None:
        moved = FlaggedEmail(
            kind="deadline_change",
            summary="A1 moved",
            course="DATT 1200",
            new_date="2026-10-07",
            message_id="<moved@x>",
        )
        repo.remember_flagged(self.conn, [moved])
        rendered = checkin.render_context(checkin.gather(self.conn, NOW))
        self.assertIn("2026-10-07", rendered)


if __name__ == "__main__":
    unittest.main()
