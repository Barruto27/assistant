"""Resolving weekly instalments (from "Iclicker done in class").

That message used to be answered with four consecutive weeks of the same mark
and no question he could answer in one word:

    Which one - iClicker Participation (1/11) (CMDS 1630, due Fri Sep 18);
    ... (2/11) ...; (3/11) ...; (4/11) ...?

Weekly work is numbered and earned in order, so the row he means is the
earliest still open. What is genuinely unclear is the course.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo, router  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from bot.intents import ParsedIntent  # noqa: E402
from db import database  # noqa: E402
from fakes import ScriptedClassifier  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 11, 22, 0)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def series(self, course: str, base: str, count: int, start_day: int) -> list[int]:
        ids = []
        for n in range(1, count + 1):
            ids.append(
                repo.add_task(
                    self.conn,
                    title=f"{base} ({n}/{count})",
                    course=course,
                    due_date=f"2026-09-{start_day + (n - 1) * 7:02d}",
                )
            )
        return ids

    def route(self, fields: dict) -> str:
        return router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(name="update_task", fields=fields)),
            "iclicker done in class",
            now=NOW,
        )

    def status(self, task_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]


class OneSeriesTestCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ids = self.series("CMDS 1630", "iClicker Participation", 11, 18)

    def test_the_earliest_outstanding_one_is_taken(self) -> None:
        reply = self.route({"task_query": "iclicker", "status": "done"})
        self.assertNotIn("Which", reply)
        self.assertEqual(self.status(self.ids[0]), "done")
        self.assertEqual(self.status(self.ids[1]), "not_started")

    def test_the_next_report_takes_the_next_one(self) -> None:
        self.route({"task_query": "iclicker", "status": "done"})
        self.route({"task_query": "iclicker", "status": "done"})
        self.assertEqual(self.status(self.ids[0]), "done")
        self.assertEqual(self.status(self.ids[1]), "done")
        self.assertEqual(self.status(self.ids[2]), "not_started")

    def test_the_receipt_names_the_week_it_took(self) -> None:
        reply = self.route({"task_query": "iclicker", "status": "done"})
        self.assertIn("(1/11)", reply)


class TwoCoursesTestCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cmds = self.series("CMDS 1630", "iClicker Participation", 11, 18)
        self.psyc = self.series("PSYC 3265", "iClicker Participation", 9, 17)

    def test_the_question_is_about_the_course_not_the_week(self) -> None:
        reply = self.route({"task_query": "iclicker", "status": "done"})
        self.assertIn("Which course", reply)
        self.assertIn("CMDS 1630", reply)
        self.assertIn("PSYC 3265", reply)
        self.assertNotIn("(2/11)", reply)

    def test_nothing_is_written_while_it_is_still_ambiguous(self) -> None:
        self.route({"task_query": "iclicker", "status": "done"})
        statuses = {self.status(i) for i in self.cmds + self.psyc}
        self.assertEqual(statuses, {"not_started"})

    def test_naming_the_course_settles_it(self) -> None:
        reply = self.route(
            {"task_query": "iclicker", "course": "CMDS 1630", "status": "done"}
        )
        self.assertNotIn("Which", reply)
        self.assertEqual(self.status(self.cmds[0]), "done")
        self.assertEqual(self.status(self.psyc[0]), "not_started")


class AlreadyResolvedTestCase(Base):
    def test_a_row_already_done_is_not_a_candidate(self) -> None:
        ids = self.series("CMDS 1630", "iClicker Participation", 11, 18)
        repo.update_task(self.conn, ids[0], status="done")

        self.route({"task_query": "iclicker", "status": "done"})
        self.assertEqual(self.status(ids[1]), "done", "should take the next one")

    def test_a_finished_series_still_answers_rather_than_crashing(self) -> None:
        ids = self.series("CMDS 1630", "iClicker Participation", 3, 18)
        for task_id in ids:
            repo.update_task(self.conn, task_id, status="done")
        reply = self.route({"task_query": "iclicker", "status": "done"})
        self.assertTrue(reply.strip())


class UnrelatedTasksTestCase(Base):
    """The collapse must only apply to a genuine single series."""

    def test_different_work_is_still_disambiguated_by_name(self) -> None:
        repo.add_task(
            self.conn, title="A1: Soundscape", course="DATT 1200",
            due_date="2026-09-30",
        )
        repo.add_task(
            self.conn, title="A1: Soundscape draft", course="DATT 1200",
            due_date="2026-09-20",
        )
        reply = router.handle_message(
            self.conn,
            ScriptedClassifier(
                ParsedIntent(
                    name="update_task",
                    fields={"task_query": "soundscape", "status": "done"},
                )
            ),
            "finished the soundscape",
            now=NOW,
        )
        self.assertIn("Which one", reply)
        self.assertIn("Soundscape", reply)

    def test_a_single_match_is_untouched_by_any_of_this(self) -> None:
        task = repo.add_task(
            self.conn, title="Group charter", course="DATT 1200",
            due_date="2026-09-23",
        )
        reply = router.handle_message(
            self.conn,
            ScriptedClassifier(
                ParsedIntent(
                    name="update_task",
                    fields={"task_query": "charter", "status": "done"},
                )
            ),
            "signed the charter",
            now=NOW,
        )
        self.assertNotIn("Which", reply)
        self.assertEqual(self.status(task), "done")


if __name__ == "__main__":
    unittest.main()
