"""Evening check-in (plan Section 10).

Nothing else marks work done, so this is the only thing keeping the brief from
drifting away from what is actually true.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import checkin, repository as repo  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

NOW = datetime(2026, 9, 10, 21, 0)
TODAY = "2026-09-10"
TOMORROW = "2026-09-11"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class GatherTestCase(Base):
    def test_nothing_on_today_is_not_worth_asking(self) -> None:
        """A check-in on an empty day is a demand dressed as a question."""
        self.assertFalse(checkin.has_anything_to_ask(checkin.gather(self.conn, NOW)))

    def test_work_due_today_is_worth_asking(self) -> None:
        repo.add_task(self.conn, title="Reflection", due_date=TODAY)
        self.assertTrue(checkin.has_anything_to_ask(checkin.gather(self.conn, NOW)))

    def test_attendance_is_separated_from_work(self) -> None:
        repo.add_task(self.conn, title="Reflection", due_date=TODAY)
        tid = repo.add_task(self.conn, title="iClicker", due_date=TODAY)
        self.conn.execute("UPDATE tasks SET attendance = 1 WHERE id = ?", (tid,))
        context = checkin.gather(self.conn, NOW)
        self.assertEqual([r["title"] for r in context.due_today], ["Reflection"])
        self.assertEqual([r["title"] for r in context.attendance_today], ["iClicker"])

    def test_tomorrow_is_offered_for_context_only(self) -> None:
        repo.add_task(self.conn, title="A1", due_date=TOMORROW)
        context = checkin.gather(self.conn, NOW)
        self.assertEqual([r["title"] for r in context.due_tomorrow], ["A1"])

    def test_completed_work_is_not_asked_about_again(self) -> None:
        tid = repo.add_task(self.conn, title="Done already", due_date=TODAY)
        repo.update_task(self.conn, tid, status="done")
        self.assertEqual(checkin.gather(self.conn, NOW).due_today, [])

    def test_rendered_context_carries_ids(self) -> None:
        tid = repo.add_task(self.conn, title="Reflection", due_date=TODAY)
        self.assertIn(f"[{tid}]", checkin.render_context(checkin.gather(self.conn, NOW)))

    def test_prompt_forbids_asking_why(self) -> None:
        flat = " ".join(checkin.PROMPT_SYSTEM.lower().split())
        self.assertIn("never ask why something didn't happen", flat)
        self.assertIn("no cheerleading, no disappointment", flat)


class PendingTestCase(Base):
    def test_no_checkin_means_nothing_pending(self) -> None:
        self.assertIsNone(checkin.pending(self.conn, NOW))

    def test_sending_records_the_offered_ids(self) -> None:
        checkin.mark_sent(self.conn, NOW, [3, 7])
        self.assertEqual(checkin.pending(self.conn, NOW), {3, 7})

    def test_it_expires_so_tomorrows_message_is_not_a_reply(self) -> None:
        checkin.mark_sent(self.conn, NOW, [3])
        self.assertIsNone(
            checkin.pending(self.conn, NOW + timedelta(hours=checkin.REPLY_WINDOW_HOURS + 1))
        )

    def test_still_open_the_next_morning(self) -> None:
        checkin.mark_sent(self.conn, NOW, [3])
        self.assertEqual(checkin.pending(self.conn, NOW + timedelta(hours=10)), {3})

    def test_clearing_closes_it(self) -> None:
        checkin.mark_sent(self.conn, NOW, [3])
        checkin.clear(self.conn)
        self.assertIsNone(checkin.pending(self.conn, NOW))

    def test_a_corrupt_timestamp_is_treated_as_nothing_pending(self) -> None:
        database.set_config(self.conn, "checkin_sent_at", "last tuesday")
        self.assertIsNone(checkin.pending(self.conn, NOW))


class ParseTestCase(unittest.TestCase):
    """Ids not offered are dropped, never guessed at."""

    def test_only_offered_ids_survive(self) -> None:
        result = checkin.parse_reply(
            {"done": [1, 999], "started": [2], "reply": "ok"}, {1, 2}
        )
        self.assertEqual(result.done, [1])
        self.assertEqual(result.started, [2])

    def test_not_done_entries_are_filtered_too(self) -> None:
        result = checkin.parse_reply(
            {"not_done": [{"task_id": 1, "reason": "ran out of time"},
                          {"task_id": 42, "reason": "invented"}], "reply": "ok"},
            {1},
        )
        self.assertEqual(result.not_done, [(1, "ran out of time")])

    def test_malformed_entries_are_skipped(self) -> None:
        result = checkin.parse_reply(
            {"not_done": [{"reason": "no id"}, {"task_id": "abc"}], "reply": "ok"}, {1}
        )
        self.assertEqual(result.not_done, [])

    def test_a_missing_reason_stays_missing(self) -> None:
        result = checkin.parse_reply(
            {"not_done": [{"task_id": 1}], "reply": "ok"}, {1}
        )
        self.assertEqual(result.not_done, [(1, None)])


class ApplyTestCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.done_id = repo.add_task(self.conn, title="Reflection", due_date=TODAY)
        self.open_id = repo.add_task(self.conn, title="Essay", due_date=TODAY)
        self.att_id = repo.add_task(self.conn, title="iClicker", due_date=TODAY)
        self.conn.execute("UPDATE tasks SET attendance = 1 WHERE id = ?", (self.att_id,))

    def status(self, task_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]

    def test_finished_work_is_marked_done(self) -> None:
        checkin.apply(self.conn, checkin.parse_reply(
            {"done": [self.done_id], "reply": "ok"}, {self.done_id}), now=NOW)
        self.assertEqual(self.status(self.done_id), "done")

    def test_attendance_confirmed_is_marked_done(self) -> None:
        checkin.apply(self.conn, checkin.parse_reply(
            {"attended": [self.att_id], "reply": "ok"}, {self.att_id}), now=NOW)
        self.assertEqual(self.status(self.att_id), "done")

    def test_started_work_becomes_in_progress(self) -> None:
        checkin.apply(self.conn, checkin.parse_reply(
            {"started": [self.open_id], "reply": "ok"}, {self.open_id}), now=NOW)
        self.assertEqual(self.status(self.open_id), "in_progress")

    def test_not_done_stays_open(self) -> None:
        """The backlog rule decides when it stops showing, not the check-in."""
        checkin.apply(self.conn, checkin.parse_reply(
            {"not_done": [{"task_id": self.open_id, "reason": "no time"}], "reply": "ok"},
            {self.open_id}), now=NOW)
        self.assertEqual(self.status(self.open_id), "not_started")

    def test_a_given_reason_is_kept_on_the_task(self) -> None:
        checkin.apply(self.conn, checkin.parse_reply(
            {"not_done": [{"task_id": self.open_id, "reason": "lab ran long"}],
             "reply": "ok"}, {self.open_id}), now=NOW)
        notes = self.conn.execute(
            "SELECT notes FROM tasks WHERE id = ?", (self.open_id,)
        ).fetchone()["notes"]
        self.assertIn("lab ran long", notes)

    def test_nothing_is_deleted(self) -> None:
        before = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        checkin.apply(self.conn, checkin.parse_reply(
            {"done": [self.done_id], "reply": "ok"}, {self.done_id}), now=NOW)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(before, after)

    def test_tomorrows_plan_is_saved_as_a_note(self) -> None:
        checkin.apply(self.conn, checkin.parse_reply(
            {"tomorrow": "start the soundscape", "reply": "ok"}, set()), now=NOW)
        row = self.conn.execute("SELECT text, tags FROM notes").fetchone()
        self.assertIn("start the soundscape", row["text"])
        self.assertIn("tomorrow", row["tags"])

    def test_meeting_the_daily_goal_closes_it(self) -> None:
        repo.set_goal(self.conn, text="read 20 pages", tier="daily", now=NOW)
        checkin.apply(self.conn, checkin.parse_reply(
            {"goal_met": True, "reply": "ok"}, set()), now=NOW)
        self.assertEqual(
            self.conn.execute("SELECT status FROM goals").fetchone()["status"], "done"
        )

    def test_an_unmet_goal_is_left_active_not_failed(self) -> None:
        repo.set_goal(self.conn, text="read 20 pages", tier="daily", now=NOW)
        checkin.apply(self.conn, checkin.parse_reply(
            {"goal_met": False, "reply": "ok"}, set()), now=NOW)
        self.assertEqual(
            self.conn.execute("SELECT status FROM goals").fetchone()["status"], "active"
        )

    def test_reply_prompt_forbids_inventing_reasons(self) -> None:
        flat = " ".join(checkin.REPLY_SYSTEM.lower().split())
        self.assertIn("do not infer one", flat)
        self.assertIn("not a failing", flat)


class TomorrowGoalTestCase(unittest.TestCase):
    """Plan Section 11: ask for tomorrow's goal when there isn't one.

    The half that was never built. The check-in recorded a goal if Kaan
    volunteered one and otherwise said nothing, so the only daily goals that
    ever existed were the ones he thought to set unprompted.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        self.now = datetime(2026, 9, 12, 21, 0)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def rendered(self) -> str:
        return checkin.render_context(checkin.gather(self.conn, self.now))

    def test_no_goal_at_all_prompts_for_one(self) -> None:
        self.assertIn("NO GOAL SET FOR TOMORROW", self.rendered())

    def test_todays_goal_does_not_count_for_tomorrow(self) -> None:
        """It expires tonight, so tomorrow still has nothing."""
        repo.set_goal(self.conn, text="Finish the reading", tier="daily", now=self.now)
        self.assertIn("NO GOAL SET FOR TOMORROW", self.rendered())

    def test_a_goal_that_runs_past_tonight_counts(self) -> None:
        repo.set_goal(self.conn, text="Finish the reading", tier="daily", now=self.now)
        with database.transaction(self.conn):
            self.conn.execute("UPDATE goals SET expires_at = '2026-09-13'")
        self.assertNotIn("NO GOAL SET FOR TOMORROW", self.rendered())

    def test_a_weekly_goal_is_not_a_daily_one(self) -> None:
        repo.set_goal(self.conn, text="Get ahead on DATT", tier="weekly", now=self.now)
        self.assertIn("NO GOAL SET FOR TOMORROW", self.rendered())

    def test_a_dropped_goal_does_not_count(self) -> None:
        repo.set_goal(self.conn, text="Finish the reading", tier="daily", now=self.now)
        with database.transaction(self.conn):
            self.conn.execute(
                "UPDATE goals SET expires_at = '2026-09-13', status = 'dropped'"
            )
        self.assertIn("NO GOAL SET FOR TOMORROW", self.rendered())

    def test_the_prompt_asks_once_and_lets_him_ignore_it(self) -> None:
        self.assertIn("NO GOAL SET FOR TOMORROW", checkin.PROMPT_SYSTEM)
        self.assertIn("free to ignore", checkin.PROMPT_SYSTEM)
        self.assertIn("already set", checkin.PROMPT_SYSTEM)


if __name__ == "__main__":
    unittest.main()
