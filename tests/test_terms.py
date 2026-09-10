"""Reading the academic calendar out of Google Calendar (plan Section 3)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo, term_dates  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from bot.google_calendar import CalendarEvent  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


def allday(name: str, start: str, days: int = 1) -> CalendarEvent:
    """An all-day event as Google returns it: end date exclusive."""
    s = date.fromisoformat(start)
    return CalendarEvent(name, s, s + timedelta(days=days), True, recurring=False)


#: Kaan's actual entries, verbatim including the trailing space.
REAL = [
    allday("classes start", "2026-09-09"),
    allday("LAST DAY TO ADD A COURSE WITHOUT PERMISSION", "2026-09-22"),
    allday("READING WEEK", "2026-10-10", 7),
    allday("LAST DAY TO DROP A COURSE WITHOUT RECEIVING A GRADE", "2026-11-10"),
    allday("FALL CLASSES END ", "2026-12-08"),
    allday("FALL STUDY DAY", "2026-12-09"),
    allday("FALL EXAM DAYS", "2026-12-10", 14),
    allday("WINTER BREAK", "2026-12-24", 11),
    allday("WINTER CLASSES START", "2027-01-04"),
    allday("WINTER READING WEEK", "2027-02-13", 7),
]


class FindTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.found = {
            f.key: f.value
            for f in term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026)
        }

    def test_finds_every_date(self) -> None:
        self.assertEqual(
            self.found,
            {
                "semester_start_date": "2026-09-09",
                "semester_end_date": "2026-12-08",
                "reading_week_start": "2026-10-10",
                "reading_week_end": "2026-10-16",
                "exam_period_start": "2026-12-10",
                "exam_period_end": "2026-12-23",
            },
        )

    def test_all_day_end_dates_are_made_inclusive(self) -> None:
        """Google's end is exclusive; storing it raw adds a phantom day."""
        self.assertEqual(self.found["reading_week_end"], "2026-10-16")
        self.assertEqual(self.found["exam_period_end"], "2026-12-23")

    def test_the_winter_reading_week_is_not_mistaken_for_the_fall_one(self) -> None:
        self.assertEqual(self.found["reading_week_start"], "2026-10-10")

    def test_trailing_whitespace_in_a_title_still_matches(self) -> None:
        self.assertEqual(self.found["semester_end_date"], "2026-12-08")

    def test_recurring_events_are_ignored(self) -> None:
        """A weekly lecture is not an academic date."""
        lecture = CalendarEvent(
            "Reading Week Prep", date(2026, 9, 1), date(2026, 9, 2), True, recurring=True
        )
        found = {f.key: f.value for f in term_dates.find([lecture], today=date(2026, 9, 10), term_year=2026)}
        self.assertEqual(found, {})

    def test_timed_events_are_ignored(self) -> None:
        timed = CalendarEvent(
            "classes start",
            datetime(2026, 9, 9, 9, 0),
            datetime(2026, 9, 9, 10, 0),
            False,
            recurring=False,
        )
        self.assertEqual(term_dates.find([timed], today=date(2026, 9, 10), term_year=2026), [])

    def test_an_empty_calendar_finds_nothing(self) -> None:
        self.assertEqual(term_dates.find([], today=date(2026, 9, 10), term_year=2026), [])

    def test_render_says_what_was_missing(self) -> None:
        partial = term_dates.find(
            [allday("classes start", "2026-09-09")], today=date(2026, 9, 10), term_year=2026
        )
        text = term_dates.render(partial, partial, today=date(2026, 9, 10))
        self.assertIn("Couldn't find:", text)
        self.assertIn("Exams", text)

    def test_render_reads_as_prose_not_config_keys(self) -> None:
        text = term_dates.render(
            term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026), [],
            today=date(2026, 9, 10))
        self.assertIn("Classes", text)
        self.assertIn("Reading week", text)
        self.assertNotIn("semester_start_date", text)

    def test_render_names_the_source_entry(self) -> None:
        text = term_dates.render(term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026), [],
            today=date(2026, 9, 10))
        self.assertIn("Dec 10", text)

    def test_render_with_nothing_found_explains_what_it_looks_for(self) -> None:
        self.assertIn("all-day events", term_dates.render([], [], today=date(2026, 9, 10)))


class ApplyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_dates_are_stored(self) -> None:
        term_dates.apply(
            self.conn, term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026)
        )
        self.assertEqual(
            database.get_config(self.conn, "reading_week_end"), "2026-10-16"
        )

    def test_rerunning_reports_nothing_changed(self) -> None:
        found = term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026)
        self.assertTrue(term_dates.apply(self.conn, found))
        self.assertEqual(term_dates.apply(self.conn, found), [], "idempotent")

    def test_the_stored_dates_make_the_syllabus_weeks_correct(self) -> None:
        """The point of loading them at all."""
        term_dates.apply(
            self.conn, term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026)
        )
        syllabus = {
            1: "2026-09-11", 5: "2026-10-09", 6: "2026-10-23",
            9: "2026-11-13", 12: "2026-12-04",
        }
        for week, day in syllabus.items():
            with self.subTest(week=week):
                self.assertEqual(
                    repo.week_number(self.conn, date.fromisoformat(day)), week
                )

    def test_reading_week_is_detected_from_the_stored_dates(self) -> None:
        term_dates.apply(
            self.conn, term_dates.find(REAL, today=date(2026, 9, 10), term_year=2026)
        )
        self.assertTrue(repo.in_reading_week(self.conn, date(2026, 10, 14)))
        self.assertFalse(repo.in_reading_week(self.conn, date(2026, 10, 17)))


class TwoTermCalendarTestCase(unittest.TestCase):
    """A full academic year holds two "classes start" entries.

    Run against the live calendar, taking the first match in iteration order
    picked the previous January's winter term and reported today as week 36.
    """

    CALENDAR = [
        allday("WINTER CLASSES START", "2026-01-05"),
        allday("WINTER READING WEEK", "2026-02-14", 7),
        allday("WINTER CLASSES END", "2026-04-06"),
        allday("classes start", "2026-09-09"),
        allday("READING WEEK", "2026-10-10", 7),
        allday("FALL CLASSES END ", "2026-12-08"),
        allday("FALL EXAM DAYS", "2026-12-10", 14),
        allday("WINTER CLASSES START", "2027-01-04"),
        allday("WINTER READING WEEK", "2027-02-13", 7),
    ]

    def found(self, today: str) -> dict[str, str]:
        return {
            f.key: f.value
            for f in term_dates.find(
                self.CALENDAR, today=date.fromisoformat(today), term_year=2026
            )
        }

    def test_mid_fall_picks_the_fall_term(self) -> None:
        found = self.found("2026-09-10")
        self.assertEqual(found["semester_start_date"], "2026-09-09")
        self.assertEqual(found["semester_end_date"], "2026-12-08")
        self.assertEqual(found["reading_week_start"], "2026-10-10")

    def test_a_start_takes_the_most_recent_one_already_passed(self) -> None:
        """Not the earliest in the file, and not next January's."""
        self.assertEqual(
            self.found("2026-11-20")["semester_start_date"], "2026-09-09"
        )

    def test_an_end_takes_the_next_one_still_to_come(self) -> None:
        self.assertEqual(self.found("2026-09-10")["semester_end_date"], "2026-12-08")

    def test_after_the_fall_term_it_moves_on(self) -> None:
        """In January the winter term is the current one."""
        self.assertEqual(
            self.found("2027-01-10")["semester_start_date"], "2027-01-04"
        )

    def test_before_any_term_starts_it_looks_forward(self) -> None:
        found = self.found("2025-12-20")
        self.assertEqual(found["semester_start_date"], "2026-01-05")

    def test_the_january_entry_never_wins_during_the_fall(self) -> None:
        for day in ("2026-09-09", "2026-10-14", "2026-12-01"):
            with self.subTest(day=day):
                self.assertNotEqual(
                    self.found(day)["semester_start_date"], "2026-01-05"
                )


class DeadlineTestCase(unittest.TestCase):
    """Enrolment deadlines become priority 1 tasks.

    They are dates rather than work, but missing one has consequences no effort
    afterwards undoes — which is what priority 1 is for, since the backlog rule
    exempts it and they stay visible even once past.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        self.deadlines = term_dates.find_deadlines(
            REAL, today=date(2026, 9, 10), term_year=2026
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_finds_the_fall_deadlines(self) -> None:
        found = {d.title: d.due for d in self.deadlines}
        self.assertEqual(found["Last day to add a course"], "2026-09-22")
        self.assertEqual(
            found["Last day to drop a course (no grade)"], "2026-11-10"
        )

    def test_they_become_priority_one_tasks(self) -> None:
        term_dates.apply_deadlines(self.conn, self.deadlines)
        rows = self.conn.execute(
            "SELECT title, due_date, priority, source FROM tasks ORDER BY due_date"
        ).fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["priority"], 1)
            self.assertEqual(row["source"], "seed")

    def test_rerunning_does_not_duplicate(self) -> None:
        term_dates.apply_deadlines(self.conn, self.deadlines)
        before = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(term_dates.apply_deadlines(self.conn, self.deadlines), [])
        after = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(before, after)

    def test_a_moved_deadline_updates_in_place(self) -> None:
        term_dates.apply_deadlines(self.conn, self.deadlines)
        moved = [
            term_dates.Deadline(d.title, "2026-11-17", d.source)
            if "drop" in d.title else d
            for d in self.deadlines
        ]
        changed = term_dates.apply_deadlines(self.conn, moved)
        self.assertEqual(len(changed), 1)
        row = self.conn.execute(
            "SELECT due_date FROM tasks WHERE title LIKE '%drop%'"
        ).fetchone()
        self.assertEqual(row["due_date"], "2026-11-17")

    def test_a_passed_deadline_survives_backlog_triage(self) -> None:
        """Priority 1 is exempt, so a missed drop deadline stays visible."""
        from bot import backlog

        term_dates.apply_deadlines(self.conn, self.deadlines)
        self.assertEqual(backlog.demote(self.conn, date(2026, 12, 31)), [])

    def test_render_counts_down_to_each(self) -> None:
        text = term_dates.render(
            [], [], self.deadlines, today=date(2026, 9, 10)
        )
        self.assertIn("in 12 days", text)
        self.assertIn("Last day to add a course", text)

    def test_render_says_passed_for_old_ones(self) -> None:
        text = term_dates.render([], [], self.deadlines, today=date(2026, 12, 31))
        self.assertIn("passed", text)


if __name__ == "__main__":
    unittest.main()
