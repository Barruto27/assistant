"""Answering questions about Kaan's own data (plan Section 4, answer_query).

The rule this module exists to enforce: **the bot knows exactly what it was
told, and nothing else.** A syllabus import is a reading of a PDF, not a
transcription; a course with no syllabus on file is a course it knows nothing
about. Answering "what's the weight of the final?" with a plausible number
would be worse than useless — it would be trusted.

So the answer is assembled in two strictly separated steps. ``gather`` pulls
rows from the database and nothing else. ``answer`` hands those rows to Claude
under instructions to use only what it was given, name what is missing, and
never fill a gap with something reasonable-sounding.

Where the extraction itself was unsure, ``courses.verify_notes`` carries the
detail, and it travels with any answer about that course.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from bot.claude_client import Writer
from bot.errors import AssistantError, E, logger
from bot.formatting import pct
from bot.voice import VOICE

#: Bounds the context so one question can't pull an unbounded prompt. Sized to
#: hold a full course load for a whole term: four courses of weekly work plus
#: their deadlines runs to roughly a hundred rows.
MAX_TASKS = 200


@dataclass
class QueryContext:
    """Everything the answer may draw on. Nothing else exists, as far as it knows."""

    now: datetime
    question: str
    week_number: int | None = None
    courses: list[sqlite3.Row] = field(default_factory=list)
    tasks: list[sqlite3.Row] = field(default_factory=list)
    goals: list[sqlite3.Row] = field(default_factory=list)
    reminders: list[sqlite3.Row] = field(default_factory=list)
    gym: list[sqlite3.Row] = field(default_factory=list)
    topics: list[sqlite3.Row] = field(default_factory=list)
    notes: list[sqlite3.Row] = field(default_factory=list)
    #: True when the row cap hid some coursework, so the answer can say so
    #: rather than implying the list is complete.
    tasks_truncated: bool = False


def gather(
    conn: sqlite3.Connection,
    question: str,
    *,
    now: datetime,
    limit: int = MAX_TASKS,
) -> QueryContext:
    """Collect the rows an answer may use. No model call happens here."""
    from bot import repository as repo

    today = now.date()
    context = QueryContext(
        now=now, question=question, week_number=repo.week_number(conn, today)
    )

    context.courses = conn.execute(
        "SELECT code, name, verify_notes FROM courses ORDER BY code"
    ).fetchall()

    # Everything still live, plus completed work so "did I finish X" can be
    # answered. Archived and stale rows are deliberately out of view.
    #
    # Deliberately NOT limited to a date window. A 21-day horizon here made the
    # bot answer "I don't have a final essay for CMDS 1630" about an essay it
    # holds, due in December — a confident false negative, which is exactly as
    # misleading as a guess and just as likely to be believed.
    context.tasks = conn.execute(
        "SELECT title, course, type, due_date, weight_pct, priority, status, "
        "tentative, attendance, notes FROM tasks "
        "WHERE status IN ('not_started', 'in_progress', 'done') "
        "ORDER BY COALESCE(due_date, '9999-12-31'), priority LIMIT ?",
        (limit + 1,),
    ).fetchall()
    context.tasks_truncated = len(context.tasks) > limit
    context.tasks = context.tasks[:limit]

    context.goals = repo.active_goals(conn)
    context.reminders = conn.execute(
        "SELECT text, fire_at FROM reminders WHERE sent = 0 ORDER BY fire_at LIMIT 20"
    ).fetchall()
    context.gym = conn.execute(
        "SELECT day_of_week, split_name FROM gym ORDER BY day_of_week"
    ).fetchall()

    if context.week_number is not None:
        context.topics = conn.execute(
            "SELECT c.code, w.week_number, w.topic FROM course_weeks w "
            "JOIN courses c ON c.id = w.course_id "
            "WHERE w.week_number BETWEEN ? AND ? ORDER BY w.week_number, c.code",
            (context.week_number, context.week_number + 3),
        ).fetchall()

    context.notes = conn.execute(
        "SELECT text, tags, created_at FROM notes ORDER BY created_at DESC LIMIT 15"
    ).fetchall()
    return context


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def render(context: QueryContext) -> str:
    """The data block. This is the entire world the answer may draw on."""
    today = context.now.date()
    lines = [f"TODAY: {WEEKDAYS[today.weekday()]} {today.isoformat()}"]
    if context.week_number is not None:
        lines.append(f"SEMESTER WEEK: {context.week_number}")

    lines.append("")
    if context.courses:
        lines.append("COURSES ON FILE (any course not listed here is unknown):")
        for row in context.courses:
            lines.append(f"- {row['code']}" + (f" — {row['name']}" if row["name"] else ""))
    else:
        lines.append("COURSES ON FILE: none. Nothing is known about any course.")

    flagged = [(r["code"], r["verify_notes"]) for r in context.courses if r["verify_notes"]]
    if flagged:
        lines.append("")
        lines.append(
            "UNVERIFIED IN THE SYLLABUS EXTRACTION — say so if the question "
            "touches any of this, and tell him to check the syllabus:"
        )
        for code, notes in flagged:
            for note in str(notes).splitlines():
                lines.append(f"- {code}: {note}")

    if context.tasks:
        lines.append("")
        lines.append("COURSEWORK ON FILE:")
        for row in context.tasks:
            bits = [row["title"], row["course"] or "no course"]
            bits.append(f"due {row['due_date']}" if row["due_date"] else "no due date")
            if row["weight_pct"] is not None:
                bits.append(f"{pct(row['weight_pct'])}% of grade")
            bits.append(f"P{row['priority']}")
            bits.append(row["status"])
            if row["tentative"]:
                bits.append("DATE TENTATIVE")
            if row["attendance"]:
                bits.append("attendance mark, nothing to submit")
            if row["notes"]:
                bits.append(str(row["notes"]))
            lines.append("- " + " | ".join(bits))
        if context.tasks_truncated:
            lines.append(
                "- (this list was cut off at the row limit; there is more "
                "coursework on file than is shown here)"
            )
    else:
        lines.append("")
        lines.append("COURSEWORK ON FILE: none.")

    def block(title: str, entries: list[str]) -> None:
        if entries:
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(f"- {e}" for e in entries)

    block("GOALS", [f"{r['tier']}: {r['text']}" for r in context.goals])
    block("PENDING REMINDERS", [f"{r['fire_at']}: {r['text']}" for r in context.reminders])
    block("GYM SPLIT", [f"{WEEKDAYS[r['day_of_week']]}: {r['split_name']}" for r in context.gym])
    block(
        "UPCOMING COURSE TOPICS",
        [f"{r['code']} week {r['week_number']}: {r['topic']}" for r in context.topics],
    )
    block("NOTES", [f"{r['text']}" + (f" [{r['tags']}]" if r["tags"] else "") for r in context.notes])
    return "\n".join(lines)


SYSTEM = """\
{voice}

Kaan asked a question about his own data. Below is everything on file. It is
the whole of what you know.

The one rule: answer only from that data.

- If the answer isn't there, say so plainly and say what would fix it — send
  that syllabus, tell me the date, I don't have that course.
- Never estimate a weight, a date, or a deadline that isn't listed. A confident
  wrong answer about a deadline is worse than no answer, because he'll act on it.
- A course not in COURSES ON FILE is one you know nothing about. Do not reason
  about what a course like that "probably" involves.
- If anything under UNVERIFIED bears on the question, say that part came from
  reading a PDF and is worth checking against the syllabus.
- Where a date is marked TENTATIVE, say it's provisional.
- An attendance mark is not something he submits; never describe it as due.

You are reading, not writing. This path cannot change anything: it does not
mark work done, record attendance, set a reminder, or save a note. So never say
or imply that you did. No "marked done", no "noted", no "I've recorded that",
no "that's logged". If he is telling you something happened and it ought to be
saved, say plainly that you haven't saved it and ask him to say it again as an
instruction — "tell me 'mark the PSYC reflection done' and I'll record it".
Claiming a write you did not perform is the worst thing you can do here: he
will believe his term is tracked when it is not.

Answer the question directly, in a sentence or a short list. This is a text
message. No preamble, no restating the question.

{data}
"""


CONVERSATIONAL = """
He is talking rather than asking a direct question. Reply as a friend would —
briefly, and to what he actually said. The same rule still holds: anything you
say about his term comes from the data above or not at all. If he is worried
about something the data doesn't cover, say you don't have it rather than
reassuring him with a guess.

The read-only rule matters most here, because conversation invites it. If he
reports finishing something, acknowledge what he said without claiming to have
recorded it, and tell him how to make it stick.
"""


def answer(
    conn: sqlite3.Connection,
    question: str,
    writer: Writer,
    *,
    now: datetime | None = None,
    conversational: bool = False,
) -> str:
    """Answer from the database alone."""
    moment = now or datetime.now()
    context = gather(conn, question, now=moment)
    logger.info(
        "Query %r against %d task(s), %d course(s)",
        question,
        len(context.tasks),
        len(context.courses),
    )
    system = SYSTEM.format(voice=VOICE, data=render(context))
    if conversational:
        system += CONVERSATIONAL
    text = writer.compose(system, question, max_tokens=1000)
    if not text.strip():
        raise AssistantError(
            E.CLAUDE, "I couldn't put an answer together for that.", trigger=question
        )
    return text
