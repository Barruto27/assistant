"""Answering a check-in, then taking it back.

    > did the reflection
    < Got it, reflection marked done.
    > wait no i didnt, i started it
    < Got it, iClicker participation marked as started.

The correction landed on a lecture he never mentioned. Two separate causes, one
in each of the two calls the check-in makes.

Routing: with a check-in open, "made it to the lecture" classified as just_chat
- the read-only path, which records nothing. The parser was told only that a
check-in existed, never what it had asked about, so his words had no referents.

Resolution: answering for a row retired it, so by the second message the parser
could not see the reflection at all and picked the only row left. Retiring an
answered row was right for deciding what still needs asking and wrong for
deciding what he may talk about.
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

EVENING = datetime(2026, 9, 12, 21, 30)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)
        self.reflection = repo.add_task(
            self.conn, title="Weekly Reflections (2/9)", course="PSYC 3265",
            due_date="2026-09-12",
        )
        self.iclicker = repo.add_task(
            self.conn, title="iClicker Participation (2/9)", course="PSYC 3265",
            due_date="2026-09-12",
        )
        with database.transaction(self.conn):
            self.conn.execute(
                "UPDATE tasks SET attendance = 1 WHERE id = ?", (self.iclicker,)
            )
        checkin.mark_sent(self.conn, EVENING, [self.reflection, self.iclicker])

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def status(self, task_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]


class AddressableTestCase(Base):
    """What he may talk about is not what still needs asking."""

    def test_everything_offered_stays_addressable(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        self.assertEqual(
            checkin.addressable(self.conn, EVENING),
            {self.reflection, self.iclicker},
        )

    def test_but_only_the_rest_is_still_pending(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        self.assertEqual(checkin.pending(self.conn, EVENING), {self.iclicker})

    def test_pending_is_none_once_everything_is_answered(self) -> None:
        checkin.resolve(self.conn, {self.reflection, self.iclicker}, now=EVENING)
        self.assertIsNone(checkin.pending(self.conn, EVENING))

    def test_answered_rows_remain_addressable_for_a_correction(self) -> None:
        checkin.resolve(self.conn, {self.reflection, self.iclicker}, now=EVENING)
        self.assertEqual(
            checkin.addressable(self.conn, EVENING),
            {self.reflection, self.iclicker},
        )

    def test_the_window_still_ends_it(self) -> None:
        later = EVENING + timedelta(hours=checkin.REPLY_WINDOW_HOURS + 1)
        self.assertEqual(checkin.addressable(self.conn, later), set())
        self.assertIsNone(checkin.pending(self.conn, later))


class OrderTestCase(Base):
    """A retraction refers to the last thing said, so order has to survive."""

    def test_answers_keep_the_order_he_gave_them(self) -> None:
        checkin.resolve(self.conn, {self.iclicker}, now=EVENING)
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        context = checkin.gather(self.conn, EVENING)
        self.assertEqual(
            [row["id"] for row in context.answered_tonight],
            [self.iclicker, self.reflection],
        )

    def test_the_most_recent_is_marked_for_the_parser(self) -> None:
        checkin.resolve(self.conn, {self.iclicker}, now=EVENING)
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        rendered = checkin.render_context(checkin.gather(self.conn, EVENING))
        last_line = [
            line for line in rendered.splitlines()
            if "the last thing he told you about" in line
        ]
        self.assertEqual(len(last_line), 1)
        self.assertIn(str(self.reflection), last_line[0])

    def test_answering_twice_moves_it_to_the_end(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        checkin.resolve(self.conn, {self.iclicker}, now=EVENING)
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        context = checkin.gather(self.conn, EVENING)
        self.assertEqual(
            [row["id"] for row in context.answered_tonight],
            [self.iclicker, self.reflection],
        )


class AttendanceInvariantTestCase(Base):
    """An attendance mark has no half-way state."""

    def apply(self, **kwargs) -> dict:
        result = checkin.CheckinResult(**kwargs)
        return checkin.apply(self.conn, result, now=EVENING)

    def test_an_attendance_mark_cannot_be_started(self) -> None:
        counts = self.apply(started=[self.iclicker])
        self.assertEqual(self.status(self.iclicker), "not_started")
        self.assertEqual(counts["refused"], 1)
        self.assertEqual(counts["started"], 0)

    def test_ordinary_work_still_can_be(self) -> None:
        counts = self.apply(started=[self.reflection])
        self.assertEqual(self.status(self.reflection), "in_progress")
        self.assertEqual(counts["refused"], 0)

    def test_a_mixed_batch_keeps_the_good_half(self) -> None:
        counts = self.apply(started=[self.reflection, self.iclicker])
        self.assertEqual(self.status(self.reflection), "in_progress")
        self.assertEqual(self.status(self.iclicker), "not_started")
        self.assertEqual((counts["started"], counts["refused"]), (1, 1))

    def test_attending_is_still_how_it_gets_marked(self) -> None:
        self.apply(attended=[self.iclicker])
        self.assertEqual(self.status(self.iclicker), "done")


class RenderTestCase(Base):
    def test_the_offered_rows_are_described_for_the_parser(self) -> None:
        described = checkin.offered(self.conn, EVENING)
        self.assertTrue(any("Weekly Reflections" in d for d in described))
        self.assertTrue(any("attendance mark" in d for d in described))

    def test_answered_rows_are_not_offered_again(self) -> None:
        checkin.resolve(self.conn, {self.reflection}, now=EVENING)
        described = checkin.offered(self.conn, EVENING)
        self.assertFalse(any("Weekly Reflections" in d for d in described))
        self.assertTrue(any("iClicker" in d for d in described))

    def test_the_reply_prompt_allows_a_retraction(self) -> None:
        flat = " ".join(checkin.REPLY_SYSTEM.split())
        self.assertIn("take back what he said", flat)
        self.assertIn("the last thing he told you about", flat)


if __name__ == "__main__":
    unittest.main()
