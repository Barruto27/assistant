"""Asking about email (plan Section 6).

At 17:56 Kaan sent "Email summa rt" and then "Any important emails". Both
classified as just_chat and ran against 58 tasks and 3 courses, because the
query path has no email in it and no email is ever saved. So the bot said it
had nothing, twice, while the mailbox was connected and working - only the
07:30 brief could see it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import router  # noqa: E402
from bot.email_reader import FlaggedEmail  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from bot.intents import CHECK_EMAIL, INTENT_TOOLS, ParsedIntent  # noqa: E402
from db import database  # noqa: E402
from fakes import ScriptedClassifier  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 11, 17, 56)

DEADLINE = FlaggedEmail(
    kind="deadline_change",
    summary="A1 moved a week later",
    course="DATT 1200",
    new_date="2026-10-07",
)
CLARIFICATION = FlaggedEmail(
    kind="announcement",
    summary="the Week 1 version sheet is not submitted for marks",
    course="DATT 1200",
)
PSYC_NOTE = FlaggedEmail(
    kind="announcement", summary="lecture moved to Vari Hall B", course="PSYC 3265"
)


def lookup_of(flagged, scanned):
    return lambda: (list(flagged), scanned)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def ask(self, lookup, fields: dict | None = None) -> str:
        return router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(name=CHECK_EMAIL, fields=fields or {})),
            "any important emails",
            now=NOW,
            email_lookup=lookup,
        )


class ReadsTheMailboxTestCase(Base):
    def test_flagged_mail_comes_back(self) -> None:
        reply = self.ask(lookup_of([DEADLINE, CLARIFICATION], 9))
        self.assertIn("A1 moved a week later", reply)
        self.assertIn("version sheet", reply)
        self.assertIn("DATT 1200", reply)

    def test_a_new_date_is_shown_readably(self) -> None:
        reply = self.ask(lookup_of([DEADLINE], 9))
        self.assertIn("Oct 7", reply)
        self.assertNotIn("2026-10-07", reply)

    def test_it_says_nothing_was_saved(self) -> None:
        """Section 6: he confirms before anything becomes a task."""
        reply = self.ask(lookup_of([DEADLINE], 9))
        self.assertIn("nothing saved", reply.lower())

    def test_nothing_flagged_still_reports_that_it_looked(self) -> None:
        reply = self.ask(lookup_of([], 9))
        self.assertIn("9", reply)
        self.assertNotIn("can't read", reply)

    def test_an_empty_window_says_so(self) -> None:
        reply = self.ask(lookup_of([], 0))
        self.assertIn("No course mail", reply)

    def test_one_course_can_be_asked_about_alone(self) -> None:
        reply = self.ask(
            lookup_of([DEADLINE, PSYC_NOTE], 9), {"course": "PSYC 3265"}
        )
        self.assertIn("Vari Hall B", reply)
        self.assertNotIn("A1 moved", reply)

    def test_a_course_with_nothing_in_it_says_so_by_name(self) -> None:
        reply = self.ask(lookup_of([DEADLINE], 9), {"course": "PSYC 3265"})
        self.assertIn("PSYC 3265", reply)
        self.assertNotIn("A1 moved", reply)


class FailureTestCase(Base):
    def test_no_mailbox_configured_says_what_is_missing(self) -> None:
        reply = self.ask(None)
        self.assertIn("GMAIL_IMAP_USER", reply)

    def test_a_failed_read_is_not_reported_as_an_empty_inbox(self) -> None:
        """"Nothing for you" and "I couldn't look" must not be the same reply."""
        reply = self.ask(lookup_of([], None))
        self.assertIn("couldn't get into the mailbox", reply)
        self.assertNotIn("Nothing", reply)


class WiringTestCase(unittest.TestCase):
    def test_the_tool_warns_against_answering_from_saved_data(self) -> None:
        """The mistake that made this necessary."""
        tool = next(t for t in INTENT_TOOLS if t["name"] == CHECK_EMAIL)
        self.assertIn("answer_query", tool["description"])
        self.assertIn("live", tool["description"])

    def test_it_has_a_handler(self) -> None:
        self.assertIn(CHECK_EMAIL, router.HANDLERS)

    def test_only_one_mailbox_read_per_message(self) -> None:
        """It is a network call plus a model call; two would double the wait."""
        from bot.claude_client import PROSE_INTENTS

        self.assertIn(CHECK_EMAIL, PROSE_INTENTS)

    def test_the_command_and_the_intent_share_one_implementation(self) -> None:
        self.assertTrue(callable(router.run_email_check))
        direct = router.run_email_check(lookup_of([DEADLINE], 9))
        self.assertIn("A1 moved a week later", direct)


if __name__ == "__main__":
    unittest.main()
