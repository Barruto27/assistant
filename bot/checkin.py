"""Evening check-in (plan Section 10).

Nothing else in the system marks work done. Without this every task stays
``not_started`` for ever, and each morning the brief drifts further from what
is actually true until it is describing someone else's term.

The shape the plan asks for, and the reason for each part:

*   **One message, one reply.** The check-in states what the system *believes*
    happened, as a light check rather than a demand. Kaan answers however he
    likes, in one message, and that is the end of it.
*   **A guess, not an interrogation.** Listing what it thinks happened is much
    easier to correct than answering "what did you get done today?" from a
    blank page.
*   **No guilt.** Something not done is information. The reply records the
    reason if he gives one and does not ask for one if he doesn't.

Task ids travel in both directions, so applying the reply is a lookup rather
than a fuzzy match on titles — the one place in this system where getting the
wrong row would quietly corrupt his record of the term.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from bot.errors import AssistantError, E, logger
from bot.voice import VOICE
from bot import repository as repo
from db.database import get_config, set_config, transaction

#: How long after the check-in a plain message is still read as a reply to it.
REPLY_WINDOW_HOURS = 14


@dataclass
class CheckinContext:
    """What the check-in believes about today."""

    now: datetime
    due_today: list[sqlite3.Row] = field(default_factory=list)
    in_progress: list[sqlite3.Row] = field(default_factory=list)
    attendance_today: list[sqlite3.Row] = field(default_factory=list)
    due_tomorrow: list[sqlite3.Row] = field(default_factory=list)
    daily_goal: sqlite3.Row | None = None
    #: True when a daily goal is already set for tomorrow, so the check-in does
    #: not ask for one. Section 11 wants it asked for when it is missing, and
    #: not otherwise.
    tomorrow_goal_set: bool = False
    #: Mail flagged since the last check-in that he has not been asked about.
    #: Never a task - Section 6 keeps him in charge of what gets written - so
    #: the check-in asks whether it needs to become one.
    flagged_emails: list[sqlite3.Row] = field(default_factory=list)
    #: Rows offered tonight that he has already answered for. They are no
    #: longer outstanding, so nothing should ask about them again - but a
    #: correction is about one of these, and the call that reads his reply
    #: cannot resolve "wait no I didn't" against a row it cannot see.
    answered_tonight: list[sqlite3.Row] = field(default_factory=list)


def gather(conn: sqlite3.Connection, now: datetime) -> CheckinContext:
    today = now.date().isoformat()
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    context = CheckinContext(now=now)

    context.due_today = conn.execute(
        "SELECT id, title, course, weight_pct, priority FROM tasks "
        "WHERE status IN ('not_started', 'in_progress') AND due_date = ? "
        "AND attendance = 0 ORDER BY priority",
        (today,),
    ).fetchall()
    context.attendance_today = conn.execute(
        "SELECT id, title, course FROM tasks "
        "WHERE status IN ('not_started', 'in_progress') AND due_date = ? "
        "AND attendance = 1",
        (today,),
    ).fetchall()
    context.in_progress = conn.execute(
        "SELECT id, title, course, due_date FROM tasks WHERE status = 'in_progress' "
        "AND (due_date IS NULL OR due_date != ?) ORDER BY COALESCE(due_date, '9999')",
        (today,),
    ).fetchall()
    context.flagged_emails = repo.outstanding_flagged(conn)

    offered_ids = addressable(conn, now)
    settled = [i for i in _ordered_ids(conn, _ANSWERED) if i in offered_ids]
    if settled:
        placeholders = ",".join("?" for _ in settled)
        rows = {
            row["id"]: row
            for row in conn.execute(
                f"SELECT id, title, course, status FROM tasks "
                f"WHERE id IN ({placeholders})",
                tuple(settled),
            )
        }
        context.answered_tonight = [rows[i] for i in settled if i in rows]
    context.due_tomorrow = conn.execute(
        "SELECT id, title, course, weight_pct FROM tasks "
        "WHERE status IN ('not_started', 'in_progress') AND due_date = ? "
        "AND attendance = 0",
        (tomorrow,),
    ).fetchall()
    rows = conn.execute(
        "SELECT id, text FROM goals WHERE tier = 'daily' AND status = 'active' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchall()
    context.daily_goal = rows[0] if rows else None

    # A daily goal counts as tomorrow's if it has not expired by then. Today's
    # goal, set this morning and expiring tonight, does not.
    context.tomorrow_goal_set = (
        conn.execute(
            "SELECT 1 FROM goals WHERE tier = 'daily' AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1",
            (tomorrow,),
        ).fetchone()
        is not None
    )
    return context


def has_anything_to_ask(context: CheckinContext) -> bool:
    """Whether tonight is worth a message at all.

    A check-in on a day with nothing on it is a demand dressed as a question.
    """
    return bool(
        context.due_today
        or context.in_progress
        or context.attendance_today
        or context.daily_goal
        # A quiet day with unanswered mail is still worth a message: this is
        # the last chance to raise it before the scan window drops it.
        or context.flagged_emails
    )


PROMPT_SYSTEM = """\
{voice}

Write Kaan's evening check-in. Below is what the system believes about today.

State that belief plainly and briefly, then leave it open. He answers in one
message, however he likes — this is a light check, not a form.

- Lead with what it looks like was on today, framed as a guess he can correct.
- If a lecture with an attendance mark was on, ask whether he made it, once,
  without weighting the question.
- If something is due tomorrow that would be easier started tonight, mention it
  in a few words. Do not press.
- Never ask why something didn't happen. If he says, fine; if not, that's fine.
- If the facts say NO GOAL SET FOR TOMORROW, close by asking what he wants
  tomorrow to be about. Once, in a short question he is free to ignore - a
  daily goal he was nagged into is worth nothing. Say nothing about goals at
  all when one is already set.
- No cheerleading, no disappointment. Two or three short lines.

{data}
"""


def render_context(context: CheckinContext) -> str:
    lines = [f"DATE: {context.now:%A %Y-%m-%d}"]

    def block(title: str, rows: list[sqlite3.Row], with_id: bool = True) -> None:
        if rows:
            lines.append("")
            lines.append(f"{title}:")
            for row in rows:
                label = f"[{row['id']}] " if with_id else ""
                bits = [row["title"]]
                if "course" in row.keys() and row["course"]:
                    bits.append(row["course"])
                lines.append(f"- {label}" + " | ".join(bits))

    block("WAS DUE TODAY", context.due_today)
    block("ATTENDANCE MARK TODAY (only counts if he was there)", context.attendance_today)
    block("ALREADY MARKED IN PROGRESS", context.in_progress)
    block("DUE TOMORROW", context.due_tomorrow)

    if context.answered_tonight:
        lines.append("")
        lines.append(
            "ALREADY ANSWERED FOR TONIGHT, oldest first (do not ask about these "
            "again; they are here so a correction can name one):"
        )
        for position, row in enumerate(context.answered_tonight):
            bits = [row["title"]]
            if row["course"]:
                bits.append(row["course"])
            last = position == len(context.answered_tonight) - 1
            marker = "  <- the last thing he told you about" if last else ""
            lines.append(
                f"- [{row['id']}] " + " | ".join(bits) + f" -> {row['status']}{marker}"
            )

    if context.flagged_emails:
        lines.append("")
        lines.append(
            "FROM HIS EMAIL, NOT SAVED AND NOT A TASK (ask whether he wants any "
            "of it kept; do not imply it is already tracked, and do not give it "
            "an id - these are not rows he can mark done):"
        )
        for row in context.flagged_emails:
            bits = []
            if row["course"]:
                bits.append(row["course"])
            bits.append(row["summary"])
            if row["new_date"]:
                bits.append(f"date {row['new_date']}")
            lines.append("- " + " | ".join(bits))

    if context.daily_goal:
        lines.append("")
        lines.append(f"TODAY'S GOAL: {context.daily_goal['text']}")
    if not context.tomorrow_goal_set:
        lines.append("")
        lines.append("NO GOAL SET FOR TOMORROW")
    return "\n".join(lines)


def compose(context: CheckinContext, writer: Any) -> str:
    return writer.compose(
        PROMPT_SYSTEM.format(voice=VOICE, data=render_context(context)),
        "Write tonight's check-in.",
        max_tokens=600,
    )


# ---------------------------------------------------------------------------
# Applying the reply
# ---------------------------------------------------------------------------

REPLY_TOOL: dict[str, Any] = {
    "name": "record_checkin",
    "description": "Record what Kaan said about his day.",
    "input_schema": {
        "type": "object",
        "properties": {
            "done": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Task ids he finished. Only ids from the list given.",
            },
            "started": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Task ids he made a start on but did not finish.",
            },
            "not_done": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "reason": {
                            "type": "string",
                            "description": "Only if he actually gave one. Never invent one.",
                        },
                    },
                    "required": ["task_id"],
                },
            },
            "attended": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Ids of attendance marks he says he was there for.",
            },
            "goal_met": {
                "type": "boolean",
                "description": "Whether he met today's goal, if he said either way.",
            },
            "tomorrow": {
                "type": "string",
                "description": "What he says he wants to do tomorrow, if anything.",
            },
            "reply": {
                "type": "string",
                "description": (
                    "A short, plain acknowledgement to send back. State what was "
                    "recorded. No praise, no commiseration, no advice unless he "
                    "asked. One or two lines."
                ),
            },
        },
        "required": ["reply"],
    },
}

REPLY_SYSTEM = """\
{voice}

Kaan is answering his evening check-in. Record what he actually said.

Only use task ids from the list below. If he mentions something not on it,
leave it out of the id fields rather than guessing which row he meant — a wrong
id quietly corrupts his record of the term.

Match on what the item actually is, not on it being the only row left. Most of
what he says in an evening will be about things that are not on this list at
all — a reminder, an errand, something he just did. The right answer then is to
record nothing and say so. Attaching his reason for skipping one thing to an
unrelated row is worse than leaving the list untouched.

Record a reason only if he gave one. Do not infer one, and do not ask for one.
Something not done is information, not a failing.

He is allowed to take back what he said a minute ago. If a row appears under
ALREADY ANSWERED FOR TONIGHT and this message contradicts it - "wait no I
didn't", "actually I only started it", "sorry, I meant..." - the new answer is
about that row and replaces the old one. Do not move the correction onto
whatever is still outstanding; that records two wrong things instead of one.

A message that opens by retracting - "wait", "no", "actually", "sorry" - and
then names no row is about the last thing he told you about, which is marked
in that list. It is not about whatever else happens to be open.

{data}
"""


@dataclass
class CheckinResult:
    done: list[int] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    not_done: list[tuple[int, str | None]] = field(default_factory=list)
    attended: list[int] = field(default_factory=list)
    goal_met: bool | None = None
    tomorrow: str | None = None
    reply: str = ""


def parse_reply(payload: dict[str, Any], valid_ids: set[int]) -> CheckinResult:
    """Keep only ids that were actually offered."""

    def ids(key: str) -> list[int]:
        return [int(i) for i in payload.get(key, []) if int(i) in valid_ids]

    not_done = []
    for entry in payload.get("not_done", []):
        try:
            task_id = int(entry["task_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if task_id in valid_ids:
            reason = entry.get("reason")
            not_done.append((task_id, str(reason).strip() if reason else None))

    return CheckinResult(
        done=ids("done"),
        started=ids("started"),
        not_done=not_done,
        attended=ids("attended"),
        goal_met=payload.get("goal_met"),
        tomorrow=(str(payload["tomorrow"]).strip() if payload.get("tomorrow") else None),
        reply=str(payload.get("reply", "")).strip(),
    )


def apply(
    conn: sqlite3.Connection, result: CheckinResult, *, now: datetime
) -> dict[str, int]:
    """Write the reply to the database. Nothing here is destructive."""
    counts = {"done": 0, "started": 0, "not_done": 0, "attended": 0, "refused": 0}
    try:
        with transaction(conn):
            for task_id in result.done + result.attended:
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,)
                )
            counts["done"] = len(result.done)
            counts["attended"] = len(result.attended)

            # An attendance mark has no half-way state, so "started" against
            # one means the wrong row was picked. Refusing beats recording a
            # status he never reported against a lecture he never mentioned.
            startable, misread = [], []
            for task_id in result.started:
                row = conn.execute(
                    "SELECT attendance FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                (misread if row and row["attendance"] else startable).append(task_id)
            if misread:
                logger.warning(
                    "Refused to mark attendance mark(s) %s as started; an "
                    "attendance mark is not something you can be part-way "
                    "through, so this is a misidentified row",
                    misread,
                )
            for task_id in startable:
                conn.execute(
                    "UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,)
                )
            counts["started"] = len(startable)
            counts["refused"] = len(misread)

            # Not-done stays open. The backlog rule decides when it stops being
            # shown; the check-in only records the reason he gave.
            for task_id, reason in result.not_done:
                if reason:
                    conn.execute(
                        "UPDATE tasks SET notes = COALESCE(notes || ' | ', '') || ? "
                        "WHERE id = ?",
                        (f"{now:%b %d}: {reason}", task_id),
                    )
            counts["not_done"] = len(result.not_done)

            if result.goal_met is not None:
                row = conn.execute(
                    "SELECT id FROM goals WHERE tier = 'daily' AND status = 'active' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE goals SET status = ?, last_progress_at = ? WHERE id = ?",
                        (
                            "done" if result.goal_met else "active",
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            row["id"],
                        ),
                    )

            if result.tomorrow:
                conn.execute(
                    "INSERT INTO notes (text, tags) VALUES (?, 'checkin,tomorrow')",
                    (f"Plan for tomorrow ({now:%b %d}): {result.tomorrow}",),
                )
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't record the check-in.", cause=exc
        ) from exc

    logger.info("Check-in applied: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Pending state
# ---------------------------------------------------------------------------


#: Config keys. The offered set is written once and left alone; answering
#: moves ids into the second list rather than out of the first, so a
#: correction a minute later can still reach a row he has answered for.
_SENT_AT = "checkin_sent_at"
_OFFERED = "checkin_task_ids"
_ANSWERED = "checkin_answered_ids"


def mark_sent(conn: sqlite3.Connection, now: datetime, ids: list[int]) -> None:
    """Remember that a check-in is awaiting an answer, and which rows it offered."""
    set_config(conn, _SENT_AT, now.strftime("%Y-%m-%d %H:%M:%S"))
    set_config(conn, _OFFERED, ",".join(str(i) for i in ids))
    set_config(conn, _ANSWERED, "")


def _ordered_ids(conn: sqlite3.Connection, key: str) -> list[int]:
    raw = get_config(conn, key, "") or ""
    return [int(i) for i in raw.split(",") if i.strip().isdigit()]


def _ids(conn: sqlite3.Connection, key: str) -> set[int]:
    return set(_ordered_ids(conn, key))


def _in_window(conn: sqlite3.Connection, now: datetime) -> bool:
    raw = get_config(conn, _SENT_AT, "")
    if not raw:
        return False
    try:
        sent = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (now.replace(tzinfo=None) - sent) <= timedelta(hours=REPLY_WINDOW_HOURS)


def addressable(conn: sqlite3.Connection, now: datetime) -> set[int]:
    """Every row tonight's check-in offered, answered or not.

    What he is allowed to talk about, which is not the same as what still
    needs asking: a correction is about something he has already answered for.
    """
    return _ids(conn, _OFFERED) if _in_window(conn, now) else set()


def pending(conn: sqlite3.Connection, now: datetime) -> set[int] | None:
    """The ids still waiting on an answer, or None if the check-in is done."""
    if not _in_window(conn, now):
        return None
    outstanding = _ids(conn, _OFFERED) - _ids(conn, _ANSWERED)
    return outstanding or None


def offered(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """The still-open rows in words, for the parser to match his wording to.

    A set of ids tells the parser nothing. "Made it to the lecture" only routes
    correctly if it can see that one of the things it asked about is an
    attendance mark for a lecture today.
    """
    ids = pending(conn, now)
    if not ids:
        return []
    return _describe(conn, ids)


def answered_already(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Rows he has answered for tonight, in words, so a correction can land."""
    ids = addressable(conn, now) & _ids(conn, _ANSWERED)
    return _describe(conn, ids) if ids else []


def _describe(conn: sqlite3.Connection, ids: set[int]) -> list[str]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT title, course, attendance FROM tasks WHERE id IN ({placeholders})",
        tuple(sorted(ids)),
    ).fetchall()
    described = []
    for row in rows:
        label = f"{row['course']} {row['title']}" if row["course"] else row["title"]
        if row["attendance"]:
            label += " (an attendance mark - being there is the whole of it)"
        described.append(label)
    return described


def clear(conn: sqlite3.Connection) -> None:
    set_config(conn, _SENT_AT, "")
    set_config(conn, _OFFERED, "")
    set_config(conn, _ANSWERED, "")


def resolve(conn: sqlite3.Connection, answered: set[int], *, now: datetime) -> None:
    """Retire the rows he just answered for, and leave the rest offered.

    An evening answer arrives in pieces — "skipped the GED thing", then a
    minute later "iClicker done in class". Closing the check-in on the first
    message sent the second one down the read-only chat path, which replied
    that both attendance marks were recorded and recorded neither.

    Answered rows are recorded rather than removed. Dropping them meant a
    correction - "wait no I didn't, I started it" - found only whatever was
    left offered and applied itself to that instead.
    """
    # Appended, not sorted: the order he answered in is what makes "wait, no"
    # resolvable, and sorting by id discards it.
    order = _ordered_ids(conn, _ANSWERED)
    for task_id in sorted(answered):
        if task_id in order:
            order.remove(task_id)
        order.append(task_id)
    set_config(conn, _ANSWERED, ",".join(str(i) for i in order))
