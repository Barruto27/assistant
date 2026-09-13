"""Remembering a question long enough to understand the answer.

Found by testing a two-message exchange:

    > remind me to email my prof about the quiz
    < When should I remind you - tonight, tomorrow, or a specific time?
    > tomorrow at 10
    < What do you want me to remind you about?

Each message is classified on its own, so the answer to the bot's own question
arrived with no idea what had been asked. It then asked the other half, and
would have gone round forever. Nothing was saved either time.

The evening check-in already solves this shape of problem by writing down that
it is waiting and what it offered. This is the same idea for clarifying
questions: when a handler asks, it writes down the intent it could not complete
and what it asked; the next message is classified knowing both, and the fields
it already had are merged back in.

Deliberately short-lived. A question from two hours ago should not reinterpret
an unrelated message, so anything older than the window is ignored - and the
window is measured from when the question was asked, not when it is read.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bot.errors import logger
from db.database import get_config, set_config

#: How long an unanswered question stays open. Long enough to walk away from
#: the phone mid-exchange, short enough that it cannot capture a message about
#: something else entirely.
WINDOW_MINUTES = 30

_KEY_INTENT = "pending_clarification"
_KEY_ASKED_AT = "pending_clarification_at"

TS = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Pending:
    """A question that was asked and the half-built intent behind it."""

    intent: str
    fields: dict[str, Any]
    question: str


def remember(
    conn: sqlite3.Connection,
    *,
    intent: str,
    fields: dict[str, Any],
    question: str,
    now: datetime,
) -> None:
    """Note what was asked, so the answer can be understood."""
    set_config(
        conn,
        _KEY_INTENT,
        json.dumps({"intent": intent, "fields": fields, "question": question}),
    )
    set_config(conn, _KEY_ASKED_AT, now.strftime(TS))
    logger.info("Awaiting an answer to: %s", question)


def pending(conn: sqlite3.Connection, now: datetime) -> Pending | None:
    """The open question, or None if there isn't one or it has gone stale."""
    raw = get_config(conn, _KEY_INTENT, "")
    stamp = get_config(conn, _KEY_ASKED_AT, "")
    if not raw or not stamp:
        return None
    try:
        asked = datetime.strptime(stamp, TS)
    except ValueError:
        return None
    if (now.replace(tzinfo=None) - asked) > timedelta(minutes=WINDOW_MINUTES):
        return None
    try:
        data = json.loads(raw)
        return Pending(
            intent=str(data["intent"]),
            fields=dict(data.get("fields") or {}),
            question=str(data.get("question", "")),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("Unreadable pending clarification: %r", raw[:120])
        return None


def clear(conn: sqlite3.Connection) -> None:
    set_config(conn, _KEY_INTENT, "")
    set_config(conn, _KEY_ASKED_AT, "")


def merge(open_question: Pending | None, intent_name: str, fields: dict) -> dict:
    """Fold the fields from before the question into the answer.

    Only for the same intent: if he answered by changing the subject entirely,
    the old half-built fields are not his and must not be smuggled in. What he
    just said wins over what was already there, so a correction still corrects.
    """
    if open_question is None or open_question.intent != intent_name:
        return fields
    merged = dict(open_question.fields)
    merged.update({k: v for k, v in fields.items() if v not in ("", None)})
    return merged
