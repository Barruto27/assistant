"""Teaching-week numbering (plan Sections 3 and 7).

Syllabus week numbers skip reading week. Counting raw elapsed weeks put every
date after the break one week ahead — 7 of the 12 weeks in a real course
schedule — which would have shown the wrong week and pulled the wrong topic,
since course_weeks is joined on this number. It would have appeared six weeks
after going live and looked like a bad extraction rather than a date bug.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo  # noqa: E402
from bot.errors import AssistantError, setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

#: CMDS 1630's own schedule, read off the syllabus. Fridays, with the break
#: between weeks 5 and 6.
SYLLABUS_WEEKS = {
    1: "2026-09-11", 2: "2026-09-18", 3: "2026-09-25", 4: "2026-10-02",
    5: "2026-10-09", 6: "2026-10-23", 7: "2026-10-30", 8: "2026-11-06",
    9: "2026-11-13", 10: "2026-11-20", 11: "2026-11-27", 12: "2026-12-04",
}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def set_break(self, start: str = "2026-10-10", end: str = "2026-10-17") -> None:
        database.set_config(self.conn, "reading_week_start", start)
        database.set_config(self.conn, "reading_week_end", end)


class WeekNumberTestCase(Base):
    def test_matches_the_syllabus_for_every_week(self) -> None:
        self.set_break()
        for week, day in SYLLABUS_WEEKS.items():
            with self.subTest(week=week):
                self.assertEqual(
                    repo.week_number(self.conn, date.fromisoformat(day)), week
                )

    def test_without_a_break_configured_it_drifts(self) -> None:
        """Documents why the dates have to be loaded, not left blank."""
        self.assertEqual(repo.week_number(self.conn, date(2026, 10, 23)), 7)
        self.set_break()
        self.assertEqual(repo.week_number(self.conn, date(2026, 10, 23)), 6)

    def test_the_count_holds_during_the_break(self) -> None:
        self.set_break()
        for day in ("2026-10-12", "2026-10-14", "2026-10-16"):
            self.assertEqual(repo.week_number(self.conn, date.fromisoformat(day)), 5)

    def test_in_reading_week_is_true_only_inside_it(self) -> None:
        self.set_break()
        self.assertFalse(repo.in_reading_week(self.conn, date(2026, 10, 9)))
        self.assertTrue(repo.in_reading_week(self.conn, date(2026, 10, 10)))
        self.assertTrue(repo.in_reading_week(self.conn, date(2026, 10, 17)))
        self.assertFalse(repo.in_reading_week(self.conn, date(2026, 10, 18)))

    def test_no_break_configured_means_never_in_one(self) -> None:
        self.assertFalse(repo.in_reading_week(self.conn, date(2026, 10, 14)))

    def test_before_the_semester_starts_there_is_no_week(self) -> None:
        self.assertIsNone(repo.week_number(self.conn, date(2026, 8, 30)))

    def test_no_start_date_means_no_week(self) -> None:
        database.set_config(self.conn, "semester_start_date", "")
        self.assertIsNone(repo.week_number(self.conn, date(2026, 10, 1)))

    def test_a_two_week_break_removes_two_weeks(self) -> None:
        self.set_break("2026-10-10", "2026-10-24")
        # Fifteen calendar days, so two teaching weeks.
        self.assertEqual(repo.week_number(self.conn, date(2026, 10, 30)), 6)

    def test_a_malformed_break_is_ignored_rather_than_crashing(self) -> None:
        database.set_config(self.conn, "reading_week_start", "2026-10-10")
        database.set_config(self.conn, "reading_week_end", "")
        self.assertEqual(repo.week_number(self.conn, date(2026, 10, 23)), 7)

    def test_a_bad_date_is_reported_not_swallowed(self) -> None:
        database.set_config(self.conn, "semester_start_date", "sometime in September")
        with self.assertRaises(AssistantError):
            repo.week_number(self.conn, date(2026, 10, 1))

    def test_never_returns_zero_or_negative(self) -> None:
        self.set_break("2026-09-09", "2026-09-30")
        for day in ("2026-09-08", "2026-09-09", "2026-10-01"):
            self.assertGreaterEqual(
                repo.week_number(self.conn, date.fromisoformat(day)), 1
            )


class TopicLookupTestCase(Base):
    """The join that made this bug matter."""

    def test_the_right_topic_is_found_after_the_break(self) -> None:
        from bot import brief

        self.set_break()
        with database.transaction(self.conn):
            cur = self.conn.execute("INSERT INTO courses (code) VALUES ('CMDS 1630')")
            cid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO course_weeks (course_id, week_number, topic) "
                "VALUES (?, 6, 'Playing Professionally')",
                (cid,),
            )
            self.conn.execute(
                "INSERT INTO course_weeks (course_id, week_number, topic) "
                "VALUES (?, 7, 'Social Gaming')",
                (cid,),
            )

        from datetime import datetime

        context = brief.assemble(self.conn, now=datetime(2026, 10, 23, 7, 30))
        self.assertEqual(context.week_number, 6)
        self.assertIn(("CMDS 1630", "Playing Professionally"), context.week_topics)

    def test_the_brief_says_reading_week_instead_of_a_number(self) -> None:
        from datetime import datetime

        from bot import brief

        self.set_break()
        context = brief.assemble(self.conn, now=datetime(2026, 10, 14, 7, 30))
        facts = brief.render_facts(context)
        self.assertIn("READING WEEK", facts)
        self.assertNotIn("SEMESTER WEEK:", facts)


if __name__ == "__main__":
    unittest.main()
