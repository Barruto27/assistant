"""Attendance marks vs deadlines (from the first real brief).

The 07:30 brief listed iClicker participation as "due today" beside a written
reflection. An iClicker mark is not something you submit — it is a mark for
being in the lecture. Called a deadline it is useless and mildly stressful.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import brief, repository as repo, syllabus as syl  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 10, 7, 30)
TODAY = "2026-09-10"


class SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_migration_0003_applied(self) -> None:
        columns = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        self.assertIn("attendance", columns)

    def test_defaults_to_not_attendance(self) -> None:
        repo.add_task(self.conn, title="Essay", due_date=TODAY)
        row = self.conn.execute("SELECT attendance FROM tasks").fetchone()
        self.assertEqual(row["attendance"], 0)


class BriefSeparationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

        payload = {
            "course_code": "PSYC 3265",
            "items": [
                {"title": "iClicker", "weight_pct": 9, "attendance": True,
                 "occurrences": [TODAY]},
                {"title": "Weekly Reflection", "weight_pct": 9,
                 "occurrences": [TODAY]},
            ],
        }
        syl.ingest(self.conn, syl.parse_extraction(payload))
        self.context = brief.assemble(self.conn, now=NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_attendance_is_not_in_due_today(self) -> None:
        titles = [r["title"] for r in self.context.due_today]
        self.assertTrue(any("Reflection" in t for t in titles))
        self.assertFalse(
            any("iClicker" in t for t in titles),
            "an iClicker mark has nothing to submit; it is not a deadline",
        )

    def test_attendance_has_its_own_bucket(self) -> None:
        titles = [r["title"] for r in self.context.attendance_today]
        self.assertTrue(any("iClicker" in t for t in titles))

    def test_facts_never_call_attendance_due(self) -> None:
        facts = brief.render_facts(self.context)
        self.assertIn("MARKS FOR TURNING UP TODAY", facts)
        due_section = facts.split("DUE TODAY:")[1] if "DUE TODAY:" in facts else ""
        self.assertNotIn("iClicker", due_section)

    def test_prompt_says_not_to_call_it_due(self) -> None:
        flattened = " ".join(brief.BRIEF_SYSTEM.lower().split())
        self.assertIn("nothing to hand in", flattened)
        self.assertIn("reason to go", flattened)

    def test_attendance_excluded_from_upcoming_and_overdue(self) -> None:
        repo.add_task(self.conn, title="Old iClicker", due_date="2026-09-01",
                      priority=1, course="PSYC 3265")
        self.conn.execute("UPDATE tasks SET attendance = 1 WHERE title = 'Old iClicker'")
        context = brief.assemble(self.conn, now=NOW)
        self.assertFalse(
            any("Old iClicker" in r["title"] for r in context.overdue_urgent),
            "a missed lecture cannot be made up, so nagging about it is pointless",
        )


class ExtractionTestCase(unittest.TestCase):
    def test_attendance_flag_survives_parsing(self) -> None:
        parsed = syl.parse_extraction({
            "course_code": "PSYC 3265",
            "items": [
                {"title": "iClicker", "attendance": True},
                {"title": "Reflection", "attendance": False},
                {"title": "Essay"},
            ],
        })
        self.assertEqual([i.attendance for i in parsed.items], [True, False, False])

    def test_flag_survives_recurring_expansion(self) -> None:
        item = syl.SyllabusItem(
            title="iClicker", weight_pct=9, attendance=True,
            occurrences=["2026-09-10", "2026-09-17"],
        )
        rows = syl.expand(item)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.attendance for r in rows))

    def test_prompt_distinguishes_work_from_showing_up(self) -> None:
        flattened = " ".join(syl.SYSTEM.lower().split())
        self.assertIn("distinguish work from attendance", flattened)


class CourseCodeTestCase(unittest.TestCase):
    """A caption saying "cmds1630" must land on the syllabus's "CMDS 1630"."""

    def test_run_together_codes_get_their_space(self) -> None:
        self.assertEqual(syl.normalize_course_code("CMDS1630"), "CMDS 1630")
        self.assertEqual(syl.normalize_course_code("cmds1630"), "CMDS 1630")
        self.assertEqual(syl.normalize_course_code("DATT1200"), "DATT 1200")

    def test_section_letter_glued_to_the_number(self) -> None:
        self.assertEqual(syl.normalize_course_code("PSYC3265A"), "PSYC 3265")

    def test_a_bare_department_code_is_left_alone(self) -> None:
        """The glued-section rule must not eat the last letter of "PSYC"."""
        self.assertEqual(syl.normalize_course_code("PSYC"), "PSYC")

    def test_already_canonical_codes_are_stable(self) -> None:
        for code in ("CMDS 1630", "DATT 1200", "PSYC 3265", "NATS 1505"):
            self.assertEqual(syl.normalize_course_code(code), code)


if __name__ == "__main__":
    unittest.main()
