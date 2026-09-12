"""Morning brief (plan Section 7).

Two stages, deliberately separated:

1. ``assemble`` gathers facts from the database, calendar and weather. Nothing
   here talks to Claude, so it is fully testable and can't invent anything.
2. ``compose`` hands those facts to Claude to write in Kaan's voice.

If stage 2 fails, ``generate`` falls back to ``render_plain`` — a deterministic
rendering of the same facts. A brief that reads a bit flat is a far better
outcome than no brief at all, and the failure still lands in the log with its
code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from bot import backlog as backlog_rules, google_calendar, repository as repo, weather
from bot.claude_client import Writer
from bot.errors import AssistantError, E, log_error, logger
from bot.formatting import pct
from bot.email_reader import FlaggedEmail
from bot.google_calendar import CalendarEvent
from bot.voice import VOICE
from db.database import get_config

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

# Statuses that still want attention. 'stale' is excluded on purpose: the whole
# point of the backlog rule is that those stop appearing in the daily view.
LIVE_STATUSES = ("not_started", "in_progress")


@dataclass
class BriefContext:
    """Everything the brief may mention. Empty collections mean skip that part."""

    now: datetime
    week_number: int | None = None
    goals: list[sqlite3.Row] = field(default_factory=list)
    forecast: weather.Forecast | None = None
    today_events: list[CalendarEvent] = field(default_factory=list)
    week_events: list[CalendarEvent] = field(default_factory=list)
    gym_split: str | None = None
    due_today: list[sqlite3.Row] = field(default_factory=list)
    due_tomorrow: list[sqlite3.Row] = field(default_factory=list)
    overdue_urgent: list[sqlite3.Row] = field(default_factory=list)
    upcoming: list[sqlite3.Row] = field(default_factory=list)
    reminders: list[sqlite3.Row] = field(default_factory=list)
    #: Marks for turning up today. Never a deadline — there is nothing to hand
    #: in, so the useful thing to say is that going is worth marks.
    attendance_today: list[sqlite3.Row] = field(default_factory=list)
    week_topics: list[tuple[str, str]] = field(default_factory=list)
    #: Email findings. Surfaced only — Section 6 is explicit that nothing is
    #: written to tasks until Kaan confirms.
    flagged_emails: list[FlaggedEmail] = field(default_factory=list)
    #: How many messages were read from the allowlist, or None if email was
    #: never checked. Nothing flagged and nothing checked produced the same
    #: empty section, so a quiet mailbox looked exactly like a broken one.
    emails_scanned: int | None = None
    #: Count of set-aside work, only when a weekly mention is due. None the
    #: rest of the time, because a daily count of things he has already decided
    #: not to do is the nagging the backlog rule exists to prevent.
    backlog_count: int | None = None
    #: True during the mid-term break, when a week number is the wrong
    #: thing to report.
    reading_week: bool = False
    #: Weekly/monthly goals with no reported movement. Nudged once, gently.
    stalled_goals: list[sqlite3.Row] = field(default_factory=list)
    #: Yesterday's unclosed daily goal. Exactly one soft mention, ever.
    missed_daily: list[sqlite3.Row] = field(default_factory=list)
    #: Subsystems that couldn't be reached, named so the brief can say so
    #: instead of quietly omitting a section that should have had content.
    unavailable: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 1: facts
# ---------------------------------------------------------------------------


def _tasks(conn: sqlite3.Connection, where: str, params: tuple) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in LIVE_STATUSES)
    return conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) AND {where} "
        "ORDER BY priority, COALESCE(due_date, '9999-12-31')",
        (*LIVE_STATUSES, *params),
    ).fetchall()


def assemble(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timezone: str = "America/Toronto",
    latitude: float | None = None,
    longitude: float | None = None,
    calendar_token: Path | None = None,
    calendar_secrets: Path | None = None,
    flagged_emails: list[FlaggedEmail] | None = None,
    emails_scanned: int | None = None,
) -> BriefContext:
    """Gather the facts. Never raises for an unavailable subsystem.

    ``flagged_emails`` is passed in rather than fetched here: deciding which
    mail matters needs a Claude call, and stage 1 stays free of those so it
    remains testable and incapable of inventing anything.
    """
    today = now.date()
    tomorrow = today + timedelta(days=1)
    context = BriefContext(now=now, week_number=repo.week_number(conn, today))
    context.flagged_emails = list(flagged_emails or [])
    context.emails_scanned = emails_scanned

    context.goals = repo.active_goals(conn)
    context.gym_split = repo.gym_split_for(conn, today.weekday())

    context.due_today = _tasks(
        conn, "due_date = ? AND attendance = 0", (today.isoformat(),)
    )
    context.attendance_today = _tasks(
        conn, "due_date = ? AND attendance = 1", (today.isoformat(),)
    )
    context.due_tomorrow = _tasks(
        conn, "due_date = ? AND attendance = 0", (tomorrow.isoformat(),)
    )
    context.overdue_urgent = _tasks(
        conn, "due_date < ? AND priority = 1 AND attendance = 0", (today.isoformat(),)
    )
    context.upcoming = _tasks(
        conn,
        "attendance = 0 AND due_date > ? AND due_date <= ?",
        (tomorrow.isoformat(), (today + timedelta(days=7)).isoformat()),
    )

    context.reading_week = repo.in_reading_week(conn, today)
    context.stalled_goals = repo.stalled_goals(conn, now)
    context.missed_daily = repo.missed_daily_goals(conn, today)

    if backlog_rules.weekly_nudge_due(conn, now):
        context.backlog_count = len(backlog_rules.backlog(conn))

    # Reminders already due but not yet sent, i.e. carried over.
    context.reminders = repo.due_reminders(conn, now)

    if context.week_number is not None:
        context.week_topics = [
            (row["code"], row["topic"])
            for row in conn.execute(
                "SELECT c.code, w.topic FROM course_weeks w "
                "JOIN courses c ON c.id = w.course_id "
                "WHERE w.week_number = ? AND w.topic IS NOT NULL ORDER BY c.code",
                (context.week_number,),
            ).fetchall()
        ]

    if latitude is not None and longitude is not None:
        try:
            context.forecast = weather.fetch(latitude, longitude, timezone)
        except AssistantError as err:
            log_error(err)
            context.unavailable.append("weather")

    if calendar_token and calendar_secrets:
        try:
            context.today_events = google_calendar.events_for_day(
                calendar_token, calendar_secrets, today, timezone
            )
            context.week_events = google_calendar.events_for_week(
                calendar_token, calendar_secrets, today, timezone
            )
        except AssistantError as err:
            log_error(err)
            context.unavailable.append("calendar")

    return context


# ---------------------------------------------------------------------------
# Rendering the facts for Claude (and as the fallback brief)
# ---------------------------------------------------------------------------


def _task_line(row: sqlite3.Row) -> str:
    bits = [row["title"]]
    if row["course"]:
        bits.append(row["course"])
    if row["weight_pct"] is not None:
        bits.append(f"{pct(row['weight_pct'])}% of grade")
    bits.append(f"priority {row['priority']}")
    if row["due_date"]:
        bits.append(f"due {row['due_date']}")
    if row["tentative"]:
        bits.append("date tentative")
    if row["notes"]:
        bits.append(str(row["notes"]))
    return " | ".join(bits)


def _event_line(event: CalendarEvent, *, with_date: bool = False) -> str:
    """One event as a fact line.

    ``with_date`` is required for any section spanning more than one day. The
    time alone reads as "today" to anything summarising it — a week's events
    listed as bare clock times produced a brief that confidently placed next
    Monday's appointment this afternoon.
    """
    when = event.when()
    if with_date:
        day = event.start
        when = f"{WEEKDAYS[day.weekday()][:3]} {day:%b} {day.day} {when}"
    bits = [f"{when} {event.summary}"]
    if event.location:
        bits.append(event.location)
    if event.recurring:
        bits.append("recurring (lecture/standing)")
    return " | ".join(bits)


def _event_day(event: CalendarEvent) -> date:
    """The calendar date an event falls on, all-day or timed."""
    start = event.start
    return start.date() if isinstance(start, datetime) else start


def render_facts(context: BriefContext) -> str:
    """The structured data block. Also the fallback brief if Claude is down."""
    today = context.now.date()
    lines: list[str] = [
        f"DATE: {WEEKDAYS[today.weekday()]}, {today:%B} {today.day}, {today.year}",
    ]
    if context.reading_week:
        lines.append("READING WEEK - no classes. Say so instead of a week number.")
    elif context.week_number is not None:
        lines.append(f"SEMESTER WEEK: {context.week_number}")

    def section(title: str, entries: list[str]) -> None:
        if entries:
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(f"- {entry}" for entry in entries)

    section(
        "GOALS",
        [f"{row['tier']}: {row['text']}" for row in context.goals],
    )
    if context.forecast:
        lines.append("")
        lines.append(f"WEATHER: {context.forecast.summary()}")

    section("OVERDUE AND STILL HIGH PRIORITY", [_task_line(r) for r in context.overdue_urgent])
    section(
        "MARKS FOR TURNING UP TODAY (nothing to submit - only counts if he goes)",
        [
            f"{row['title']} | {row['course']} | {pct(row['weight_pct'])}% of grade"
            + (f" | {row['notes']}" if row["notes"] else "")
            for row in context.attendance_today
        ],
    )
    section("DUE TODAY", [_task_line(r) for r in context.due_today])
    section("DUE TOMORROW", [_task_line(r) for r in context.due_tomorrow])
    section("DUE WITHIN A WEEK", [_task_line(r) for r in context.upcoming])
    section("CARRIED-OVER REMINDERS", [row["text"] for row in context.reminders])
    section("TODAY'S CALENDAR", [_event_line(e) for e in context.today_events])
    section(
        "THIS WEEK, EXCLUDING LECTURES",
        [
            _event_line(e, with_date=True)
            for e in context.week_events
            if not e.is_lecture and _event_day(e) != today
        ],
    )
    section("THIS WEEK'S COURSE TOPICS", [f"{code}: {topic}" for code, topic in context.week_topics])
    section(
        "FLAGGED IN EMAIL (not saved - Kaan confirms before anything is written)",
        [item.line() for item in context.flagged_emails],
    )
    if context.emails_scanned is not None and not context.flagged_emails:
        # None means email was never checked, and there is nothing honest to
        # say about it. A count - zero included - means it was.
        section(
            "EMAIL CHECKED, NOTHING IN IT (say so in a few words, so silence "
            "here reads as checked rather than broken)",
            [
                f"read {context.emails_scanned} message(s) from the allowlist, "
                "none of them needed anything"
                if context.emails_scanned
                else "no mail from the allowlist in the last few days"
            ],
        )
    section(
        "GOALS WITH NO REPORTED MOVEMENT (mention one, lightly, as a question "
        "rather than a prod - and only if the day is not already full)",
        [f"{row['tier']}: {row['text']}" for row in context.stalled_goals],
    )
    section(
        "DAILY GOAL THAT PASSED UNCLOSED (one brief, unjudgemental mention - "
        "it will never be raised again)",
        [row["text"] for row in context.missed_daily],
    )
    if context.backlog_count:
        lines.append("")
        lines.append(
            f"BACKLOG: {context.backlog_count} item(s) set aside as overdue and "
            "low-stakes. Mention once, in passing, as something he could review "
            "with /backlog. Do not list them and do not press."
        )
    if context.gym_split:
        lines.append("")
        lines.append(f"GYM TODAY: {context.gym_split}")
    if context.unavailable:
        lines.append("")
        lines.append("COULD NOT REACH: " + ", ".join(context.unavailable))

    return "\n".join(lines)


def render_plain(context: BriefContext) -> str:
    """Deterministic fallback brief. Correct, if a bit flat."""
    today = context.now.date()
    header = f"{WEEKDAYS[today.weekday()]}, {today:%B} {today.day}"
    if context.week_number is not None:
        header += f" — week {context.week_number}"
    body = render_facts(context)
    # Drop the DATE/WEEK lines the header already covers.
    body = "\n".join(
        line for line in body.splitlines()
        if not line.startswith(("DATE:", "SEMESTER WEEK:"))
    ).strip()
    return f"{header}\n\n{body}" if body else header


BRIEF_SYSTEM = """\
{voice}

You are writing Kaan's morning brief. Work only from the facts given — never
invent a task, a date, an event, or a number. If a section has no facts, leave
it out entirely rather than saying it is empty.

Open with one line of your own that gives him a reason to get up and do today's
version of the work. It is the first thing he reads in the morning, so it has
to earn its place.

- Talk to him. Second person, present tense.
- Make it about doing something, not about how things are. It should push.
- Concrete beats abstract. If today turns on one thing - a lecture that carries
  a mark for being there, a deadline that closes tonight - make the line about
  that thing.
- One sentence, around fifteen words.
- Write a new one every day. Never a famous quotation, never attributed, never
  in quotation marks.
- Do not open with "Some weeks", "Some days", "There are days", or any other
  throat-clearing about how things generally go, and do not tag an observation
  onto the end to make it land ("- that's today", "and today is one of them").
- No hedging, no wistfulness, no both-sides. If it would work printed over a
  photo of a sunrise, write a different one.
- It is its own line. Do not fold the date into it, and do not let it become a
  summary of the week; the sections below already do that.
- Vary how it opens. Not every one starts with "get up and" or "get to" - three
  mornings of the same construction reads as nagging. What must not vary is
  that it points at something real today.

Then, in this order, skipping anything with no facts:
1. Date and semester week
2. Goals
3. Weather
4. Anything overdue and still high priority
5. What's on today (calendar, then what's due today). If a lecture carries a
   mark just for being there, say so as a reason to go — briefly, once, without
   moralising. Never call it "due"; there is nothing to hand in.
6. Today's gym split
7. Carried-over reminders
8. The rest of the week: non-lecture events, upcoming deadlines, course topics
9. Anything flagged in email — say plainly that it came from an email and is
   not saved yet, so he can confirm it. List every one of them: these are the
   only items in the brief that exist nowhere else, and a flag you leave out to
   save room is gone for good. A run that dropped a "syllabus quiz is now live"
   to stay short cost him the one piece of real work in the whole message. If
   instead the facts say email was checked and held nothing, say that in a
   short clause rather than omitting it; he needs to be able to tell a quiet
   mailbox from a broken one.
10. What to prep for tomorrow

Formatting: this is a Telegram message. Short lines, no markdown headers, no
bold. A bare line of text for each section beats a label. Under 200 words unless
the day genuinely has a lot in it — and email flags are never what you cut to
get there. If something could not be reached, say so in
a few words at the end rather than pretending the section was empty.
"""


def compose(context: BriefContext, writer: Writer) -> str:
    """Have Claude write the brief. Raises AssistantError on failure."""
    return writer.compose(
        BRIEF_SYSTEM.format(voice=VOICE),
        render_facts(context),
        max_tokens=1200,
    )


def generate(context: BriefContext, writer: Writer | None) -> str:
    """The brief, falling back to the plain rendering if Claude is unavailable."""
    if writer is None:
        return render_plain(context)
    try:
        text = compose(context, writer)
    except AssistantError as err:
        log_error(
            AssistantError(
                E.BRIEF_FAILED,
                "Falling back to the plain brief.",
                cause=err.cause or err,
            )
        )
        return render_plain(context)

    if not text.strip():
        logger.warning("Claude returned an empty brief; using the plain rendering")
        return render_plain(context)
    return text
