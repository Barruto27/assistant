"""Morning brief assembly and fallback (plan Section 7)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import brief, repository as repo  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from bot.google_calendar import CalendarEvent  # noqa: E402
from bot.weather import Forecast  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 14, 7, 30)  # Monday, week 2 of the semester
TODAY = NOW.date().isoformat()
TOMORROW = "2026-09-15"


class StubWriter:
    def __init__(self, result: str | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def compose(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.calls.append((system, user))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class BriefAssemblyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-07")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def assemble(self) -> brief.BriefContext:
        return brief.assemble(self.conn, now=NOW)

    def test_week_number_and_empty_day(self) -> None:
        context = self.assemble()
        self.assertEqual(context.week_number, 2)
        self.assertEqual(context.due_today, [])
        self.assertIsNone(context.forecast, "no coordinates means no weather call")
        self.assertEqual(context.unavailable, [])

    def test_buckets_tasks_by_due_date(self) -> None:
        repo.add_task(self.conn, title="Quiz", course="PSYC 3040", due_date=TODAY)
        repo.add_task(self.conn, title="Lab", course="BIOL 1000", due_date=TOMORROW)
        repo.add_task(self.conn, title="Essay", course="ENGL 1000", due_date="2026-09-18")
        repo.add_task(self.conn, title="Far off", due_date="2026-12-01")

        context = self.assemble()
        self.assertEqual([r["title"] for r in context.due_today], ["Quiz"])
        self.assertEqual([r["title"] for r in context.due_tomorrow], ["Lab"])
        self.assertEqual([r["title"] for r in context.upcoming], ["Essay"])

    def test_only_priority_one_survives_overdue(self) -> None:
        repo.add_task(self.conn, title="Old exam", due_date="2026-09-01", priority=1)
        repo.add_task(self.conn, title="Old reading", due_date="2026-09-01", priority=3)
        context = self.assemble()
        self.assertEqual([r["title"] for r in context.overdue_urgent], ["Old exam"])

    def test_done_and_stale_tasks_are_excluded(self) -> None:
        for status in ("done", "stale", "archived"):
            task_id = repo.add_task(self.conn, title=f"{status} task", due_date=TODAY)
            repo.update_task(self.conn, task_id, status=status)
        repo.add_task(self.conn, title="live task", due_date=TODAY)

        context = self.assemble()
        self.assertEqual([r["title"] for r in context.due_today], ["live task"])

    def test_week_topics_match_the_current_week(self) -> None:
        with database.transaction(self.conn):
            cur = self.conn.execute("INSERT INTO courses (code) VALUES ('PSYC 3040')")
            self.conn.execute(
                "INSERT INTO course_weeks (course_id, week_number, topic) VALUES (?, 2, 'Memory')",
                (cur.lastrowid,),
            )
            self.conn.execute(
                "INSERT INTO course_weeks (course_id, week_number, topic) VALUES (?, 3, 'Attention')",
                (cur.lastrowid,),
            )
        self.assertEqual(self.assemble().week_topics, [("PSYC 3040", "Memory")])

    def test_carried_over_reminders_are_included(self) -> None:
        repo.add_reminder(self.conn, text="email the TA", fire_at="2026-09-14 06:00:00")
        repo.add_reminder(self.conn, text="much later", fire_at="2026-09-20 06:00:00")
        self.assertEqual(
            [r["text"] for r in self.assemble().reminders], ["email the TA"]
        )


class RenderingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-07")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_sections_are_omitted(self) -> None:
        facts = brief.render_facts(brief.assemble(self.conn, now=NOW))
        self.assertIn("DATE: Monday, September 14, 2026", facts)
        self.assertIn("SEMESTER WEEK: 2", facts)
        for absent in ("GOALS", "DUE TODAY", "WEATHER", "GYM TODAY"):
            self.assertNotIn(absent, facts)

    def test_facts_include_what_reveals_a_misparse(self) -> None:
        repo.add_task(
            self.conn,
            title="Midterm",
            course="PSYC 3040",
            due_date=TODAY,
            weight_pct=25,
            priority=1,
            tentative=True,
        )
        facts = brief.render_facts(brief.assemble(self.conn, now=NOW))
        self.assertIn("Midterm", facts)
        self.assertIn("PSYC 3040", facts)
        self.assertIn("25% of grade", facts)
        self.assertIn("date tentative", facts)

    def test_lectures_are_excluded_from_the_week_summary(self) -> None:
        context = brief.assemble(self.conn, now=NOW)
        # A later day, not NOW: today's events have their own section and are
        # deliberately excluded from the week list.
        later = datetime(2026, 9, 17, 9, 0)
        context.week_events = [
            CalendarEvent("PSYC lecture", later, None, False, recurring=True),
            CalendarEvent("Dentist", later, None, False, recurring=False),
        ]
        facts = brief.render_facts(context)
        week_section = facts.split("THIS WEEK, EXCLUDING LECTURES:")[1]
        self.assertIn("Dentist", week_section)
        self.assertNotIn("PSYC lecture", week_section)

    def test_week_events_carry_their_date(self) -> None:
        """A bare clock time in a seven-day list reads as "today".

        This actually happened: next Monday's appointment was rendered as
        "12:15 Appointment", and the brief placed it that afternoon.
        """
        context = brief.assemble(self.conn, now=NOW)
        context.week_events = [
            CalendarEvent("Appointment with atse", datetime(2026, 9, 21, 12, 15),
                          None, False, recurring=False),
        ]
        facts = brief.render_facts(context)
        self.assertIn("Mon Sep 21 12:15 Appointment with atse", facts)

    def test_today_is_not_repeated_in_the_week_section(self) -> None:
        """Today already has its own section; listing it twice invites confusion."""
        context = brief.assemble(self.conn, now=NOW)
        today_event = CalendarEvent("Dentist", datetime(2026, 9, 14, 9, 0),
                                    None, False, recurring=False)
        later = CalendarEvent("Haircut", datetime(2026, 9, 17, 9, 0),
                              None, False, recurring=False)
        context.today_events = [today_event]
        context.week_events = [today_event, later]

        facts = brief.render_facts(context)
        week_section = facts.split("THIS WEEK, EXCLUDING LECTURES:")[1]
        self.assertIn("Haircut", week_section)
        self.assertNotIn("Dentist", week_section)
        self.assertIn("Dentist", facts.split("TODAY'S CALENDAR:")[1].split("THIS WEEK")[0])

    def test_all_day_week_events_also_carry_a_date(self) -> None:
        from datetime import date as _date

        context = brief.assemble(self.conn, now=NOW)
        context.week_events = [
            CalendarEvent("Reading week", _date(2026, 9, 18), None, True, recurring=False),
        ]
        self.assertIn("Fri Sep 18 all day Reading week", brief.render_facts(context))

    def test_unavailable_subsystems_are_named(self) -> None:
        context = brief.assemble(self.conn, now=NOW)
        context.unavailable = ["calendar"]
        self.assertIn("COULD NOT REACH: calendar", brief.render_facts(context))

    def test_weather_mentions_feels_like_only_when_it_differs(self) -> None:
        mild = Forecast(20, 12, 21, 13, 15)
        self.assertNotIn("feels like", mild.summary())
        harsh = Forecast(-5, -12, -14, -20, 30)
        self.assertIn("feels like", harsh.summary())

    def test_plain_brief_leads_with_the_date_and_week(self) -> None:
        plain = brief.render_plain(brief.assemble(self.conn, now=NOW))
        self.assertTrue(plain.startswith("Monday, September 14"))
        self.assertIn("week 2", plain)


class GenerateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)
        self.context = brief.assemble(self.conn, now=NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_uses_claude_when_it_works(self) -> None:
        writer = StubWriter("Here is your day.")
        self.assertEqual(brief.generate(self.context, writer), "Here is your day.")
        system, _ = writer.calls[0]
        # The prompt wraps across lines, so compare on normalised whitespace.
        flattened = " ".join(system.lower().split())
        self.assertIn("never invent a task", flattened)

    def test_falls_back_when_claude_fails(self) -> None:
        writer = StubWriter(AssistantError(E.CLAUDE, "down"))
        result = brief.generate(self.context, writer)
        self.assertIn("Monday, September 14", result)

    def test_falls_back_on_an_empty_response(self) -> None:
        result = brief.generate(self.context, StubWriter("   "))
        self.assertIn("Monday, September 14", result)

    def test_no_writer_at_all_still_produces_a_brief(self) -> None:
        self.assertIn("Monday, September 14", brief.generate(self.context, None))


if __name__ == "__main__":
    unittest.main()
