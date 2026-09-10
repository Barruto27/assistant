"""Answering from the database, and only from it (plan Section 4).

The property under test throughout: the bot knows what it was told and nothing
else. A confident wrong answer about a deadline is worse than no answer,
because it gets acted on.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import query, repository as repo, syllabus as syl  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 10, 9, 0)


class RecordingWriter:
    """Captures the prompt so the tests can assert on what the model was given."""

    def __init__(self, reply: str = "an answer") -> None:
        self.reply = reply
        self.system = ""
        self.user = ""

    def compose(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.system, self.user = system, user
        return self.reply


class GatherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_database_says_nothing_is_known(self) -> None:
        rendered = query.render(query.gather(self.conn, "what's due?", now=NOW))
        self.assertIn("COURSES ON FILE: none", rendered)
        self.assertIn("COURSEWORK ON FILE: none", rendered)

    def test_courses_are_listed_as_the_limit_of_knowledge(self) -> None:
        syl.ingest(self.conn, syl.parse_extraction(
            {"course_code": "PSYC 3265", "items": [{"title": "Test 1", "weight_pct": 20}]}))
        rendered = query.render(query.gather(self.conn, "?", now=NOW))
        self.assertIn("any course not listed here is unknown", rendered)
        self.assertIn("PSYC 3265", rendered)

    def test_archived_and_stale_work_is_out_of_view(self) -> None:
        for status in ("archived", "stale"):
            tid = repo.add_task(self.conn, title=f"{status} item", due_date="2026-09-11")
            repo.update_task(self.conn, tid, status=status)
        repo.add_task(self.conn, title="live item", due_date="2026-09-11")
        titles = [r["title"] for r in query.gather(self.conn, "?", now=NOW).tasks]
        self.assertEqual(titles, ["live item"])

    def test_completed_work_stays_visible_so_did_i_finish_can_be_answered(self) -> None:
        tid = repo.add_task(self.conn, title="Essay", due_date="2026-09-11")
        repo.update_task(self.conn, tid, status="done")
        rendered = query.render(query.gather(self.conn, "did I finish the essay?", now=NOW))
        self.assertIn("Essay", rendered)
        self.assertIn("done", rendered)

    def test_context_is_bounded_and_says_when_it_truncates(self) -> None:
        for i in range(12):
            repo.add_task(self.conn, title=f"Task {i}", due_date="2026-09-11")
        context = query.gather(self.conn, "?", now=NOW, limit=5)
        self.assertEqual(len(context.tasks), 5)
        self.assertTrue(context.tasks_truncated)
        self.assertIn("cut off at the row limit", query.render(context))

    def test_no_truncation_notice_when_everything_fits(self) -> None:
        repo.add_task(self.conn, title="Only task", due_date="2026-09-11")
        context = query.gather(self.conn, "?", now=NOW)
        self.assertFalse(context.tasks_truncated)
        self.assertNotIn("cut off at the row limit", query.render(context))

    def test_distant_work_is_still_known(self) -> None:
        """A 21-day window made the bot deny a December essay it holds.

        A confident false negative is as misleading as a guess, and just as
        likely to be acted on.
        """
        repo.add_task(self.conn, title="Final Essay", course="CMDS 1630",
                      due_date="2026-12-08", weight_pct=35)
        rendered = query.render(query.gather(self.conn, "final essay worth?", now=NOW))
        self.assertIn("Final Essay", rendered)
        self.assertIn("35% of grade", rendered)

    def test_undated_work_is_still_known(self) -> None:
        repo.add_task(self.conn, title="Concept Brief", course="CMDS 1630", weight_pct=15)
        rendered = query.render(query.gather(self.conn, "concept brief?", now=NOW))
        self.assertIn("Concept Brief", rendered)
        self.assertIn("no due date", rendered)

    def test_tentative_and_attendance_are_marked_in_the_data(self) -> None:
        repo.add_task(self.conn, title="Midterm", due_date="2026-09-15", tentative=True)
        tid = repo.add_task(self.conn, title="iClicker", due_date="2026-09-15")
        self.conn.execute("UPDATE tasks SET attendance = 1 WHERE id = ?", (tid,))
        rendered = query.render(query.gather(self.conn, "?", now=NOW))
        self.assertIn("DATE TENTATIVE", rendered)
        self.assertIn("attendance mark, nothing to submit", rendered)


class VerificationNotesTestCase(unittest.TestCase):
    """What the extraction was unsure about must travel with the answer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        syl.ingest(self.conn, syl.parse_extraction({
            "course_code": "CMDS 1630",
            "items": [{"title": "Research Report", "due_date": "2026-10-23", "weight_pct": 20}],
            "uncertainties": [
                "Check-in due day is ambiguous: prose says Thursday, the table shows Friday",
            ],
        }))

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_notes_are_stored_on_the_course(self) -> None:
        row = self.conn.execute("SELECT verify_notes FROM courses").fetchone()
        self.assertIn("ambiguous", row["verify_notes"])

    def test_notes_reach_the_prompt_with_an_instruction(self) -> None:
        rendered = query.render(query.gather(self.conn, "when is the check-in?", now=NOW))
        self.assertIn("UNVERIFIED", rendered)
        self.assertIn("check the syllabus", rendered)
        self.assertIn("CMDS 1630: Check-in due day is ambiguous", rendered)

    def test_a_clean_import_adds_no_unverified_section(self) -> None:
        conn2 = database.connect(Path(self._tmp.name) / "clean.sqlite3")
        database.migrate(conn2)
        syl.ingest(conn2, syl.parse_extraction(
            {"course_code": "PSYC 3265", "items": [{"title": "Test 1"}]}))
        self.assertNotIn("UNVERIFIED", query.render(query.gather(conn2, "?", now=NOW)))
        conn2.close()

    def test_reimport_refreshes_rather_than_accumulates(self) -> None:
        syl.ingest(self.conn, syl.parse_extraction({
            "course_code": "CMDS 1630",
            "items": [{"title": "Research Report", "due_date": "2026-10-23"}],
            "uncertainties": ["Only the final essay weight was unclear"],
        }))
        notes = self.conn.execute("SELECT verify_notes FROM courses").fetchone()["verify_notes"]
        self.assertIn("final essay weight", notes)
        self.assertNotIn("ambiguous", notes, "stale uncertainties must not linger")


class AnswerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_prompt_forbids_inventing_answers(self) -> None:
        writer = RecordingWriter()
        query.answer(self.conn, "what's due this week?", writer, now=NOW)
        flat = " ".join(writer.system.lower().split())
        self.assertIn("answer only from that data", flat)
        self.assertIn("never estimate a weight, a date, or a deadline", flat)
        self.assertIn("worse than no answer", flat)

    def test_the_question_is_what_gets_asked(self) -> None:
        writer = RecordingWriter()
        query.answer(self.conn, "how much is the final worth?", writer, now=NOW)
        self.assertEqual(writer.user, "how much is the final worth?")

    def test_data_travels_in_the_system_prompt(self) -> None:
        repo.add_task(self.conn, title="Term Paper", course="PSYC 3265",
                      due_date="2026-09-15", weight_pct=25)
        writer = RecordingWriter()
        query.answer(self.conn, "what's coming up?", writer, now=NOW)
        self.assertIn("Term Paper", writer.system)
        self.assertIn("25% of grade", writer.system)

    def test_empty_reply_is_an_error_not_silence(self) -> None:
        with self.assertRaises(AssistantError) as ctx:
            query.answer(self.conn, "anything?", RecordingWriter("   "), now=NOW)
        self.assertEqual(ctx.exception.code, E.CLAUDE)

    def test_returns_the_models_answer(self) -> None:
        writer = RecordingWriter("Two things: the essay Friday and a quiz Monday.")
        self.assertEqual(
            query.answer(self.conn, "what's due?", writer, now=NOW),
            "Two things: the essay Friday and a quiz Monday.",
        )


if __name__ == "__main__":
    unittest.main()
