"""Database locking (from the Sep 11 freeze).

bot.main held one coarse lock around the whole of handle_message, which
includes a classification and usually a second Claude call to write prose. A
request that went slow froze the bot for three and a half minutes: no reminder
fired, no job ran, no other message was answered, and the chat simply did not
reply.

The lock only ever existed to stop two explicit BEGIN/COMMIT blocks
interleaving on the shared connection, so it now lives on transaction()
instead. These tests pin both halves of that: transactions are still
serialised, and slow work that is not a transaction no longer blocks anyone.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import repository as repo  # noqa: E402
from bot.errors import setup_logging  # noqa: E402
from db import database  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class SerialisationTestCase(Base):
    """Two transactions must not interleave."""

    def test_transactions_do_not_overlap(self) -> None:
        inside: list[str] = []

        def hold(tag: str) -> None:
            with database.transaction(self.conn):
                inside.append(f"{tag}-in")
                time.sleep(0.05)
                inside.append(f"{tag}-out")

        threads = [threading.Thread(target=hold, args=(t,)) for t in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Whatever the order, each pair must be adjacent: no "a-in, b-in".
        self.assertEqual(len(inside), 4)
        self.assertEqual(inside[0][-2:], "in")
        self.assertEqual(inside[1][0], inside[0][0])
        self.assertEqual(inside[3][0], inside[2][0])

    def test_the_lock_is_reentrant(self) -> None:
        """bot.main takes it coarsely around sequences that then transact."""
        with database.write_lock:
            repo.add_task(self.conn, title="Essay", due_date="2026-09-20")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"], 1
        )

    def test_bot_main_shares_this_lock_rather_than_owning_one(self) -> None:
        """A second, independent lock would deadlock against this one.

        bot.main still takes the lock coarsely where a whole sequence must be
        exclusive, and those sequences transact while holding it. That only
        works while both are the same reentrant lock.
        """
        from bot import main

        self.assertIs(main._db_lock, database.write_lock)

    def test_a_failed_transaction_releases_the_lock(self) -> None:
        with self.assertRaises(ValueError):
            with database.transaction(self.conn):
                raise ValueError("boom")
        self.assertTrue(database.write_lock.acquire(timeout=1), "lock was left held")
        database.write_lock.release()


class NoFreezeTestCase(Base):
    """The actual bug: slow non-database work blocking the database."""

    def test_slow_work_outside_a_transaction_blocks_nobody(self) -> None:
        """Stands in for the Claude call that used to hold the lock."""
        started = threading.Event()
        release = threading.Event()

        def slow_claude_call() -> None:
            # Exactly what handle_message does now: read, think, write. The
            # thinking is not a transaction and must not hold the lock.
            self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            started.set()
            release.wait(timeout=5)
            repo.add_task(self.conn, title="After thinking", due_date="2026-09-20")

        worker = threading.Thread(target=slow_claude_call)
        worker.start()
        self.assertTrue(started.wait(timeout=2))

        # Meanwhile the reminder poll has to get through. Before this change it
        # waited behind the whole call.
        deadline = time.monotonic() + 2
        repo.add_reminder(self.conn, text="poll", fire_at="2026-09-20 09:00:00")
        self.assertLess(
            time.monotonic(), deadline, "a write waited on unrelated slow work"
        )

        release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        titles = {r["title"] for r in self.conn.execute("SELECT title FROM tasks")}
        self.assertIn("After thinking", titles)

    def test_reads_do_not_wait_on_a_write(self) -> None:
        """sqlite3 is in serialized mode; reads never needed the lock."""
        repo.add_task(self.conn, title="Essay", due_date="2026-09-20")
        done = threading.Event()

        def read_many() -> None:
            for _ in range(50):
                self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            done.set()

        reader = threading.Thread(target=read_many)
        with database.write_lock:
            reader.start()
            self.assertTrue(
                done.wait(timeout=3), "reads blocked behind a held write lock"
            )
        reader.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
