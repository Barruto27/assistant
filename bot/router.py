"""Intent dispatch and receipts (plan Section 4).

``handle_message`` is a plain function taking a connection and a classifier, so
the whole pipeline is testable without Telegram, a network, or an API key.

Receipts are built here in Python rather than by a second Claude call: they are
structured confirmations of what landed in specific columns, they must be
identical every time so a misparse is obvious at a glance, and one message
should cost one API call (principle 1).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from bot import checkin as checkin_mod, clarify, query, repository as repo
from contextvars import ContextVar
from typing import Any

from bot.claude_client import Classifier, PromptContext, Writer
from bot.errors import AssistantError, E, log_error, logger
from bot.formatting import pct
from bot.voice import VOICE
from bot.intents import (
    ADD_REMINDER,
    ADD_TASK,
    ANSWER_QUERY,
    ASK_CLARIFICATION,
    CHECK_EMAIL,
    CHECKIN_REPLY,
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
#: Today's remaining events, so an unresolved anchor can be settled here
#: rather than bounced back to Kaan.
_EVENTS: ContextVar[list] = ContextVar("events", default=[])
#: Reads the mailbox and returns (flagged, scanned). Set by bot.main, which
#: owns the IMAP credentials; None when email was never configured.
_EMAIL: ContextVar[Any] = ContextVar("email_lookup", default=None)

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


def _ask(
    question: str,
    code: str,
    *,
    conn: sqlite3.Connection | None = None,
    intent: ParsedIntent | None = None,
    now: datetime | None = None,
) -> str:
    """A clarifying question, logged but not dressed up as a failure.

    Plan Section 4 wants a missing required field to produce one targeted
    question; Section 8's user-facing codes are for things that actually broke.
    These were using the second to do the first, so "remind me to buy milk"
    came back as "When should I remind you? (E103)".

    Given the intent it could not complete, it also writes the question down,
    so the next message is read as the answer rather than as a fresh request.
    """
    logger.info("[%s] Asking: %s", code, question)
    if conn is not None and intent is not None:
        clarify.remember(
            conn,
            intent=intent.name,
            fields=dict(intent.fields),
            question=question,
            now=now or datetime.now(),
        )
    return question


def _handle_add_task(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    title = intent.get("title")
    if not title:
        return _ask(
            "What should I call it?", E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )

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
            resolved = _resolve_anchor(now)
            if resolved is None:
                return _ask(
                    f"Nothing left on today's calendar tells me when {anchor!r} "
                    "is. Give me a time and I'll set it.",
                    E.MISSING_FIELD,
                    conn=conn, intent=intent, now=now,
                )
            fire_at, after_what = resolved
            repo.add_reminder(conn, text=text, fire_at=fire_at)
            return (
                f"Reminder set — {_pretty_time(fire_at)}, after {after_what}: {text}"
            )
        return _ask(
            "When should I remind you — tonight, tomorrow, or a specific time?",
            E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )

    repo.add_reminder(conn, text=text, fire_at=fire_at)
    return f"Reminder set — {_pretty_time(fire_at)}: {text}"


def _resolve_anchor(now: datetime) -> tuple[str, str] | None:
    """Turn "after my next class" into a time, from today's remaining events.

    The parser is given the schedule and asked to compute this itself, but a
    prompt is guidance and not a guarantee — it once handed back the anchor
    "after Memory class ends at 21:00", which names the answer while refusing
    to state it. Resolving here means the reminder gets set either way.

    Fifteen minutes after the event ends: "after my next class" means once he
    is out, not the instant it finishes.
    """
    events = _EVENTS.get()
    if not events:
        return None

    span, summary = events[0]
    end = span.split("-")[-1].strip()
    try:
        hour, minute = (int(part) for part in end.split(":", 1))
    except ValueError:
        return None

    fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
        minutes=15
    )
    return fire.strftime("%Y-%m-%d %H:%M:%S"), summary


def _handle_set_gym_split(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    split = intent.get("split_name")
    if not split:
        return _ask(
            "Which split — push, pull, legs, rest?", E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )

    raw = intent.get("day_of_week")
    try:
        day = int(raw)
    except (TypeError, ValueError):
        # Used to be a bare int(None), which is a TypeError rather than an
        # AssistantError and so escaped as "Something broke on my end."
        return _ask(
            f"Which day is {split} on?", E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )
    if not 0 <= day <= 6:
        return _ask(
            f"Which day is {split} on? I read {raw!r}, which isn't a weekday.",
            E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )

    repo.set_gym_split(conn, day_of_week=day, split_name=split)
    return f"{FULL_WEEKDAYS[day]} is {split}."


#: "iClicker Participation (3/11)" -> the instalment number and the series.
_INSTALMENT = re.compile(r"^(?P<base>.*?)\s*\((?P<n>\d+)\s*/\s*(?P<of>\d+)\)\s*$")


def _series_key(row: sqlite3.Row) -> tuple[str, str] | None:
    """(course, series title) for one instalment of weekly work, else None."""
    match = _INSTALMENT.match(row["title"] or "")
    if not match:
        return None
    return (row["course"] or "", match.group("base").strip().lower())


def _instalment_number(row: sqlite3.Row) -> int:
    match = _INSTALMENT.match(row["title"] or "")
    return int(match.group("n")) if match else 0


def _narrow(matches: list[sqlite3.Row], status: str | None) -> list[sqlite3.Row]:
    """Drop what the report cannot be about, then collapse an ordered series.

    Called only when several rows matched. Returns one row when the data
    settles it, and the remaining candidates when it does not.
    """
    # A row already in the reported state is not what he is reporting.
    if status:
        open_rows = [row for row in matches if row["status"] != status]
        if open_rows:
            matches = open_rows
    if len(matches) == 1:
        return matches

    # One series of numbered instalments: the one he means is the earliest
    # still outstanding, because that is the next one he can have earned.
    keys = {_series_key(row) for row in matches}
    if len(keys) == 1 and None not in keys:
        return [min(matches, key=_instalment_number)]

    return matches


def _ask_which(matches: list[sqlite3.Row]) -> str:
    """The disambiguating question, asked at the level that is actually unclear.

    When the candidates are several weeks of one series in two courses, the
    open question is the course, not the week - so ask that.
    """
    courses = {row["course"] for row in matches if row["course"]}
    series = {_series_key(row) for row in matches}
    if len(courses) > 1 and len(series) == len(courses) and None not in series:
        return "Which course — " + " or ".join(sorted(courses)) + "?"

    listed = "; ".join(
        f"{row['title']} ({row['course'] or 'no course'}"
        + (f", due {_pretty_date(row['due_date'])}" if row["due_date"] else "")
        + ")"
        for row in matches[:4]
    )
    return f"Which one — {listed}?"


def _handle_update_task(conn: sqlite3.Connection, intent: ParsedIntent, now: datetime) -> str:
    query = intent.get("task_query")
    matches = repo.find_tasks(conn, query, course=intent.get("course"))

    if not matches:
        return f"Nothing on file matching {query!r}."
    if len(matches) > 1:
        matches = _narrow(matches, intent.get("status"))
    if len(matches) > 1:
        return _ask_which(matches)

    task = matches[0]
    changes = {
        key: intent.get(key)
        for key in ("status", "due_date", "priority")
        if intent.get(key) is not None
    }
    if not changes:
        return _ask(
            f"What should I change about {task['title']} — the date, the "
            "priority, or is it done?",
            E.MISSING_FIELD,
            conn=conn, intent=intent, now=now,
        )

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
    """Reply in conversation, still grounded in what's actually on file.

    Answered through the same query path as a direct question, because most
    "chat" here is really about his term — "feeling behind", "is this week
    bad" — and a reply that ignores the data would be worse than the
    placeholder it replaces. The same rule applies: only what's on file, and
    say when something isn't.
    """
    writer = _WRITER.get()
    if writer is None:
        return "I hear you. I can't hold a conversation without a Claude client, though."
    return query.answer(conn, intent.get("message", "") or "(just talking)", writer,
                        now=now, conversational=True)


def _handle_checkin_reply(
    conn: sqlite3.Connection, intent: ParsedIntent, now: datetime
) -> str:
    """Apply the evening check-in answer.

    A second, richer call than the classification: the first decides this is a
    check-in reply, this one works out which specific rows he meant. Ids come
    from the check-in itself, so applying it is a lookup — matching the wrong
    row here would quietly corrupt his record of the term.
    """
    writer = _WRITER.get()
    if writer is None:
        return "Got it, but I can't record that right now — no Claude client."

    # Everything tonight offered, not just what is unanswered: he may be
    # correcting something he already answered for.
    offered = checkin_mod.addressable(conn, now)
    if not offered:
        return "Noted."

    context = checkin_mod.gather(conn, now)
    payload = writer.call_tool(
        checkin_mod.REPLY_SYSTEM.format(
            voice=VOICE, data=checkin_mod.render_context(context)
        ),
        intent.get("summary", ""),
        checkin_mod.REPLY_TOOL,
        max_tokens=900,
    )
    result = checkin_mod.parse_reply(payload, offered)
    counts = checkin_mod.apply(conn, result, now=now)

    # Retire only what he actually answered for. The rest stays offered, so a
    # follow-up a minute later is still a check-in reply and not small talk.
    answered = {
        *result.done, *result.started, *result.attended,
        *(task_id for task_id, _ in result.not_done),
    }
    checkin_mod.resolve(conn, answered, now=now)

    logger.info("Check-in reply applied: %s", counts)
    if counts.get("refused"):
        # Never let the composed acknowledgement claim a write that was
        # refused; that is the false-confirmation failure in miniature.
        return (
            "I didn't follow that one — an attendance mark is either earned or "
            "not, so I can't mark one as started. Say which thing you mean and "
            "I'll fix it."
        )
    if not answered:
        # Nothing matched a row he was offered, so nothing was written. Saying
        # "Recorded." here is how the bot came to confirm writes it never made.
        return result.reply or (
            "Nothing there matched what I asked about, so I haven't changed "
            "anything. Name the task and I'll mark it."
        )
    return result.reply or "Recorded."


def _handle_check_email(
    conn: sqlite3.Connection, intent: ParsedIntent, now: datetime
) -> str:
    """Read the mailbox now and say what in it matters.

    The same fetch and the same flagging the morning brief uses, so the two
    cannot disagree. Read-only: nothing here becomes a task, which the reply
    says outright rather than leaving him to assume either way.
    """
    return run_email_check(_EMAIL.get(), intent.get("course"), conn=conn)


def run_email_check(
    lookup: Any, course: str | None = None, *, conn: sqlite3.Connection | None = None
) -> str:
    """Read the mailbox and describe it. Shared by check_email and /email.

    What it finds is recorded, so mail he goes looking for himself reaches the
    evening check-in the same way mail found by the brief does. Without that,
    checking manually would quietly opt an email out of ever being raised
    again.
    """
    if lookup is None:
        return (
            "I can't read your email — no mailbox is set up for me. That's "
            "GMAIL_IMAP_USER and GMAIL_APP_PASSWORD in .env."
        )

    flagged, scanned = lookup()
    if conn is not None and flagged:
        repo.remember_flagged(conn, flagged)
    if scanned is None:
        return (
            "I couldn't get into the mailbox just now. The morning brief will "
            "try again at 07:30."
        )

    if course:
        flagged = [
            item for item in flagged
            if not item.course or item.course.lower() == str(course).lower()
        ]

    if not flagged:
        about = f" about {course}" if course else ""
        if not scanned:
            return f"No course mail{about} in the last few days."
        return (
            f"Read {scanned} message{'s' if scanned != 1 else ''} from the "
            f"allowlist. Nothing{about} needs anything from you."
        )

    lines = [f"From your email — nothing saved, tell me if you want any of it kept:"]
    for item in flagged:
        bits = []
        # The summary often names the course itself, and "PSYC 3265 - Memory
        # (PSYC 3265) first class..." reads like a bug.
        if item.course and item.course.lower() not in item.summary.lower():
            bits.append(item.course)
        bits.append(item.summary)
        if item.new_date:
            bits.append(f"new date {_pretty_date(item.new_date)}")
        lines.append("- " + " — ".join(bits))
    return "\n".join(lines)


def _handle_ask_clarification(
    conn: sqlite3.Connection, intent: ParsedIntent, now: datetime
) -> str:
    reason = intent.get("reason", "intent_unclear")
    code = CLARIFICATION_CODES.get(reason, E.INTENT_UNCLEAR)
    logger.info("Clarification requested [%s]: %s", code, intent.get("question"))
    clarify.remember(
        conn,
        intent=ASK_CLARIFICATION,
        fields={},
        question=str(intent.get("question", "")),
        now=now,
    )
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
    CHECKIN_REPLY: _handle_checkin_reply,
    ASK_CLARIFICATION: _handle_ask_clarification,
    CHECK_EMAIL: _handle_check_email,
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
        checkin_pending=checkin_mod.pending(conn, now) is not None,
        checkin_offered=checkin_mod.offered(conn, now),
        checkin_answered=checkin_mod.answered_already(conn, now),
        open_question=clarify.pending(conn, now),
    )


def handle_message(
    conn: sqlite3.Connection,
    classifier: Classifier,
    message: str,
    *,
    now: datetime | None = None,
    writer: Writer | None = None,
    upcoming_events: list[tuple[str, str]] | None = None,
    email_lookup: Any = None,
) -> str:
    """Classify one message, run its handler, return the reply text.

    Raises ``AssistantError`` on any failure; the caller decides how to surface
    it (``bot.main.on_error`` does, for Telegram).
    """
    moment = now or datetime.now()
    token = _WRITER.set(writer)
    events_token = _EVENTS.set(list(upcoming_events or []))
    email_token = _EMAIL.set(email_lookup)
    try:
        intents = classifier.classify(
            message, build_context(conn, moment, upcoming_events=upcoming_events)
        )
    except Exception:
        _WRITER.reset(token)
        _EVENTS.reset(events_token)
        _EMAIL.reset(email_token)
        raise

    # Fold in what was already known before the question was asked, then close
    # it: whatever happens next, this exchange is over. A handler that asks
    # again will write a fresh one.
    open_question = clarify.pending(conn, moment)
    if open_question is not None:
        intents = [
            ParsedIntent(
                name=i.name,
                fields=clarify.merge(open_question, i.name, dict(i.fields)),
            )
            for i in intents
        ]
        clarify.clear(conn)

    for intent in intents:
        if intent.name not in HANDLERS:
            _WRITER.reset(token)
            _EVENTS.reset(events_token)
            _EMAIL.reset(email_token)
            raise AssistantError(
                E.INTENT_UNCLEAR,
                "I got a response I don't know how to act on.",
                trigger=f"{intent.name}: {message}",
            )

    logger.info(
        "Intent(s) %s for message %r", ", ".join(i.name for i in intents), message
    )
    try:
        return _run_all(conn, intents, moment, message)
    finally:
        _WRITER.reset(token)
        _EVENTS.reset(events_token)
        _EMAIL.reset(email_token)


def _run_all(
    conn: sqlite3.Connection,
    intents: list[ParsedIntent],
    moment: datetime,
    message: str,
) -> str:
    """Run each intent in turn and stitch the receipts into one reply.

    One failure does not discard the rest. "Remind me today and tomorrow to
    upload the forms" is two writes, and losing the second silently — which is
    what taking only the first intent used to do — is the failure mode worth
    engineering against. If one leg fails he sees which, and the other still
    happened.
    """
    if len(intents) == 1:
        return HANDLERS[intents[0].name](conn, intents[0], moment)

    replies: list[str] = []
    failures = 0
    for intent in intents:
        try:
            reply = HANDLERS[intent.name](conn, intent, moment)
        except AssistantError as err:
            failures += 1
            log_error(err)
            reply = err.user_message()
        except Exception as exc:  # noqa: BLE001
            # Anything a handler failed to anticipate. Caught here so it costs
            # one line of the reply rather than the whole message: a bare
            # int(None) in the gym handler used to discard every other
            # instruction alongside itself.
            failures += 1
            wrapped = AssistantError(
                E.UNEXPECTED,
                "Something broke on my end.",
                cause=exc,
                trigger=f"{intent.name}: {message}",
            )
            log_error(wrapped)
            reply = wrapped.user_message()
        if reply and reply.strip():
            replies.append(reply.strip())

    if failures == len(intents):
        raise AssistantError(
            E.INTENT_UNCLEAR,
            "None of that went through.",
            trigger=message,
        )
    return "\n".join(replies)
