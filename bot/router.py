"""Intent dispatch and receipts (plan Section 4).

``handle_message`` is a plain function taking a connection and a classifier, so
the whole pipeline is testable without Telegram, a network, or an API key.

Receipts are built here in Python rather than by a second Claude call: they are
structured confirmations of what landed in specific columns, they must be
identical every time so a misparse is obvious at a glance, and one message
should cost one API call (principle 1).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from bot import query, repository as repo
from contextvars import ContextVar

from bot.claude_client import Classifier, PromptContext, Writer
from bot.errors import AssistantError, E, logger
from bot.formatting import pct
from bot.intents import (
    ADD_REMINDER,
    ADD_TASK,
    ANSWER_QUERY,
    ASK_CLARIFICATION,
    CLARIFICATION_CODES,
    JUST_CHAT,
    SAVE_NOTE,
    SET_GOAL,
    SET_GYM_SPLIT,
    UPDATE_TASK,
    ParsedIntent,
)

#: The Writer for the message being handled. Set by handle_message, so every
#: handler keeps the same (conn, intent, now) signature while the two that need
#: prose can still reach a client.
_WRITER: ContextVar[Writer | None] = ContextVar("writer", default=None)

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FULL_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _pretty_date(raw: str | None) -> str | None:
    """'2026-04-13' -> 'Mon Apr 13'. Returns the input unchanged if unparseable."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return f"{WEEKDAY_NAMES[parsed.weekday()]} {parsed:%b} {parsed.day}"
    return raw


def _pretty_time(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw
    return f"{_pretty_date(raw)} at {parsed:%H:%M}"


# ---------------------------------------------------------------------------
# Handlers — each returns the reply text
# ---------------------------------------------------------------------------


def _handle_add_task(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    title = intent.get("title")
    if not title:
        raise AssistantError(E.MISSING_FIELD, "That task needs a name.")

    course = intent.get("course")
    priority = int(intent.get("priority", 2))
    task_id = repo.add_task(
        conn,
        title=title,
        type=intent.get("type", "other"),
        course=course,
        due_date=intent.get("due_date"),
        tentative=bool(intent.get("tentative", False)),
        weight_pct=intent.get("weight_pct"),
        priority=priority,
        notes=intent.get("notes"),
        week=repo.week_number(conn, now.date()),
    )

    parts = [title]
    if course:
        parts.append(course)
    else:
        # Course tagging is required on every creation path; say so rather than
        # letting an untagged task quietly collide with another course later.
        parts.append("no course")
    if due := _pretty_date(intent.get("due_date")):
        parts.append(f"due {due}{' (tentative)' if intent.get('tentative') else ''}")
    if (weight := intent.get("weight_pct")) is not None:
        parts.append(f"{pct(weight)}%")
    parts.append(f"P{priority}")
    if notes := intent.get("notes"):
        parts.append(notes)

    logger.info("Saved task %s: %s", task_id, title)
    return "Saved — " + ", ".join(parts)


def _handle_add_reminder(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    text = intent.get("text")
    fire_at = intent.get("fire_at")

    if not fire_at:
        anchor = intent.get("anchor")
        if anchor:
            # Today's schedule is given to the parser, so reaching here means
            # the calendar genuinely doesn't settle it — say which part.
            raise AssistantError(
                E.MISSING_FIELD,
                f"Nothing on today's calendar tells me when {anchor!r} is. "
                "Give me a time and I'll set it.",
            )
        raise AssistantError(E.MISSING_FIELD, "When should I remind you?")

    repo.add_reminder(conn, text=text, fire_at=fire_at)
    return f"Reminder set — {_pretty_time(fire_at)}: {text}"


def _handle_set_gym_split(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    day = int(intent.get("day_of_week"))
    split = intent.get("split_name")
    repo.set_gym_split(conn, day_of_week=day, split_name=split)
    return f"{FULL_WEEKDAYS[day]} is {split}."


def _handle_update_task(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    query = intent.get("task_query")
    matches = repo.find_tasks(conn, query, course=intent.get("course"))

    if not matches:
        return f"Nothing on file matching {query!r}."
    if len(matches) > 1:
        listed = "; ".join(
            f"{row['title']} ({row['course'] or 'no course'}"
            + (f", due {_pretty_date(row['due_date'])}" if row["due_date"] else "")
            + ")"
            for row in matches[:4]
        )
        return f"Which one — {listed}?"

    task = matches[0]
    changes = {
        key: intent.get(key)
        for key in ("status", "due_date", "priority")
        if intent.get(key) is not None
    }
    if not changes:
        raise AssistantError(E.MISSING_FIELD, "What should I change about it?")

    repo.update_task(conn, task["id"], **changes)

    described = []
    if "status" in changes:
        described.append(str(changes["status"]).replace("_", " "))
    if "due_date" in changes:
        described.append(f"due {_pretty_date(changes['due_date'])}")
    if "priority" in changes:
        described.append(f"P{changes['priority']}")
    return f"{task['title']} — {', '.join(described)}."


def _handle_set_goal(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    text = intent.get("text")
    tier = intent.get("tier", "daily")
    repo.set_goal(conn, text=text, tier=tier, now=now)
    return f"{tier.capitalize()} goal — {text}"


def _handle_save_note(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    text = intent.get("text")
    # Normalise before both the write and the receipt, so the receipt shows what
    # actually landed rather than what Claude proposed.
    tags = [t.strip().lower() for t in (intent.get("tags") or []) if t.strip()]
    repo.save_note(conn, text=text, tags=tags)
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return f"Noted{suffix}."


def _handle_answer_query(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    writer = _WRITER.get()
    if writer is None:
        return "I can't answer questions right now — no Claude client is configured."
    return query.answer(conn, intent.get("question", ""), writer, now=now)


def _handle_just_chat(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    # TODO(Session 10): conversational replies belong with the evening check-in,
    # which is where a second generation call earns its cost.
    return "Noted. Conversation isn't wired up yet — I can save and track things."


def _handle_ask_clarification(
    conn: sqlite3.Connection, intent: ParsedIntent, now: datetime
) -> str:
    reason = intent.get("reason", "intent_unclear")
    code = CLARIFICATION_CODES.get(reason, E.INTENT_UNCLEAR)
    logger.info("Clarification requested [%s]: %s", code, intent.get("question"))
    return intent.get(
        "question", "Not sure what to do with that — task, reminder, or just chatting?"
    )


HANDLERS = {
    ADD_TASK: _handle_add_task,
    ADD_REMINDER: _handle_add_reminder,
    SET_GYM_SPLIT: _handle_set_gym_split,
    UPDATE_TASK: _handle_update_task,
    SET_GOAL: _handle_set_goal,
    SAVE_NOTE: _handle_save_note,
    ANSWER_QUERY: _handle_answer_query,
    JUST_CHAT: _handle_just_chat,
    ASK_CLARIFICATION: _handle_ask_clarification,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_context(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    upcoming_events: list[tuple[str, str]] | None = None,
) -> PromptContext:
    from db.database import get_config

    return PromptContext(
        now=now,
        timezone=get_config(conn, "timezone", "America/Toronto"),
        courses=repo.course_codes(conn),
        week_number=repo.week_number(conn, now.date()),
        upcoming_events=upcoming_events or [],
    )


def handle_message(
    conn: sqlite3.Connection,
    classifier: Classifier,
    message: str,
    *,
    now: datetime | None = None,
    writer: Writer | None = None,
    upcoming_events: list[tuple[str, str]] | None = None,
) -> str:
    """Classify one message, run its handler, return the reply text.

    Raises ``AssistantError`` on any failure; the caller decides how to surface
    it (``bot.main.on_error`` does, for Telegram).
    """
    moment = now or datetime.now()
    token = _WRITER.set(writer)
    try:
        intent = classifier.classify(
            message, build_context(conn, moment, upcoming_events=upcoming_events)
        )
    except Exception:
        _WRITER.reset(token)
        raise

    handler = HANDLERS.get(intent.name)
    if handler is None:
        raise AssistantError(
            E.INTENT_UNCLEAR,
            "I got a response I don't know how to act on.",
            trigger=f"{intent.name}: {message}",
        )

    logger.info("Intent %s for message %r", intent.name, message)
    try:
        return handler(conn, intent, moment)
    finally:
        _WRITER.reset(token)
