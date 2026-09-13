"""Adversarial harness for the conversation pipeline.

Runs messages through the real classifier and the real router against a *copy*
of the database, and never touches Telegram. That is the containment that
matters here: the risk in this work is corrupting Kaan's data or spamming his
chat at 2am, not anything the filesystem can protect against.

Each case says what it expects. A case can assert on the intents chosen, on
substrings that must or must not appear in the reply, and on the database
afterwards. Anything that does not hold is printed as a gap to go and fix.

    python lab.py                 run every case
    python lab.py --only pronoun  run cases whose name contains "pronoun"
    python lab.py --list          names only, no API calls
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv(".env")

from bot import claude_client, router  # noqa: E402
from bot.config import load_settings  # noqa: E402
from bot.errors import AssistantError  # noqa: E402
from db import database  # noqa: E402

#: Cases run against a copy of the real database, because synthetic data hides
#: the bugs that matter - every finding in these files came from real rows.
LIVE = Path(os.getenv("LAB_SOURCE_DB", "/home/assistant/assistant/db/assistant.sqlite3"))
WORKING = Path(os.getenv("LAB_WORKING_DB", "db/lab.sqlite3"))


#: Row counts worth comparing before and after a case.
COUNTED = ("tasks", "reminders", "goals", "notes", "flagged_emails", "quotes")


def snapshot(conn: sqlite3.Connection) -> dict:
    """Counts before a case runs, plus the set of tasks already finished.

    A copy of the real database starts with real rows in it - one task is
    already done - so every assertion has to be about what *changed*.
    """
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in COUNTED
    }
    counts["done_ids"] = {
        row["id"]
        for row in conn.execute("SELECT id FROM tasks WHERE status = 'done'")
    }
    return counts


@dataclass
class Case:
    """One adversarial message, and what should be true afterwards."""

    name: str
    #: A single message, or several sent in order to test follow-ups.
    messages: list[str]
    intents: list[str] | None = None          # exact intent names, in order
    wants: list[str] = field(default_factory=list)      # substrings the reply must contain
    forbids: list[str] = field(default_factory=list)    # substrings it must not
    #: check(conn, before) -> complaint or None. `before` is a snapshot of
    #: row counts taken before the messages ran, because comparing against
    #: a wall-clock window got this wrong twice: created_at is written in
    #: localtime and SQLite's datetime('now') is UTC.
    check: Callable[[sqlite3.Connection, dict], str | None] | None = None
    #: Runs before the messages, against the same connection. Whatever it
    #: returns lands in before['setup'], so a check can refer to the rows
    #: it created. Used to stage a check-in that is already outstanding.
    setup: Callable[[sqlite3.Connection], Any] | None = None
    note: str = ""                            # why this case exists
    #: Pin the clock when the case depends on it (an evening check-in).
    when: datetime | None = None


@dataclass
class Result:
    case: Case
    intents: list[str]
    replies: list[str]
    problems: list[str]
    seconds: float

    @property
    def ok(self) -> bool:
        return not self.problems


class Lab:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.client = claude_client.AnthropicClient(
            self.settings.anthropic_api_key,
            self.settings.claude_model,
            classify_model=self.settings.classify_model,
        )

    def fresh(self) -> sqlite3.Connection:
        """A copy of the real database, so cases start from real data.

        The sidecars have to go with it. Copying only the main file left the
        previous case's -wal in place, SQLite replayed it onto the new copy,
        and one case answered with another case's reminder text - the same
        hybrid that corrupted the live database during a deploy.
        """
        WORKING.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            leftover = WORKING.with_name(WORKING.name + suffix)
            leftover.unlink(missing_ok=True)
        shutil.copy(LIVE, WORKING)
        conn = database.connect(WORKING)
        database.migrate(conn)
        return conn

    def run(self, case: Case, now: datetime | None = None) -> Result:
        conn = self.fresh()
        staged = case.setup(conn) if case.setup else None
        before = snapshot(conn)
        before["setup"] = staged
        moment = now or getattr(case, "when", None) or datetime.now()
        seen: list[str] = []
        replies: list[str] = []
        problems: list[str] = []
        started = time.time()

        try:
            for message in case.messages:
                captured: list[str] = []

                class Watching:
                    """Wraps the classifier to record what it chose."""

                    def __init__(self, inner):
                        self._inner = inner

                    def classify(self, message, context):
                        intents = self._inner.classify(message, context)
                        captured.extend(i.name for i in intents)
                        return intents

                    def compose(self, *a, **k):
                        return self._inner.compose(*a, **k)

                    def call_tool(self, *a, **k):
                        return self._inner.call_tool(*a, **k)

                watched = Watching(self.client)
                try:
                    reply = router.handle_message(
                        conn, watched, message, now=moment, writer=watched
                    )
                except AssistantError as err:
                    reply = f"[{err.code}] {err.user_message()}"
                seen.extend(captured)
                replies.append(reply)

            joined = "\n".join(replies)
            if case.intents is not None and seen != case.intents:
                problems.append(f"intents: wanted {case.intents}, got {seen}")
            for needle in case.wants:
                if needle.lower() not in joined.lower():
                    problems.append(f"reply is missing {needle!r}")
            for needle in case.forbids:
                if needle.lower() in joined.lower():
                    problems.append(f"reply should not contain {needle!r}")
            if case.check is not None:
                complaint = case.check(conn, before)
                if complaint:
                    problems.append(complaint)
        except Exception:  # noqa: BLE001 - a crash is the most interesting result
            problems.append("EXCEPTION\n" + traceback.format_exc())
        finally:
            conn.close()

        return Result(case, seen, replies, problems, time.time() - started)


def report(results: list[Result]) -> int:
    failed = [r for r in results if not r.ok]
    for r in results:
        mark = "ok  " if r.ok else "GAP "
        print(f"[{mark}] {r.seconds:5.1f}s  {r.case.name}")
        if not r.ok:
            if r.case.note:
                print(f"          why it exists: {r.case.note}")
            for message, reply in zip(r.case.messages, r.replies):
                print(f"          > {message}")
                for line in reply.splitlines() or [""]:
                    print(f"          < {line}")
            for problem in r.problems:
                for line in problem.splitlines():
                    print(f"          ! {line}")
    print()
    print(f"{len(results) - len(failed)}/{len(results)} clean, {len(failed)} gaps")
    return len(failed)


def main(cases: list[Case]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    chosen = [c for c in cases if args.only.lower() in c.name.lower()]
    if args.list:
        for c in chosen:
            print(f"  {c.name}")
        return 0

    lab = Lab()
    results = [lab.run(c) for c in chosen]
    return report(results)
