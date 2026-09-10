"""Backlog triage and goal nudges (plan Sections 9 and 11).

Both features exist to stop the brief nagging. The tests are mostly about what
must NOT appear.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import backlog, brief, repository as repo  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

TODAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 7, 30)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        database.set_config(self.conn, "semester_start_date", "2026-09-08")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class DemotionTestCase(Base):
    def test_long_overdue_low_stakes_work_is_set_aside(self) -> None:
        repo.add_task(self.conn, title="Old reading", due_date="2026-09-01", priority=3)
        moved = backlog.demote(self.conn, TODAY)
        self.assertEqual([m.title for m in moved], ["Old reading"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks").fetchone()["status"], "stale"
        )

    def test_priority_one_is_never_demoted(self) -> None:
        """The whole point of P1: an exam stays visible however late."""
        repo.add_task(self.conn, title="Missed exam", due_date="2026-08-01", priority=1)
        self.assertEqual(backlog.demote(self.conn, TODAY), [])
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks").fetchone()["status"],
            "not_started",
        )

    def test_recently_overdue_work_is_left_alone(self) -> None:
        repo.add_task(self.conn, title="Essay", due_date="2026-09-28", priority=2)
        self.assertEqual(backlog.demote(self.conn, TODAY), [])

    def test_threshold_is_configurable(self) -> None:
        repo.add_task(self.conn, title="Essay", due_date="2026-09-28", priority=2)
        database.set_config(self.conn, "backlog_threshold_days", "1")
        self.assertEqual([m.title for m in backlog.demote(self.conn, TODAY)], ["Essay"])

    def test_a_bad_threshold_falls_back_rather_than_crashing(self) -> None:
        database.set_config(self.conn, "backlog_threshold_days", "soon")
        self.assertEqual(backlog.threshold_days(self.conn), backlog.DEFAULT_THRESHOLD_DAYS)

    def test_passed_attendance_goes_immediately(self) -> None:
        """A missed lecture can't be made up; carrying it forward helps nobody."""
        tid = repo.add_task(self.conn, title="iClicker wk2", due_date="2026-09-29")
        self.conn.execute("UPDATE tasks SET attendance = 1 WHERE id = ?", (tid,))
        moved = backlog.demote(self.conn, TODAY)
        self.assertEqual([m.title for m in moved], ["iClicker wk2"])
        self.assertIn("lecture that has passed", moved[0].reason)

    def test_completed_work_is_not_touched(self) -> None:
        tid = repo.add_task(self.conn, title="Done thing", due_date="2026-09-01")
        repo.update_task(self.conn, tid, status="done")
        self.assertEqual(backlog.demote(self.conn, TODAY), [])

    def test_undated_work_is_never_demoted(self) -> None:
        repo.add_task(self.conn, title="Concept Brief", priority=2)
        self.assertEqual(backlog.demote(self.conn, TODAY), [])


class RestoreAndArchiveTestCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.tid = repo.add_task(self.conn, title="Old reading",
                                 due_date="2026-09-01", priority=3)
        backlog.demote(self.conn, TODAY)

    def test_nothing_is_ever_deleted(self) -> None:
        backlog.archive(self.conn, [self.tid])
        row = self.conn.execute("SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "archived", "archive, never delete")

    def test_restore_brings_it_back(self) -> None:
        self.assertTrue(backlog.restore(self.conn, self.tid))
        row = self.conn.execute("SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "not_started")

    def test_restoring_something_not_stale_is_a_no_op(self) -> None:
        backlog.restore(self.conn, self.tid)
        self.assertFalse(backlog.restore(self.conn, self.tid))

    def test_render_lists_ids_so_they_can_be_named(self) -> None:
        text = backlog.render(backlog.backlog(self.conn))
        self.assertIn(f"#{self.tid}", text)
        self.assertIn("bring back", text)

    def test_empty_backlog_reads_as_reassurance(self) -> None:
        backlog.archive(self.conn, [self.tid])
        self.assertIn("nothing has fallen behind", backlog.render(backlog.backlog(self.conn)))


class WeeklyNudgeTestCase(Base):
    def test_no_backlog_means_no_nudge(self) -> None:
        self.assertFalse(backlog.weekly_nudge_due(self.conn, NOW))

    def test_first_time_nudges(self) -> None:
        repo.add_task(self.conn, title="Old", due_date="2026-09-01", priority=3)
        backlog.demote(self.conn, TODAY)
        self.assertTrue(backlog.weekly_nudge_due(self.conn, NOW))

    def test_not_again_the_next_day(self) -> None:
        """Weekly, not daily — that repetition is the nagging being avoided."""
        repo.add_task(self.conn, title="Old", due_date="2026-09-01", priority=3)
        backlog.demote(self.conn, TODAY)
        database.set_config(self.conn, "backlog_nudged_on", TODAY.isoformat())
        self.assertFalse(backlog.weekly_nudge_due(self.conn, NOW + timedelta(days=1)))

    def test_again_after_a_week(self) -> None:
        repo.add_task(self.conn, title="Old", due_date="2026-09-01", priority=3)
        backlog.demote(self.conn, TODAY)
        database.set_config(self.conn, "backlog_nudged_on", TODAY.isoformat())
        self.assertTrue(backlog.weekly_nudge_due(self.conn, NOW + timedelta(days=8)))


class GoalNudgeTestCase(Base):
    def test_daily_goals_are_never_nudged_for_staleness(self) -> None:
        """A daily goal with no progress is just an ordinary day."""
        repo.set_goal(self.conn, text="read 20 pages", tier="daily",
                      now=NOW - timedelta(days=30))
        self.assertEqual(repo.stalled_goals(self.conn, NOW), [])

    def test_quiet_weekly_goal_is_nudged(self) -> None:
        repo.set_goal(self.conn, text="gym 4 times", tier="weekly",
                      now=NOW - timedelta(days=30))
        stalled = repo.stalled_goals(self.conn, NOW)
        self.assertEqual([g["text"] for g in stalled], ["gym 4 times"])

    def test_recent_progress_stops_the_nudge(self) -> None:
        gid = repo.set_goal(self.conn, text="gym 4 times", tier="weekly",
                            now=NOW - timedelta(days=30))
        repo.record_goal_progress(self.conn, gid, NOW)
        self.assertEqual(repo.stalled_goals(self.conn, NOW), [])

    def test_a_nudged_goal_goes_quiet_again(self) -> None:
        gid = repo.set_goal(self.conn, text="gym 4 times", tier="weekly",
                            now=NOW - timedelta(days=30))
        repo.mark_goal_nudged(self.conn, gid, NOW)
        self.assertEqual(repo.stalled_goals(self.conn, NOW), [])

    def test_missed_daily_goal_is_mentioned_exactly_once(self) -> None:
        gid = repo.set_goal(self.conn, text="run", tier="daily",
                            now=NOW - timedelta(days=3))
        first = repo.missed_daily_goals(self.conn, TODAY)
        self.assertEqual(len(first), 1)
        repo.mark_goal_missed_mentioned(self.conn, gid)
        self.assertEqual(repo.missed_daily_goals(self.conn, TODAY), [],
                         "Section 11: one soft mention, never repeated")


class BriefIntegrationTestCase(Base):
    def test_backlog_mentioned_once_and_never_listed(self) -> None:
        repo.add_task(self.conn, title="Old reading", due_date="2026-09-01", priority=3)
        backlog.demote(self.conn, TODAY)
        facts = brief.render_facts(brief.assemble(self.conn, now=NOW))
        self.assertIn("BACKLOG: 1 item", facts)
        self.assertIn("Do not list them", facts)
        self.assertNotIn("Old reading", facts)

    def test_no_backlog_section_when_nothing_is_set_aside(self) -> None:
        self.assertNotIn("BACKLOG:", brief.render_facts(brief.assemble(self.conn, now=NOW)))

    def test_stale_work_stays_out_of_the_daily_view(self) -> None:
        repo.add_task(self.conn, title="Old reading", due_date="2026-09-30", priority=3)
        self.conn.execute("UPDATE tasks SET status = 'stale'")
        context = brief.assemble(self.conn, now=NOW)
        self.assertEqual(context.due_today, [])

    def test_goal_prompts_ask_for_restraint(self) -> None:
        repo.set_goal(self.conn, text="gym 4 times", tier="weekly",
                      now=NOW - timedelta(days=30))
        facts = brief.render_facts(brief.assemble(self.conn, now=NOW))
        self.assertIn("NO REPORTED MOVEMENT", facts)
        self.assertIn("rather than a prod", facts)


if __name__ == "__main__":
    unittest.main()
