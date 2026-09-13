"""The morning brief's opening quotation (plan Section 7).

Kaan's verdict on the original opener - "Some weeks you just show up and let
the syllabus tell you what kind of semester it's going to be" - was that it was
not motivating. The instruction was the cause: it asked for a line in the
spirit of a famous quotation and then forbade quoting one, which leaves nothing
to aim at. Two rewrites made it concrete but no better, because on a quiet day
a line derived from his tasks has nothing to say and just restates the date.

He chose real attributed quotations. The thing that would ruin them is
repetition, so most of this file is about never sending the same one twice.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import brief, quotes  # noqa: E402
from bot import repository as repo  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 12, 7, 30)


class FakeCaller:
    """Returns queued payloads from call_tool, recording the prompts."""

    def __init__(self, *payloads, error: BaseException | None = None) -> None:
        self._queue = list(payloads)
        self._error = error
        self.systems: list[str] = []

    def call_tool(self, system, user, tool, *, max_tokens=1024):
        self.systems.append(system)
        if self._error:
            raise self._error
        return self._queue.pop(0) if self._queue else {}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def context(self) -> brief.BriefContext:
        return brief.assemble(self.conn, now=NOW)


class PickTestCase(Base):
    LANGER = {
        "text": "The pain of discipline is far less than the pain of regret",
        "author": "Justin Langer",
        "why": "two deadlines land together on Thursday",
    }

    def test_it_returns_and_records_the_quote(self) -> None:
        quote = quotes.pick(self.conn, FakeCaller(self.LANGER), self.context())
        self.assertEqual(quote.author, "Justin Langer")
        self.assertIn("pain of discipline", quote.text)
        self.assertEqual(len(quotes.recent(self.conn)), 1)

    def test_the_line_is_quoted_and_attributed(self) -> None:
        quote = quotes.pick(self.conn, FakeCaller(self.LANGER), self.context())
        self.assertTrue(quote.line().startswith('"'))
        self.assertIn("— Justin Langer", quote.line())

    def test_surrounding_quote_marks_are_stripped(self) -> None:
        """The model adds them about half the time."""
        payload = dict(self.LANGER, text='"' + self.LANGER["text"] + '"')
        quote = quotes.pick(self.conn, FakeCaller(payload), self.context())
        self.assertFalse(quote.text.startswith('"'))
        self.assertEqual(quote.line().count('"'), 2)

    def test_curly_quote_marks_are_stripped_too(self) -> None:
        payload = dict(self.LANGER, text="“" + self.LANGER["text"] + "”")
        quote = quotes.pick(self.conn, FakeCaller(payload), self.context())
        self.assertFalse(quote.text.startswith("“"))


class NeverRepeatsTestCase(Base):
    A = {"text": "Man is not worried by real problems so much as by his "
                 "imagined anxieties about real problems",
         "author": "Epictetus", "why": "week one nerves"}

    def test_a_repeat_is_dropped_rather_than_sent(self) -> None:
        first = quotes.pick(self.conn, FakeCaller(self.A), self.context())
        self.assertIsNotNone(first)
        again = quotes.pick(self.conn, FakeCaller(self.A), self.context())
        self.assertIsNone(again, "a repeat must not be sent")

    def test_punctuation_and_case_do_not_make_it_new(self) -> None:
        quotes.pick(self.conn, FakeCaller(self.A), self.context())
        reworded = dict(self.A, text=self.A["text"].upper() + "!!!")
        self.assertIsNone(
            quotes.pick(self.conn, FakeCaller(reworded), self.context())
        )

    def test_what_was_used_is_shown_to_the_picker(self) -> None:
        quotes.pick(self.conn, FakeCaller(self.A), self.context())
        caller = FakeCaller({"text": "Something else", "author": "Someone", "why": "x"})
        quotes.pick(self.conn, caller, self.context())
        self.assertIn("Epictetus", caller.systems[0])

    def test_the_first_morning_has_an_empty_list(self) -> None:
        caller = FakeCaller(self.A)
        quotes.pick(self.conn, caller, self.context())
        self.assertIn("nothing yet", caller.systems[0])


class FailsQuietlyTestCase(Base):
    """A missing opener is a small loss; a brief that did not send is not."""

    def test_a_failed_call_returns_none(self) -> None:
        caller = FakeCaller(error=AssistantError(E.CLAUDE, "down"))
        self.assertIsNone(quotes.pick(self.conn, caller, self.context()))

    def test_an_empty_payload_returns_none(self) -> None:
        self.assertIsNone(quotes.pick(self.conn, FakeCaller({}), self.context()))

    def test_a_quote_with_no_author_is_refused(self) -> None:
        payload = {"text": "Something wise", "author": "", "why": "x"}
        self.assertIsNone(quotes.pick(self.conn, FakeCaller(payload), self.context()))

    def test_nothing_is_recorded_when_the_pick_fails(self) -> None:
        quotes.pick(self.conn, FakeCaller({}), self.context())
        self.assertEqual(quotes.recent(self.conn), [])


class DayShapeTestCase(Base):
    """The picker is told what kind of day it is, so it is not choosing blind."""

    def test_a_quiet_day_says_so(self) -> None:
        self.assertIn("quiet", quotes.describe_day(self.context()))

    def test_an_attendance_day_asks_for_showing_up(self) -> None:
        repo.add_task(
            self.conn, title="iClicker Participation (1/9)", course="PSYC 3265",
            due_date="2026-09-12",
        )
        with database.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET attendance = 1")
        shape = quotes.describe_day(brief.assemble(self.conn, now=NOW))
        self.assertIn("being there", shape)

    def test_work_due_today_says_so(self) -> None:
        repo.add_task(
            self.conn, title="Essay", course="DATT 1200", due_date="2026-09-12"
        )
        self.assertIn("due today", quotes.describe_day(brief.assemble(self.conn, now=NOW)))


class RenderingTestCase(Base):
    QUOTE = quotes.Quote(text="A line worth reading", author="Someone Real")

    def test_the_facts_block_carries_it_for_claude(self) -> None:
        context = self.context()
        context.quote = self.QUOTE
        facts = brief.render_facts(context)
        self.assertIn("OPENING QUOTATION", facts)
        self.assertIn("A line worth reading", facts)

    def test_the_fallback_brief_shows_the_quote_not_the_instruction(self) -> None:
        """render_plain runs when Claude is down, so a label for Claude leaks."""
        context = self.context()
        context.quote = self.QUOTE
        plain = brief.render_plain(context)
        self.assertIn("A line worth reading", plain)
        self.assertNotIn("OPENING QUOTATION", plain)
        self.assertNotIn("verbatim", plain)

    def test_the_quote_leads_the_fallback(self) -> None:
        context = self.context()
        context.quote = self.QUOTE
        self.assertTrue(brief.render_plain(context).startswith('"A line worth reading"'))

    def test_no_quote_means_no_block_anywhere(self) -> None:
        context = self.context()
        self.assertNotIn("OPENING QUOTATION", brief.render_facts(context))
        self.assertNotIn("OPENING QUOTATION", brief.render_plain(context))

    def test_the_prompt_forbids_inventing_one(self) -> None:
        self.assertIn("Never invent one", brief.BRIEF_SYSTEM)
        self.assertIn("start with the date instead", brief.BRIEF_SYSTEM)


if __name__ == "__main__":
    unittest.main()
