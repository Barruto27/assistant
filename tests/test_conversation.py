"""Failures found reading a real evening of messages (Sep 10-11).

Three of them, in the order they compounded:

1.  "Remind me today and tomorrow that I need to upload my accessibility
    documents" set one reminder. The classifier was told to call exactly one
    tool, so the second half of the sentence went nowhere and nothing said so.

2.  The check-in closed on its first answer. "I chose not to do the GED study"
    cleared it, so a minute later "Iclicker done in class" no longer had a
    check-in to attach to.

3.  With no check-in open, that message fell to the conversational path -
    which reads and cannot write - and the reply came back: "that's PSYC 3265
    (1/9) and CMDS 1630 (1/11) both marked as attendance for today." Neither
    was. Nothing in the database had status 'done' at all.

The third is the one worth the file. A bot that quietly does nothing is a
nuisance; a bot that says it recorded something and didn't is worse than no bot
at all, because he stops checking.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import checkin, query, router  # noqa: E402
from bot import repository as repo  # noqa: E402
from bot.claude_client import SYSTEM_PROMPT  # noqa: E402
from bot.errors import AssistantError, setup_logging  # noqa: E402
from bot.intents import ParsedIntent  # noqa: E402
from db import database  # noqa: E402
from fakes import ScriptedClassifier  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 10, 22, 15)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class MultipleInstructionsTestCase(Base):
    """One message, more than one thing asked for."""

    def route(self, intents: list[ParsedIntent], message: str = "m") -> str:
        return router.handle_message(
            self.conn, ScriptedClassifier(intents), message, now=NOW
        )

    def test_two_reminders_from_one_message(self) -> None:
        reply = self.route([
            ParsedIntent(name="add_reminder", fields={
                "text": "Upload the accessibility documents",
                "fire_at": "2026-09-10 19:00:00",
            }),
            ParsedIntent(name="add_reminder", fields={
                "text": "Upload the accessibility documents",
                "fire_at": "2026-09-11 09:00:00",
            }),
        ], "Remind me today and tomorrow to upload my accessibility documents")

        rows = self.conn.execute(
            "SELECT fire_at FROM reminders ORDER BY fire_at"
        ).fetchall()
        self.assertEqual(
            [r["fire_at"] for r in rows],
            ["2026-09-10 19:00:00", "2026-09-11 09:00:00"],
        )
        # Both have to show in the receipt, or he cannot tell one was dropped.
        self.assertEqual(len(reply.strip().splitlines()), 2)

    def test_a_task_and_a_reminder_together(self) -> None:
        self.route([
            ParsedIntent(name="add_task", fields={
                "title": "Essay draft", "due_date": "2026-09-18",
            }),
            ParsedIntent(name="add_reminder", fields={
                "text": "Start the essay draft", "fire_at": "2026-09-10 20:00:00",
            }),
        ])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"], 1
        )

    def test_one_failure_does_not_discard_the_other(self) -> None:
        reply = self.route([
            ParsedIntent(name="add_task", fields={
                "title": "Essay draft", "due_date": "2026-09-18",
            }),
            # No text, so the reminder handler rejects it.
            ParsedIntent(name="add_reminder", fields={"fire_at": "2026-09-11 09:00:00"}),
        ])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"],
            1,
            "the good half still has to land",
        )
        self.assertIn("Essay draft", reply)
        self.assertEqual(len(reply.strip().splitlines()), 2, "the failure is reported")

    def test_everything_failing_raises(self) -> None:
        with self.assertRaises(AssistantError):
            self.route([
                ParsedIntent(name="add_reminder", fields={}),
                ParsedIntent(name="add_reminder", fields={}),
            ])

    def test_the_prompt_asks_for_one_call_per_instruction(self) -> None:
        """The rule that caused this said the opposite."""
        rendered = SYSTEM_PROMPT.format(voice="", context="")
        self.assertNotIn("Call exactly one tool", rendered)
        self.assertNotIn("One message can only be one intent", rendered)
        self.assertIn("more than one instruction", rendered)


class CheckinStaysOpenTestCase(Base):
    """The check-in has to survive being answered in pieces."""

    def setUp(self) -> None:
        super().setUp()
        self.reflection = repo.add_task(
            self.conn, title="Weekly Reflections (1/9)", course="PSYC 3265",
            due_date="2026-09-10",
        )
        self.iclicker = repo.add_task(
            self.conn, title="iClicker Participation (1/9)", course="PSYC 3265",
            due_date="2026-09-10",
        )
        checkin.mark_sent(self.conn, NOW, [self.reflection, self.iclicker])

    def test_answering_one_leaves_the_other_offered(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=NOW)
        still_open = checkin.pending(self.conn, NOW + timedelta(minutes=1))
        self.assertEqual(still_open, {self.iclicker})

    def test_answering_everything_closes_it(self) -> None:
        checkin.resolve(self.conn, {self.reflection, self.iclicker}, now=NOW)
        self.assertIsNone(checkin.pending(self.conn, NOW + timedelta(minutes=1)))

    def test_an_answer_matching_nothing_leaves_it_all_offered(self) -> None:
        """"I skipped the GED study" is about a reminder, not either task."""
        checkin.resolve(self.conn, set(), now=NOW)
        self.assertEqual(
            checkin.pending(self.conn, NOW + timedelta(minutes=1)),
            {self.reflection, self.iclicker},
        )

    def test_the_window_still_expires(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=NOW)
        later = NOW + timedelta(hours=checkin.REPLY_WINDOW_HOURS + 1)
        self.assertIsNone(checkin.pending(self.conn, later))


class NoFalseConfirmationTestCase(Base):
    """Never say something was recorded unless it was."""

    def test_the_read_only_prompt_forbids_claiming_a_write(self) -> None:
        context = query.gather(self.conn, "iclicker done in class", now=NOW)
        system = query.SYSTEM.format(voice="", data=query.render(context))
        self.assertIn("You are reading, not writing", system)
        self.assertIn("marked done", system)

    def test_the_conversational_prompt_carries_the_same_rule(self) -> None:
        self.assertIn("without claiming to have", query.CONVERSATIONAL)

    def test_a_checkin_reply_matching_nothing_says_so(self) -> None:
        """It used to answer "Recorded." having written nothing at all."""
        task = repo.add_task(
            self.conn, title="Weekly Reflections (1/9)", course="PSYC 3265",
            due_date="2026-09-10",
        )
        checkin.mark_sent(self.conn, NOW, [task])

        class NoMatchWriter:
            """Stands in for Claude declining to guess which row he meant."""

            def call_tool(self, system, user, tool, *, max_tokens=1024):
                return {"done": [], "started": [], "not_done": [], "attended": []}

            def compose(self, system, user, *, max_tokens=1024):
                raise AssertionError("compose is not part of this path")

        reply = router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(
                name="checkin_reply",
                fields={"summary": "I skipped the GED study, it was too long"},
            )),
            "I skipped the GED study, it was too long",
            now=NOW,
            writer=NoMatchWriter(),
        )
        self.assertNotIn("Recorded", reply)
        self.assertIn("haven't changed anything", reply)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task,)
            ).fetchone()["status"],
            "not_started",
        )

    def test_nothing_answered_keeps_the_checkin_open(self) -> None:
        """So the next message is still read as an answer to it."""
        task = repo.add_task(self.conn, title="Reflection", due_date="2026-09-10")
        checkin.mark_sent(self.conn, NOW, [task])
        checkin.resolve(self.conn, set(), now=NOW)
        self.assertEqual(
            checkin.pending(self.conn, NOW + timedelta(minutes=1)), {task}
        )


class ReportingCompletionTestCase(Base):
    """"Iclicker done in class" is an instruction, not small talk."""

    def test_the_prompt_routes_a_completion_report_to_update_task(self) -> None:
        rendered = SYSTEM_PROMPT.format(voice="", context="")
        self.assertIn("iclicker done in class", rendered.lower())
        self.assertIn("update_task", rendered)


if __name__ == "__main__":
    unittest.main()
