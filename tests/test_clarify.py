"""Remembering a question long enough to understand the answer.

    > remind me to email my prof about the quiz
    < When should I remind you - tonight, tomorrow, or a specific time?
    > tomorrow at 10
    < What do you want me to remind you about?

Each message was classified alone, so the answer to the bot's own question
arrived with no idea what had been asked, and it asked the other half. Nothing
was saved either time and the exchange could have gone round forever.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import clarify, router  # noqa: E402
from bot import repository as repo  # noqa: E402
from bot.claude_client import PromptContext  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from bot.intents import ParsedIntent  # noqa: E402
from db import database  # noqa: E402
from fakes import ScriptedClassifier  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 12, 23, 0)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def ask_about_a_reminder(self) -> None:
        clarify.remember(
            self.conn,
            intent="add_reminder",
            fields={"text": "buy milk"},
            question="When should I remind you?",
            now=NOW,
        )


class StorageTestCase(Base):
    def test_nothing_pending_to_begin_with(self) -> None:
        self.assertIsNone(clarify.pending(self.conn, NOW))

    def test_what_was_asked_comes_back(self) -> None:
        self.ask_about_a_reminder()
        open_q = clarify.pending(self.conn, NOW)
        self.assertEqual(open_q.intent, "add_reminder")
        self.assertEqual(open_q.fields["text"], "buy milk")
        self.assertIn("When", open_q.question)

    def test_it_goes_stale(self) -> None:
        """A question from two hours ago must not reinterpret a new message."""
        self.ask_about_a_reminder()
        later = NOW + timedelta(minutes=clarify.WINDOW_MINUTES + 1)
        self.assertIsNone(clarify.pending(self.conn, later))

    def test_it_survives_within_the_window(self) -> None:
        self.ask_about_a_reminder()
        soon = NOW + timedelta(minutes=clarify.WINDOW_MINUTES - 1)
        self.assertIsNotNone(clarify.pending(self.conn, soon))

    def test_clearing_works(self) -> None:
        self.ask_about_a_reminder()
        clarify.clear(self.conn)
        self.assertIsNone(clarify.pending(self.conn, NOW))

    def test_unreadable_state_is_ignored_not_fatal(self) -> None:
        database.set_config(self.conn, "pending_clarification", "{not json")
        database.set_config(
            self.conn, "pending_clarification_at", NOW.strftime(clarify.TS)
        )
        self.assertIsNone(clarify.pending(self.conn, NOW))


class MergeTestCase(Base):
    def test_the_earlier_fields_come_back(self) -> None:
        self.ask_about_a_reminder()
        open_q = clarify.pending(self.conn, NOW)
        merged = clarify.merge(open_q, "add_reminder", {"fire_at": "2026-09-13 10:00:00"})
        self.assertEqual(merged["text"], "buy milk")
        self.assertEqual(merged["fire_at"], "2026-09-13 10:00:00")

    def test_the_new_answer_wins(self) -> None:
        self.ask_about_a_reminder()
        open_q = clarify.pending(self.conn, NOW)
        merged = clarify.merge(open_q, "add_reminder", {"text": "buy bread"})
        self.assertEqual(merged["text"], "buy bread")

    def test_a_different_intent_does_not_inherit(self) -> None:
        """If he changed the subject, the half-built fields are not his."""
        self.ask_about_a_reminder()
        open_q = clarify.pending(self.conn, NOW)
        merged = clarify.merge(open_q, "add_task", {"title": "Essay"})
        self.assertNotIn("text", merged)

    def test_empty_values_do_not_overwrite(self) -> None:
        self.ask_about_a_reminder()
        open_q = clarify.pending(self.conn, NOW)
        merged = clarify.merge(open_q, "add_reminder", {"text": ""})
        self.assertEqual(merged["text"], "buy milk")

    def test_no_pending_question_changes_nothing(self) -> None:
        self.assertEqual(clarify.merge(None, "add_reminder", {"a": 1}), {"a": 1})


class RoundTripTestCase(Base):
    """The whole exchange, through the router."""

    def route(self, intent: ParsedIntent, message: str = "m") -> str:
        return router.handle_message(
            self.conn, ScriptedClassifier(intent), message, now=NOW
        )

    def test_the_answer_completes_the_reminder(self) -> None:
        first = self.route(
            ParsedIntent(name="add_reminder", fields={"text": "buy milk"}),
            "remind me to buy milk",
        )
        self.assertIn("when should i remind you", first.lower())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"], 0
        )

        second = self.route(
            ParsedIntent(name="add_reminder", fields={"fire_at": "2026-09-13 18:00:00"}),
            "6pm tomorrow",
        )
        self.assertIn("Reminder set", second)
        row = self.conn.execute("SELECT text, fire_at FROM reminders").fetchone()
        self.assertEqual(row["text"], "buy milk")
        self.assertEqual(row["fire_at"], "2026-09-13 18:00:00")

    def test_the_question_closes_once_answered(self) -> None:
        self.route(ParsedIntent(name="add_reminder", fields={"text": "buy milk"}))
        self.route(
            ParsedIntent(name="add_reminder", fields={"fire_at": "2026-09-13 18:00:00"})
        )
        self.assertIsNone(clarify.pending(self.conn, NOW))

    def test_changing_the_subject_abandons_it(self) -> None:
        self.route(ParsedIntent(name="add_reminder", fields={"text": "buy milk"}))
        self.route(
            ParsedIntent(
                name="add_task", fields={"title": "Essay", "due_date": "2026-09-18"}
            )
        )
        self.assertIsNone(clarify.pending(self.conn, NOW))
        row = self.conn.execute("SELECT title FROM tasks").fetchone()
        self.assertEqual(row["title"], "Essay")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"], 0
        )

    def test_asking_again_replaces_the_question(self) -> None:
        self.route(ParsedIntent(name="add_reminder", fields={"text": "buy milk"}))
        self.route(ParsedIntent(name="add_reminder", fields={"text": "buy bread"}))
        open_q = clarify.pending(self.conn, NOW)
        self.assertEqual(open_q.fields["text"], "buy bread")


class PromptTestCase(Base):
    def test_the_parser_is_told_what_was_asked(self) -> None:
        self.ask_about_a_reminder()
        context = router.build_context(self.conn, NOW)
        rendered = context.render()
        self.assertIn("When should I remind you?", rendered)
        self.assertIn("buy milk", rendered)
        self.assertIn("add_reminder", rendered)

    def test_nothing_is_said_when_nothing_is_pending(self) -> None:
        rendered = router.build_context(self.conn, NOW).render()
        self.assertNotIn("You just asked him", rendered)

    def test_it_is_told_it_may_be_ignored(self) -> None:
        self.ask_about_a_reminder()
        rendered = router.build_context(self.conn, NOW).render()
        self.assertIn("moved on to something else", rendered)


if __name__ == "__main__":
    unittest.main()
