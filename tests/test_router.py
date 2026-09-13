"""Message pipeline: classification, dispatch, receipts (plan Section 4)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo  # noqa: E402
from bot import router  # noqa: E402
from bot.claude_client import (  # noqa: E402
    MAX_INTENTS,
    REPLY_TIMEOUT_SECONDS,
    AnthropicClient,
    PromptContext,
    _explain,
    parse_tool_uses,
)
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from bot.intents import INTENT_NAMES, ParsedIntent  # noqa: E402
from db import database  # noqa: E402
from fakes import (  # noqa: E402
    ExplodingClassifier,
    FakeTextBlock,
    FakeToolUseBlock,
    ScriptedClassifier,
)

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 5, 14, 30)  # a Saturday


class RouterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "test.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-07")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def route(self, intent: ParsedIntent, message: str = "test message") -> str:
        return router.handle_message(
            self.conn, ScriptedClassifier(intent), message, now=NOW
        )

    # -- wiring -------------------------------------------------------------

    def test_every_intent_has_a_handler(self) -> None:
        self.assertEqual(set(router.HANDLERS), set(INTENT_NAMES))

    def test_unknown_intent_name_fails_loudly(self) -> None:
        with self.assertRaises(AssistantError) as ctx:
            self.route(ParsedIntent(name="teleport"))
        self.assertEqual(ctx.exception.code, E.INTENT_UNCLEAR)

    def test_classifier_failure_propagates(self) -> None:
        boom = AssistantError(E.CLAUDE, "Couldn't reach Claude to read that.")
        with self.assertRaises(AssistantError) as ctx:
            router.handle_message(self.conn, ExplodingClassifier(boom), "hi", now=NOW)
        self.assertEqual(ctx.exception.code, E.CLAUDE)

    # -- context ------------------------------------------------------------

    def test_context_carries_courses_and_week_number(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute("INSERT INTO courses (code) VALUES ('PSYC 3040')")
            self.conn.execute("INSERT INTO courses (code) VALUES ('MATH 1021')")

        classifier = ScriptedClassifier(ParsedIntent(name="just_chat"))
        router.handle_message(self.conn, classifier, "hey", now=datetime(2026, 9, 14))
        _, context = classifier.calls[0]

        self.assertEqual(context.courses, ["MATH 1021", "PSYC 3040"])
        self.assertEqual(context.week_number, 2)  # semester starts 2026-09-07
        rendered = context.render()
        self.assertIn("PSYC 3040", rendered)
        self.assertIn("week 2", rendered)

    def test_week_number_is_none_before_semester_starts(self) -> None:
        self.assertIsNone(repo.week_number(self.conn, datetime(2026, 8, 1).date()))

    # -- add_task: the Section 4 definition of done -------------------------

    def test_add_task_saves_and_receipts(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="add_task",
                fields={
                    "title": "Test 2",
                    "type": "test",
                    "course": "PSYC 3040",
                    "due_date": "2026-04-13",
                    "weight_pct": 20,
                    "priority": 1,
                    "notes": "ch 3-5",
                },
            ),
            "test April 13, psyc 3040, ch 3-5",
        )

        self.assertEqual(
            reply, "Saved — Test 2, PSYC 3040, due Mon Apr 13, 20%, P1, ch 3-5"
        )
        row = self.conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["title"], "Test 2")
        self.assertEqual(row["course"], "PSYC 3040")
        self.assertEqual(row["due_date"], "2026-04-13")
        self.assertEqual(row["weight_pct"], 20)
        self.assertEqual(row["priority"], 1)
        self.assertEqual(row["source"], "text")

    def test_add_task_flags_a_missing_course(self) -> None:
        reply = self.route(
            ParsedIntent(name="add_task", fields={"title": "Read chapter 4"})
        )
        self.assertIn("no course", reply)

    def test_add_task_marks_tentative_dates(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="add_task",
                fields={"title": "Midterm", "due_date": "2026-10-20", "tentative": True},
            )
        )
        self.assertIn("(tentative)", reply)
        row = self.conn.execute("SELECT tentative FROM tasks").fetchone()
        self.assertEqual(row["tentative"], 1)

    def test_add_task_without_a_title_asks_for_one(self) -> None:
        """A question, not an error code. Nothing has gone wrong here."""
        reply = self.route(ParsedIntent(name="add_task", fields={"course": "PSYC 3040"}))
        self.assertIn("call it", reply.lower())
        self.assertNotIn("E103", reply)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"], 0
        )

    def test_add_task_stamps_the_current_week(self) -> None:
        router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(name="add_task", fields={"title": "Essay"})),
            "essay",
            now=datetime(2026, 9, 14),
        )
        row = self.conn.execute("SELECT week_number FROM tasks").fetchone()
        self.assertEqual(row["week_number"], 2)

    # -- reminders ----------------------------------------------------------

    def test_add_reminder_saves_absolute_time(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="add_reminder",
                fields={"text": "email prof", "fire_at": "2026-09-05 16:00:00"},
            )
        )
        self.assertEqual(reply, "Reminder set — Sat Sep 5 at 16:00: email prof")
        row = self.conn.execute("SELECT * FROM reminders").fetchone()
        self.assertEqual(row["sent"], 0)

    def test_anchor_resolves_from_todays_schedule(self) -> None:
        """The parser once returned the anchor "after Memory class ends at
        21:00" — naming the answer while refusing to state it. Resolving here
        means the reminder gets set regardless."""
        reply = router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(
                name="add_reminder",
                fields={"text": "email the TA", "anchor": "after my next class"},
            )),
            "remind me after my next class to email the TA",
            now=NOW,
            upcoming_events=[("18:00-21:00", "Memory (PSYC 3265)")],
        )
        self.assertIn("21:15", reply)
        self.assertIn("after Memory (PSYC 3265)", reply)
        row = self.conn.execute("SELECT fire_at FROM reminders").fetchone()
        self.assertEqual(row["fire_at"], "2026-09-05 21:15:00")

    def test_anchor_uses_the_next_event_not_a_later_one(self) -> None:
        router.handle_message(
            self.conn,
            ScriptedClassifier(ParsedIntent(
                name="add_reminder",
                fields={"text": "stretch", "anchor": "after my next class"},
            )),
            "x",
            now=NOW,
            upcoming_events=[("16:00-17:00", "Lab"), ("18:00-21:00", "Memory")],
        )
        row = self.conn.execute("SELECT fire_at FROM reminders").fetchone()
        self.assertEqual(row["fire_at"], "2026-09-05 17:15:00")

    def test_unresolvable_anchor_says_why(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="add_reminder",
                fields={"text": "stretch", "anchor": "after my next class"},
            )
        )
        # Today's schedule is given to the parser now, so reaching the anchor
        # path means the calendar genuinely didn't settle it.
        self.assertIn("Nothing left on today's calendar", reply)
        self.assertNotIn("E103", reply)

    def test_reminder_without_a_time_asks(self) -> None:
        """"remind me to buy milk" used to answer "... (E103)"."""
        reply = self.route(ParsedIntent(name="add_reminder", fields={"text": "stretch"}))
        self.assertIn("when should i remind you", reply.lower())
        self.assertNotIn("E103", reply)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"], 0
        )

    def test_due_reminders_respects_sent_flag(self) -> None:
        repo.add_reminder(self.conn, text="a", fire_at="2026-09-05 10:00:00")
        repo.add_reminder(self.conn, text="b", fire_at="2026-09-05 23:00:00")
        due = repo.due_reminders(self.conn, NOW)
        self.assertEqual([r["text"] for r in due], ["a"])

        repo.mark_reminder_sent(self.conn, due[0]["id"])
        self.assertEqual(repo.due_reminders(self.conn, NOW), [])

    # -- update_task --------------------------------------------------------

    def test_update_task_single_match(self) -> None:
        repo.add_task(self.conn, title="Essay draft", course="ENGL 1000")
        reply = self.route(
            ParsedIntent(
                name="update_task", fields={"task_query": "essay", "status": "done"}
            )
        )
        self.assertEqual(reply, "Essay draft — done.")
        row = self.conn.execute("SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")

    def test_update_task_no_match(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="update_task", fields={"task_query": "lab report", "status": "done"}
            )
        )
        self.assertIn("Nothing on file", reply)

    def test_update_task_ambiguous_lists_the_options(self) -> None:
        repo.add_task(self.conn, title="Assignment 3", course="PSYC 3040")
        repo.add_task(self.conn, title="Assignment 3", course="MATH 1021")
        reply = self.route(
            ParsedIntent(
                name="update_task",
                fields={"task_query": "assignment 3", "status": "done"},
            )
        )
        self.assertIn("Which one", reply)
        self.assertIn("PSYC 3040", reply)
        self.assertIn("MATH 1021", reply)
        # Nothing was written while ambiguous.
        statuses = {
            row["status"]
            for row in self.conn.execute("SELECT status FROM tasks").fetchall()
        }
        self.assertEqual(statuses, {"not_started"})

    def test_course_disambiguates_a_duplicate_title(self) -> None:
        repo.add_task(self.conn, title="Assignment 3", course="PSYC 3040")
        repo.add_task(self.conn, title="Assignment 3", course="MATH 1021")
        reply = self.route(
            ParsedIntent(
                name="update_task",
                fields={
                    "task_query": "assignment 3",
                    "course": "MATH 1021",
                    "status": "done",
                },
            )
        )
        self.assertEqual(reply, "Assignment 3 — done.")
        done = self.conn.execute(
            "SELECT course FROM tasks WHERE status = 'done'"
        ).fetchall()
        self.assertEqual([r["course"] for r in done], ["MATH 1021"])

    def test_update_task_with_nothing_to_change(self) -> None:
        repo.add_task(self.conn, title="Essay draft")
        reply = self.route(
            ParsedIntent(name="update_task", fields={"task_query": "essay"})
        )
        self.assertIn("what should i change", reply.lower())
        self.assertIn("Essay draft", reply)
        self.assertNotIn("E103", reply)

    def test_archived_tasks_are_not_matched(self) -> None:
        task_id = repo.add_task(self.conn, title="Old reading")
        repo.update_task(self.conn, task_id, status="archived")
        reply = self.route(
            ParsedIntent(
                name="update_task", fields={"task_query": "old reading", "status": "done"}
            )
        )
        self.assertIn("Nothing on file", reply)

    def test_matches_across_title_and_course(self) -> None:
        """"finished the psyc test" must find "Test" in PSYC 3040."""
        repo.add_task(self.conn, title="Test", course="PSYC 3040")
        reply = self.route(
            ParsedIntent(
                name="update_task",
                fields={"task_query": "PSYC test", "status": "done"},
            )
        )
        self.assertEqual(reply, "Test — done.")

    def test_stopwords_do_not_block_a_match(self) -> None:
        repo.add_task(self.conn, title="Essay draft", course="ENGL 1000")
        matches = repo.find_tasks(self.conn, "the essay for my draft")
        self.assertEqual(len(matches), 1)

    def test_every_word_must_match(self) -> None:
        repo.add_task(self.conn, title="Test", course="PSYC 3040")
        # 'midterm' appears in neither column, so this is not a match.
        self.assertEqual(repo.find_tasks(self.conn, "PSYC midterm"), [])

    def test_all_stopword_query_falls_back_to_raw_string(self) -> None:
        repo.add_task(self.conn, title="A", course="PSYC 3040")
        self.assertEqual(len(repo.find_tasks(self.conn, "a")), 1)

    # -- gym, goals, notes --------------------------------------------------

    def test_set_gym_split_upserts(self) -> None:
        self.assertEqual(
            self.route(
                ParsedIntent(
                    name="set_gym_split",
                    fields={"day_of_week": 2, "split_name": "Legs"},
                )
            ),
            "Wednesday is Legs.",
        )
        self.route(
            ParsedIntent(
                name="set_gym_split", fields={"day_of_week": 2, "split_name": "Rest"}
            )
        )
        self.assertEqual(repo.gym_split_for(self.conn, 2), "Rest")

    def test_set_goal_computes_expiry(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="set_goal", fields={"text": "run 3 times", "tier": "weekly"}
            )
        )
        self.assertEqual(reply, "Weekly goal — run 3 times")
        row = self.conn.execute("SELECT * FROM goals").fetchone()
        self.assertEqual(row["tier"], "weekly")
        self.assertEqual(row["expires_at"], "2026-09-06")  # Sunday of that week

    def test_monthly_goal_expires_end_of_month(self) -> None:
        repo.set_goal(self.conn, text="finish draft", tier="monthly", now=NOW)
        row = self.conn.execute("SELECT expires_at FROM goals").fetchone()
        self.assertEqual(row["expires_at"], "2026-09-30")

    def test_save_note_with_tags(self) -> None:
        reply = self.route(
            ParsedIntent(
                name="save_note",
                fields={"text": "prof prefers APA", "tags": ["PSYC", " Writing "]},
            )
        )
        self.assertEqual(reply, "Noted [psyc, writing].")
        row = self.conn.execute("SELECT * FROM notes").fetchone()
        self.assertEqual(row["tags"], "psyc,writing")

    # -- clarification ------------------------------------------------------

    def test_clarification_is_passed_through_verbatim(self) -> None:
        question = "Which course — PSYC 3040 or MATH 1021?"
        reply = self.route(
            ParsedIntent(
                name="ask_clarification",
                fields={"question": question, "reason": "ambiguous_course"},
            )
        )
        self.assertEqual(reply, question)

    def test_chat_without_a_writer_says_so(self) -> None:
        self.assertIn("can't hold a conversation", self.route(ParsedIntent(name="just_chat")))

    def test_answer_query_without_a_writer_says_so(self) -> None:
        """No client configured must not look like "I have no data"."""
        reply = self.route(
            ParsedIntent(name="answer_query", fields={"question": "what's due?"})
        )
        self.assertIn("can't answer", reply)


class ResponseParsingTestCase(unittest.TestCase):
    """The real client's unpacking of an Anthropic response."""

    def test_extracts_the_tool_call(self) -> None:
        intents = parse_tool_uses(
            [
                FakeTextBlock(text="Let me record that."),
                FakeToolUseBlock(name="add_task", input={"title": "Essay"}),
            ]
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].name, "add_task")
        self.assertEqual(intents[0].get("title"), "Essay")

    def test_every_tool_call_is_kept(self) -> None:
        """"Remind me today and tomorrow" used to lose the second reminder."""
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="add_reminder", input={"text": "forms",
                                                             "fire_at": "2026-09-10 19:00:00"}),
                FakeToolUseBlock(name="add_reminder", input={"text": "forms",
                                                             "fire_at": "2026-09-11 09:00:00"}),
            ]
        )
        self.assertEqual([i.name for i in intents], ["add_reminder", "add_reminder"])
        self.assertNotEqual(intents[0].get("fire_at"), intents[1].get("fire_at"))

    def test_an_identical_repeated_call_is_dropped(self) -> None:
        """Two of the same write is the model stuttering, not two instructions."""
        same = {"title": "Essay", "course": "CMDS 1630"}
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="add_task", input=dict(same)),
                FakeToolUseBlock(name="add_task", input=dict(same)),
            ]
        )
        self.assertEqual(len(intents), 1)

    def test_a_question_answers_the_whole_message_alone(self) -> None:
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="add_task", input={"title": "Essay"}),
                FakeToolUseBlock(
                    name="ask_clarification",
                    input={"question": "Which course?", "reason": "ambiguous_course"},
                ),
            ]
        )
        self.assertEqual([i.name for i in intents], ["ask_clarification"])

    def test_the_number_of_intents_is_capped(self) -> None:
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="add_task", input={"title": f"Essay {n}"})
                for n in range(MAX_INTENTS + 4)
            ]
        )
        self.assertEqual(len(intents), MAX_INTENTS)

    def test_a_whole_week_of_gym_split_survives_the_cap(self) -> None:
        """set_gym_split takes one weekday per call, so a week is seven."""
        week = ["Push", "Pull", "Legs", "Rest", "Push", "Pull", "Rest"]
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(
                    name="set_gym_split",
                    input={"day_of_week": day, "split_name": name},
                )
                for day, name in enumerate(week)
            ]
        )
        self.assertEqual(len(intents), 7, "Saturday and Sunday must not be dropped")
        self.assertEqual([i.get("split_name") for i in intents], week)

    def test_only_one_intent_may_answer_in_prose(self) -> None:
        """Each of these costs a second model call; two would double the wait."""
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="answer_query", input={"question": "due today"}),
                FakeToolUseBlock(name="answer_query", input={"question": "due tomorrow"}),
                FakeToolUseBlock(name="just_chat", input={"message": "feeling behind"}),
            ]
        )
        self.assertEqual([i.name for i in intents], ["answer_query"])

    def test_a_prose_intent_still_rides_along_with_writes(self) -> None:
        intents = parse_tool_uses(
            [
                FakeToolUseBlock(name="answer_query", input={"question": "due today"}),
                FakeToolUseBlock(
                    name="add_reminder",
                    input={"text": "start it", "fire_at": "2026-09-11 19:00:00"},
                ),
            ]
        )
        self.assertEqual([i.name for i in intents], ["answer_query", "add_reminder"])

    def test_no_tool_call_becomes_a_clarification(self) -> None:
        intents = parse_tool_uses([FakeTextBlock(text="I'm not sure.")])
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].name, "ask_clarification")
        self.assertEqual(intents[0].get("reason"), "intent_unclear")

    def test_unknown_tool_name_raises(self) -> None:
        with self.assertRaises(AssistantError) as ctx:
            parse_tool_uses([FakeToolUseBlock(name="launch_missiles", input={})])
        self.assertEqual(ctx.exception.code, E.CLAUDE)

    def test_empty_optional_fields_fall_back_to_defaults(self) -> None:
        intent = ParsedIntent(name="add_task", fields={"title": "x", "course": ""})
        self.assertIsNone(intent.get("course"))
        self.assertEqual(intent.get("priority", 2), 2)


class PromptContextTestCase(unittest.TestCase):
    def test_render_without_courses_says_so(self) -> None:
        rendered = PromptContext(now=NOW).render()
        self.assertIn("2026-09-05 14:30", rendered)
        self.assertIn("Saturday", rendered)
        self.assertIn("No courses are on file", rendered)


class ApiErrorMessageTestCase(unittest.TestCase):
    """A billing problem must not read as a network problem (Section 8)."""

    class _Status(Exception):
        def __init__(self, message: str, status_code: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code

    def test_low_credit_names_the_actual_fix(self) -> None:
        exc = self._Status(
            "Error code: 400 - Your credit balance is too low to access the "
            "Anthropic API. Please go to Plans & Billing.",
            400,
        )
        explained = _explain(exc)
        self.assertIn("credits", explained)
        self.assertIn("console.anthropic.com", explained)
        self.assertNotIn("reach Claude", explained)

    def test_bad_key_points_at_the_env_file(self) -> None:
        self.assertIn("ANTHROPIC_API_KEY", _explain(self._Status("unauthorized", 401)))

    def test_rate_limit_says_retry(self) -> None:
        self.assertIn("rate limit", _explain(self._Status("slow down", 429)))

    def test_unknown_failure_stays_generic(self) -> None:
        self.assertEqual(
            _explain(self._Status("connection reset")),
            "Couldn't reach Claude to read that.",
        )

    def test_a_timeout_says_nothing_was_saved(self) -> None:
        class APITimeoutError(Exception):
            pass

        explained = _explain(APITimeoutError("timed out"))
        self.assertIn("too long", explained)
        self.assertIn("Nothing was saved", explained)


class CallBoundsTestCase(unittest.TestCase):
    """A slow Claude call must not be able to freeze the bot.

    The SDK default is a 600s read timeout with two retries. That call runs
    while the worker thread holds the database lock, so half an hour of it
    means no reminders fire and no message is answered. One request went quiet
    for three and a half minutes on Sep 11 with the API otherwise healthy.
    """

    def test_the_client_bounds_every_call(self) -> None:
        client = AnthropicClient("sk-not-a-real-key", "claude-sonnet-5")
        self.assertEqual(client._client.timeout, REPLY_TIMEOUT_SECONDS)
        self.assertEqual(client._client.max_retries, 1)

    def test_the_bound_leaves_room_for_the_slowest_real_call(self) -> None:
        """The morning brief, measured at about eight seconds."""
        self.assertGreaterEqual(REPLY_TIMEOUT_SECONDS, 30)
        self.assertLess(REPLY_TIMEOUT_SECONDS, 120)


if __name__ == "__main__":
    unittest.main()
