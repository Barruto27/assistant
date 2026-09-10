"""Syllabus extraction and ingest (plan Section 5)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import syllabus as syl  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from db import database  # noqa: E402
from fakes import FakeTextBlock, FakeToolUseBlock  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

PAYLOAD = {
    "course_code": "PSYC 3040",
    "course_name": "Cognition",
    "items": [
        {"title": "Test 1", "type": "test", "due_date": "2026-10-06", "weight_pct": 20},
        {
            "title": "Final exam",
            "type": "exam",
            "due_date": "2026-12-12",
            "weight_pct": 40,
            "tentative": True,
        },
        {"title": "Participation", "type": "other", "weight_pct": 10},
        {
            "title": "Essay",
            "type": "assignment",
            "due_date": "2026-11-03",
            "weight_pct": 30,
            "notes": "2000 words",
        },
    ],
    "weekly_topics": [
        {"week_number": 1, "topic": "Intro"},
        {"week_number": 2, "topic": "Memory"},
    ],
}


class FakeResponse:
    def __init__(self, blocks) -> None:
        self.content = blocks


class FakeAnthropic:
    """Stands in for the SDK client, recording what it was sent."""

    def __init__(self, blocks, *, error: BaseException | None = None) -> None:
        self._blocks = blocks
        self._error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeResponse(self._blocks)


class ParseTestCase(unittest.TestCase):
    def test_parses_a_full_payload(self) -> None:
        result = syl.parse_extraction(PAYLOAD)
        self.assertEqual(result.course_code, "PSYC 3040")
        self.assertEqual(result.course_name, "Cognition")
        self.assertEqual(len(result.items), 4)
        self.assertEqual(result.weekly_topics, [(1, "Intro"), (2, "Memory")])
        self.assertEqual(result.total_weight, 100)

    def test_tentative_survives_parsing(self) -> None:
        final = next(i for i in syl.parse_extraction(PAYLOAD).items if i.type == "exam")
        self.assertTrue(final.tentative)

    def test_items_without_a_title_are_dropped(self) -> None:
        result = syl.parse_extraction(
            {"course_code": "X 100", "items": [{"title": "  "}, {"title": "Real"}]}
        )
        self.assertEqual([i.title for i in result.items], ["Real"])

    def test_missing_course_code_is_an_error(self) -> None:
        with self.assertRaises(AssistantError) as ctx:
            syl.parse_extraction({"items": []})
        self.assertEqual(ctx.exception.code, E.UNPARSEABLE_DOCUMENT)

    def test_empty_strings_become_none(self) -> None:
        result = syl.parse_extraction(
            {"course_code": "X 100", "items": [{"title": "T", "due_date": "", "notes": ""}]}
        )
        self.assertIsNone(result.items[0].due_date)
        self.assertIsNone(result.items[0].notes)


class PriorityTestCase(unittest.TestCase):
    """Stakes decide priority, and priority decides what survives the backlog."""

    def test_exams_are_priority_one(self) -> None:
        self.assertEqual(
            syl.default_priority(syl.SyllabusItem("Final", type="exam")), 1
        )

    def test_heavy_weight_is_priority_one(self) -> None:
        self.assertEqual(
            syl.default_priority(syl.SyllabusItem("Essay", type="assignment", weight_pct=30)),
            1,
        )

    def test_ordinary_graded_work_is_priority_two(self) -> None:
        self.assertEqual(
            syl.default_priority(syl.SyllabusItem("Quiz", type="test", weight_pct=5)), 2
        )

    def test_readings_and_unweighted_are_priority_three(self) -> None:
        self.assertEqual(
            syl.default_priority(syl.SyllabusItem("Ch 3", type="reading")), 3
        )
        self.assertEqual(
            syl.default_priority(syl.SyllabusItem("Prep", weight_pct=0)), 3
        )


class ExtractTestCase(unittest.TestCase):
    def test_sends_the_pdf_and_parses_the_tool_call(self) -> None:
        client = FakeAnthropic(
            [FakeToolUseBlock(name="record_syllabus", input=PAYLOAD)]
        )
        result = syl.extract(b"%PDF-1.4 fake", client, "claude-sonnet-5")
        self.assertEqual(result.course_code, "PSYC 3040")

        sent = client.calls[0]
        document = sent["messages"][0]["content"][0]
        self.assertEqual(document["type"], "document")
        self.assertEqual(document["source"]["media_type"], "application/pdf")
        self.assertEqual(sent["tool_choice"]["name"], "record_syllabus")

    def test_empty_file_is_rejected_before_any_api_call(self) -> None:
        client = FakeAnthropic([])
        with self.assertRaises(AssistantError) as ctx:
            syl.extract(b"", client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.UNPARSEABLE_DOCUMENT)
        self.assertEqual(client.calls, [], "should not spend an API call on an empty file")

    def test_oversized_file_is_rejected_before_any_api_call(self) -> None:
        client = FakeAnthropic([])
        with self.assertRaises(AssistantError) as ctx:
            syl.extract(b"x" * (syl.MAX_PDF_BYTES + 1), client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.UNPARSEABLE_DOCUMENT)
        self.assertEqual(client.calls, [])

    def test_no_tool_call_is_reported_honestly(self) -> None:
        client = FakeAnthropic([FakeTextBlock(text="This looks like a menu.")])
        with self.assertRaises(AssistantError) as ctx:
            syl.extract(b"%PDF fake", client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.UNPARSEABLE_DOCUMENT)

    def test_api_failure_surfaces_as_a_claude_error(self) -> None:
        client = FakeAnthropic([], error=RuntimeError("connection reset"))
        with self.assertRaises(AssistantError) as ctx:
            syl.extract(b"%PDF fake", client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.CLAUDE)


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)
        self.syllabus = syl.parse_extraction(PAYLOAD)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_writes_course_tasks_and_topics(self) -> None:
        counts = syl.ingest(self.conn, self.syllabus)
        self.assertEqual(counts, {"tasks": 4, "topics": 2, "replaced": 0})

        course = self.conn.execute("SELECT * FROM courses").fetchone()
        self.assertEqual(course["code"], "PSYC 3040")
        self.assertEqual(course["name"], "Cognition")

        tasks = self.conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
        self.assertEqual(len(tasks), 4)
        for task in tasks:
            self.assertEqual(task["course"], "PSYC 3040", "every task must be tagged")
            self.assertEqual(task["source"], "syllabus")

    def test_priorities_reflect_stakes(self) -> None:
        syl.ingest(self.conn, self.syllabus)
        by_title = {
            row["title"]: row["priority"]
            for row in self.conn.execute("SELECT title, priority FROM tasks")
        }
        self.assertEqual(by_title["Final exam"], 1)
        self.assertEqual(by_title["Essay"], 1)  # 30% of the grade
        self.assertEqual(by_title["Test 1"], 1)  # 20% hits the threshold
        self.assertEqual(by_title["Participation"], 2)

    def test_tentative_flag_reaches_the_database(self) -> None:
        syl.ingest(self.conn, self.syllabus)
        row = self.conn.execute(
            "SELECT tentative FROM tasks WHERE title = 'Final exam'"
        ).fetchone()
        self.assertEqual(row["tentative"], 1)

    def test_reimport_replaces_rather_than_duplicates(self) -> None:
        syl.ingest(self.conn, self.syllabus)
        counts = syl.ingest(self.conn, self.syllabus)
        self.assertEqual(counts["replaced"], 4)
        total = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(total, 4, "re-import must not duplicate")

    def test_reimport_leaves_kaans_own_tasks_alone(self) -> None:
        from bot import repository as repo

        syl.ingest(self.conn, self.syllabus)
        repo.add_task(self.conn, title="Reread ch 4", course="PSYC 3040")
        syl.ingest(self.conn, self.syllabus)

        manual = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE source = 'text'"
        ).fetchone()["n"]
        self.assertEqual(manual, 1, "a re-import must not undo his own edits")

    def test_topics_upsert_on_reimport(self) -> None:
        syl.ingest(self.conn, self.syllabus)
        changed = syl.parse_extraction(
            {**PAYLOAD, "weekly_topics": [{"week_number": 2, "topic": "Working memory"}]}
        )
        syl.ingest(self.conn, changed)
        topics = self.conn.execute(
            "SELECT week_number, topic FROM course_weeks ORDER BY week_number"
        ).fetchall()
        self.assertEqual(
            [(r["week_number"], r["topic"]) for r in topics],
            [(1, "Intro"), (2, "Working memory")],
        )


class ReceiptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.syllabus = syl.parse_extraction(PAYLOAD)

    def test_receipt_lists_dates_weights_and_tentative(self) -> None:
        text = syl.receipt(self.syllabus, {"tasks": 4, "topics": 2, "replaced": 0})
        self.assertIn("PSYC 3040 — Cognition", text)
        self.assertIn("2026-10-06", text)
        self.assertIn("40%", text)
        self.assertIn("tentative", text)
        self.assertIn("no date · Participation", text)
        self.assertIn("100% of the grade accounted for", text)

    def test_flags_a_weight_total_that_misses_100(self) -> None:
        partial = syl.parse_extraction(
            {
                "course_code": "X 100",
                "items": [{"title": "Midterm", "weight_pct": 30}],
            }
        )
        text = syl.receipt(partial, {"tasks": 1, "topics": 0, "replaced": 0})
        self.assertIn("check I didn't miss anything", text)

    def test_does_not_flag_a_complete_total(self) -> None:
        text = syl.receipt(self.syllabus, {"tasks": 4, "topics": 2, "replaced": 0})
        self.assertNotIn("check I didn't miss anything", text)

    def test_mentions_replacements(self) -> None:
        text = syl.receipt(self.syllabus, {"tasks": 4, "topics": 2, "replaced": 4})
        self.assertIn("Replaced 4 earlier items", text)


if __name__ == "__main__":
    unittest.main()
