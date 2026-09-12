"""Bot entry point (plan Sections 1 and 4).

Run from the repo root::

    python -m bot.main

Answers /start and /status, and routes any other text through the classify ->
handle -> receipt pipeline in ``bot.router``. Everything is restricted to
OWNER_TELEGRAM_ID; other senders are logged and ignored without a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import (
    backlog as backlog_rules,
    brief,
    checkin as checkin_mod,
    email_reader,
    image_reader,
    google_calendar,
    repository as repo,
    router,
    syllabus as syl,
    term_dates,
    testmode,
)
from bot.claude_client import AnthropicClient
from bot.config import ConfigError, Settings, load_settings
from bot.errors import AssistantError, E, log_error, logger, setup_logging
from db import database

# Stashed on Application.bot_data so handlers can reach them without globals.
KEY_SETTINGS = "settings"
KEY_DB = "db"
KEY_CLASSIFIER = "classifier"
KEY_CONFLICTS = "conflicts"
KEY_CONFLICT_AT = "conflict_at"

# The Claude call is blocking, so it runs in a worker thread. This lock keeps two
# messages from interleaving their DB transactions while it does.
# The same lock db.database.transaction() takes, so the coarse uses below and
# the per-transaction ones cannot deadlock against each other. Reach for the
# coarse form only when a whole sequence must be exclusive; a single write does
# not need it, and holding it across a Claude call is what froze the bot.
_db_lock = database.write_lock


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data[KEY_SETTINGS]


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.application.bot_data[KEY_DB]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Up and running. Text me a task, a reminder, or a goal and I'll save "
        "it. /help lists everything, /status shows what's on file."
    )


def _email_lookup(app: Application, conn: sqlite3.Connection):
    """A callable the router can use to read the mailbox, or None if it can't.

    None means email was never set up, and the reply says so. A callable that
    returns scanned=None means it was set up and the read failed, which is a
    different sentence: try again, rather than go and configure something.
    """
    settings: Settings = app.bot_data[KEY_SETTINGS]
    if not (settings.gmail_imap_user and settings.gmail_app_password):
        return None

    def lookup():
        try:
            return _gather_email(app, conn)
        except AssistantError as err:
            log_error(err)
            return [], None

    return lookup


async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read course email on demand (plan Section 6).

    Until now email only ever arrived inside the 07:30 brief. Asking about it
    went to the query path, which sees only saved data - and no email is ever
    saved - so it answered that there was nothing, every time.
    """
    app = context.application
    conn = _db(context)
    lookup = _email_lookup(app, conn)

    async with _typing(update.effective_message):
        reply = await asyncio.to_thread(
            lambda: router.run_email_check(lookup, conn=conn)
        )
        with _db_lock:
            prefix = testmode.banner(conn)
    await update.effective_message.reply_text(prefix + reply)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A quick end-to-end proof that Telegram, the DB, and config all work."""
    conn = _db(context)
    try:
        version = database.current_version(conn)
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("tasks", "reminders", "goals", "courses", "gym")
        }
        semester_start = database.get_config(conn, "semester_start_date") or "not set"
        brief_time = database.get_config(conn, "brief_send_time", "?")
        timezone = database.get_config(conn, "timezone", "?")
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_READ, "Couldn't read the database.", cause=exc, trigger="/status"
        ) from exc

    parsing = "on" if context.application.bot_data.get(KEY_CLASSIFIER) else "OFF (no API key)"
    banner = testmode.banner(conn)
    rows = "\n".join(f"  {name}: {n}" for name, n in counts.items())

    # A second process polling the same token no longer sends a message of its
    # own, so this is where it surfaces.
    conflicts = context.application.bot_data.get(KEY_CONFLICTS, 0)
    warning = ""
    if conflicts:
        last = context.application.bot_data.get(KEY_CONFLICT_AT)
        when = f" (last {last:%b %d, %H:%M})" if last else ""
        plural = "s" if conflicts != 1 else ""
        warning = (
            f"<b>Another copy of me is running</b>{when}\n"
            f"Telegram reported {conflicts} polling conflict{plural} since I "
            "started. Stop the bot on any other machine - whichever instance "
            "grabs a message first is the one that answers it.\n\n"
        )

    await update.effective_message.reply_text(
        (f"<b>{banner}/testoff discards everything since it started</b>\n\n"
         if banner else "")
        + warning
        + f"<b>Schema</b> v{version}\n"
        f"<b>Message parsing</b> {parsing}\n"
        f"<b>Semester start</b> {semester_start}\n"
        f"<b>Brief</b> {brief_time} ({timezone})\n"
        f"<b>Rows</b>\n{rows}",
        parse_mode=ParseMode.HTML,
    )


async def on_unknown_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log-and-ignore. Never replies, so an unknown sender learns nothing."""
    user = update.effective_user
    logger.warning(
        "Ignored message from non-owner id=%s username=%s",
        user.id if user else "?",
        user.username if user else "?",
    )


def _remaining_today(settings: Settings, conn: sqlite3.Connection) -> list:
    """Today's events that haven't happened yet, for resolving "after my class".

    Returns [] on any failure: a reminder that needs a clock time is better
    than no reply at all, and the handler says plainly when the calendar
    couldn't settle it.
    """
    token = settings.google_token_personal
    if not token.exists():
        return []
    try:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        now = datetime.now(ZoneInfo(timezone))
        events = google_calendar.events_for_day(
            token, settings.google_client_secrets, now.date(), timezone
        )
    except AssistantError as err:
        log_error(err)
        return []

    remaining = []
    for event in events:
        if event.all_day:
            continue
        start = event.start
        if getattr(start, "tzinfo", None) is None:
            continue
        if start < now:
            continue
        ends = event.end.strftime("%H:%M") if getattr(event.end, "strftime", None) else "?"
        remaining.append((f"{start:%H:%M}-{ends}", event.summary))
    return remaining


#: Telegram shows "typing..." for about five seconds per call, so it has to be
#: repeated to cover a longer wait.
_TYPING_REFRESH_SECONDS = 4.0


@contextlib.asynccontextmanager
async def _typing(message):
    """Hold the typing indicator for as long as the block runs.

    A reply takes several seconds — a classification and then, for a question,
    a second call to write the answer. Without this the chat looks dead for the
    whole of it and the natural response is to send the message again.
    """

    async def keep_typing() -> None:
        while True:
            try:
                await message.reply_chat_action(ChatAction.TYPING)
            except Exception:  # noqa: BLE001 - cosmetic; never fail the reply
                return
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Classify one message, run its handler, reply with the receipt."""
    classifier = context.application.bot_data.get(KEY_CLASSIFIER)
    if classifier is None:
        await update.effective_message.reply_text(
            "I can't read messages yet - ANTHROPIC_API_KEY isn't set in .env."
        )
        return

    text = update.effective_message.text
    conn = _db(context)
    settings = _settings(context)

    def work() -> str:
        # No lock here on purpose. This runs a calendar fetch and one or two
        # Claude calls; holding the database lock across them stopped reminders
        # firing and every other message being answered for the duration. The
        # writes underneath take it per transaction, which is all it was for.
        events = _remaining_today(settings, conn)
        # The classifier doubles as the Writer; answer_query needs prose.
        return router.handle_message(
            conn,
            classifier,
            text,
            writer=classifier,
            upcoming_events=events,
            email_lookup=_email_lookup(context.application, conn),
        )

    # Off the event loop: the Anthropic SDK call is synchronous and would
    # otherwise stall every other update while it waits.
    async with _typing(update.effective_message):
        reply = await asyncio.to_thread(work)
        with _db_lock:
            prefix = testmode.banner(conn)
    await update.effective_message.reply_text(prefix + reply)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a syllabus PDF and import it (plan Section 5).

    Follows the receipt pattern rather than a confirmation gate: the import
    happens, and the reply lists what landed so a bad extraction is obvious.
    """
    message = update.effective_message
    document = message.document
    settings = _settings(context)

    name = (document.file_name or "").lower()
    if not name.endswith(".pdf") and document.mime_type != "application/pdf":
        await message.reply_text(
            "I can only read PDFs right now. Export it and send it again."
        )
        return

    if not settings.anthropic_api_key:
        await message.reply_text(
            "I can't read documents yet - ANTHROPIC_API_KEY isn't set in .env."
        )
        return

    async with _typing(message):
        telegram_file = await document.get_file()
        pdf_bytes = bytes(await telegram_file.download_as_bytearray())

        conn = _db(context)

        def work() -> str:
            from anthropic import Anthropic

            client = Anthropic(
                api_key=settings.anthropic_api_key, timeout=180.0, max_retries=1
            )
            extracted = syl.extract(pdf_bytes, client, settings.claude_model)
            with _db_lock:
                counts = syl.ingest(conn, extracted)
            return syl.receipt(extracted, counts)

        reply = await asyncio.to_thread(work)
    await message.reply_text(reply)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read dates out of a screenshot or photo.

    Kaan reached for a screenshot of a schedule unprompted, and it went nowhere:
    a photo is not a Document and a caption is not TEXT, so no handler matched
    and the message vanished without a reply or a log line.
    """
    message = update.effective_message
    settings = _settings(context)

    if not settings.anthropic_api_key:
        await message.reply_text(
            "I can't read images yet - ANTHROPIC_API_KEY isn't set in .env."
        )
        return

    async with _typing(message):
        # Telegram sends several sizes; the last is the largest.
        photo = message.photo[-1]
        telegram_file = await photo.get_file()
        data = bytes(await telegram_file.download_as_bytearray())
        caption = message.caption or ""
        conn = _db(context)

        def work() -> str:
            from anthropic import Anthropic

            client = Anthropic(
                api_key=settings.anthropic_api_key, timeout=120.0, max_retries=1
            )
            course, items = image_reader.extract(
                data, caption, client, settings.claude_model
            )
            if items:
                with _db_lock:
                    image_reader.ingest(conn, course, items)
            return image_reader.receipt(course, items)

        reply = await asyncio.to_thread(work)
    await message.reply_text(reply)


async def on_unhandled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything from Kaan that no other handler claimed.

    Registered last so nothing he sends can disappear in silence, which is what
    happened to a captioned screenshot: no handler matched, so there was no
    reply and nothing in the log to explain it.
    """
    message = update.effective_message
    kinds = [
        name for name in ("voice", "video", "audio", "sticker", "location", "poll",
                          "contact", "animation", "video_note")
        if getattr(message, name, None)
    ]
    logger.warning("Unhandled message kind=%s", kinds or "unknown")
    await message.reply_text(
        "I got that but don't know how to read it yet. Text, a PDF, or a "
        "screenshot all work."
    )


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerate the brief on demand, always from current data."""
    await update.effective_message.chat.send_action("typing")
    text = await asyncio.to_thread(_build_brief, context.application)
    await update.effective_message.reply_text(text)


def _read_term_dates(app: Application) -> str:
    """Read the academic calendar out of Google Calendar and store it."""
    settings: Settings = app.bot_data[KEY_SETTINGS]
    conn: sqlite3.Connection = app.bot_data[KEY_DB]

    token = settings.google_token_personal
    if not token.exists():
        return "Calendar isn't connected, so I can't read your term dates."

    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
    now = datetime.now(ZoneInfo(timezone))

    # The academic year around today, not the calendar year. Starting at
    # January 1st swept up the previous winter term and reported week 36.
    events = google_calendar.list_events(
        token,
        settings.google_client_secrets,
        start=now - timedelta(days=120),
        end=now + timedelta(days=300),
        max_results=250,
    )

    found = term_dates.find(events, today=now.date(), term_year=now.year)
    deadlines = term_dates.find_deadlines(
        events, today=now.date(), term_year=now.year
    )
    with _db_lock:
        changed = term_dates.apply(conn, found)
        term_dates.apply_deadlines(conn, deadlines)
        week = repo.week_number(conn, now.date())

    return term_dates.render(
        found, changed, deadlines, today=now.date(), week=week
    )


HELP_TEXT = """\
Text me normally — a task, a reminder, a goal, a question — and I'll work out \
what you meant. Send a PDF to import a syllabus, or a screenshot to pull dates \
out of it.

What I do on my own
  07:30  morning brief
  21:00  evening check-in, so things get marked done
  08:15  check my Google token still works, silent unless it doesn't
  every minute  send any reminder that's come due

Commands
  /recap     the brief again, rebuilt from current data
  /backlog   work set aside as overdue and low-stakes
  /email     read course email now and say what matters in it
  /terms     re-read term dates and deadlines from your calendar
  /status    what's on file and what's working
  /quiet     silence the morning brief; /quiet evening for the check-in
  /teston    try things out — /testoff throws it all away
  /help      this

Things worth knowing
  I only know what you've told me. Ask about a course I don't have and I'll \
say so rather than guess.
  A date I read off a syllabus is flagged if I wasn't sure — worth checking \
against the document.
  I never write to your Google Calendar. Read-only.
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything the bot responds to."""
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-read the academic calendar. Safe to run again after editing it."""
    await update.effective_message.chat.send_action("typing")
    await update.effective_message.reply_text(
        await asyncio.to_thread(_read_term_dates, context.application)
    )


async def cmd_teston(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snapshot the database so everything from here can be thrown away.

    Exists because testing against the live database left a fabricated reminder
    due to fire that evening and two real tasks marked done, one of them an
    attendance mark for a lecture that had not happened yet.
    """
    conn = _db(context)
    settings = _settings(context)
    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        testmode.start(conn, settings.db_path, datetime.now(ZoneInfo(timezone)))
    await update.effective_message.reply_text(
        "TEST MODE ON.\n\n"
        "Everything from here - tasks, reminders, status changes - is thrown "
        "away by /testoff.\n\n"
        "That includes anything real you enter meanwhile, so don't."
    )


async def cmd_testoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Roll back to the snapshot taken by /teston."""
    conn = _db(context)
    settings = _settings(context)
    with _db_lock:
        summary = testmode.stop(conn, settings.db_path)
    await update.effective_message.reply_text(
        "Test mode off. Rolled back: " + summary.describe() + "."
    )


async def cmd_backlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What has been set aside. Still on file, just out of the daily view."""
    conn = _db(context)
    with _db_lock:
        backlog_rules.demote(conn)
        rows = backlog_rules.backlog(conn)
        database.set_config(
            conn,
            "backlog_nudged_on",
            datetime.now(
                ZoneInfo(database.get_config(conn, "timezone", "America/Toronto"))
            ).date().isoformat(),
        )
    await update.effective_message.reply_text(backlog_rules.render(rows))


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the morning brief, or the evening check-in, off and on.

    Separate toggles: Section 10 asks for the evening one to be silenceable on
    its own, since wanting the brief and not wanting to be asked about your day
    are different preferences.
    """
    args = context.args or []
    evening = bool(args) and args[0].lower().startswith("even")
    key = "quiet_evening" if evening else "quiet_morning"
    label = "Evening check-in" if evening else "Morning brief"

    conn = _db(context)
    with _db_lock:
        currently_quiet = database.get_config(conn, key, "0") == "1"
        database.set_config(conn, key, "0" if currently_quiet else "1")

    if currently_quiet:
        await update.effective_message.reply_text(f"{label} back on.")
    else:
        again = "/quiet evening" if evening else "/quiet"
        await update.effective_message.reply_text(
            f"{label} off. {again} again to undo."
        )


def _gather_email(
    app: Application, conn: sqlite3.Connection
) -> tuple[list, int | None]:
    """Fetch and flag course email, and say how many were read.

    Returns (flagged, scanned). ``scanned`` is None when email was never
    checked - no credentials, no allowlist, no API key - and a count otherwise,
    including zero. The brief needs that distinction: nine messages read and
    none worth flagging used to render exactly like a mailbox that never
    connected, which is what made Kaan ask whether email was working at all.

    Kept out of brief.assemble because flagging needs a Claude call and stage 1
    stays free of those. Failures are logged and reported as an unavailable
    subsystem, never allowed to take the whole brief down.
    """
    settings: Settings = app.bot_data[KEY_SETTINGS]
    if not (settings.gmail_imap_user and settings.gmail_app_password):
        return [], None

    with _db_lock:
        senders = email_reader.known_senders(conn)
    if not senders:
        return [], None

    messages = email_reader.fetch(
        host=settings.gmail_imap_host,
        user=settings.gmail_imap_user,
        password=settings.gmail_app_password,
        senders=senders,
        since=email_reader.default_window(),
    )
    if not settings.anthropic_api_key:
        return [], None
    if not messages:
        return [], 0

    from anthropic import Anthropic

    flagged = email_reader.flag(
        messages,
        Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=1),
        settings.claude_model,
    )
    logger.info("Flagged %d of %d email(s)", len(flagged), len(messages))
    return flagged, len(messages)


def _build_brief(app: Application) -> str:
    """Assemble and write the brief. Blocking; call via asyncio.to_thread."""
    settings: Settings = app.bot_data[KEY_SETTINGS]
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    writer = app.bot_data.get(KEY_CLASSIFIER)

    flagged: list = []
    flagged_ids: list[int] = []
    scanned: int | None = None
    email_failed = False
    try:
        found, scanned = _gather_email(app, conn)
        # Only the ones not already recorded reach the brief. Without this the
        # same announcement is news again every morning until it ages out of
        # the three-day scan window.
        recorded = repo.remember_flagged(conn, found)
        flagged = [item for item, _ in recorded]
        flagged_ids = [row_id for _, row_id in recorded]
    except AssistantError as err:
        log_error(err)
        email_failed = True

    # Likewise unlocked: assemble fetches the calendar and the weather, and the
    # reminder poll should not be stuck behind either.
    timezone = database.get_config(conn, "timezone", "America/Toronto")
    # Triage before assembling, so the brief reflects today's view rather
    # than yesterday's pile.
    try:
        backlog_rules.demote(conn)
    except AssistantError as err:
        log_error(err)
    token = settings.google_token_personal
    context = brief.assemble(
        conn,
        now=datetime.now(ZoneInfo(timezone)),
        timezone=timezone,
        latitude=settings.weather_latitude,
        longitude=settings.weather_longitude,
        calendar_token=token if token.exists() else None,
        calendar_secrets=settings.google_client_secrets,
        flagged_emails=flagged,
        emails_scanned=scanned,
    )
    if email_failed:
        context.unavailable.append("email")

    text = brief.generate(context, writer)
    # Count the showing only once the brief exists. The evening check-in picks
    # up whatever is left under the cap.
    repo.mark_flagged_raised(conn, flagged_ids)
    return text


def _consume_nudges(app: Application) -> None:
    """Record that the one-time mentions have now been made.

    Only after a scheduled send. /recap regenerates the same brief on demand,
    and burning a goal's single soft mention on a preview Kaan asked for would
    mean the real morning brief never carries it.
    """
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        now = datetime.now(ZoneInfo(timezone))
        for goal in repo.stalled_goals(conn, now):
            repo.mark_goal_nudged(conn, goal["id"], now)
        for goal in repo.missed_daily_goals(conn, now.date()):
            repo.mark_goal_missed_mentioned(conn, goal["id"])
        if backlog_rules.backlog(conn):
            database.set_config(conn, "backlog_nudged_on", now.date().isoformat())


async def job_morning_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled daily send."""
    app = context.application
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    if database.get_config(conn, "quiet_morning", "0") == "1":
        logger.info("Morning brief suppressed by /quiet")
        return

    settings: Settings = app.bot_data[KEY_SETTINGS]
    try:
        text = await asyncio.to_thread(_build_brief, app)
    except Exception as exc:  # noqa: BLE001 - the job must never die silently
        raise AssistantError(
            E.BRIEF_FAILED, "Couldn't put the morning brief together.", cause=exc
        ) from exc

    try:
        await context.bot.send_message(settings.owner_telegram_id, text)
    except Exception as exc:  # noqa: BLE001
        raise AssistantError(
            E.BRIEF_FAILED, "Built the morning brief but couldn't send it.", cause=exc
        ) from exc

    # Log the send explicitly. Without this a successful brief leaves no trace,
    # so "did it go out?" can only be answered by asking Kaan whether his phone
    # buzzed — which is no way to debug a job that runs while he's asleep.
    logger.info("Morning brief sent (%d chars)", len(text))

    # Only now that it has actually landed.
    try:
        await asyncio.to_thread(_consume_nudges, app)
    except AssistantError as err:
        log_error(err)


def _build_checkin(app: Application) -> tuple[str, list[int]] | None:
    """Compose tonight's check-in, or None if there is nothing worth asking."""
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    writer = app.bot_data.get(KEY_CLASSIFIER)
    if writer is None:
        return None

    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        now = datetime.now(ZoneInfo(timezone))
        context = checkin_mod.gather(conn, now)
        if not checkin_mod.has_anything_to_ask(context):
            logger.info("Nothing on today; skipping the evening check-in")
            return None
        offered = [
            row["id"]
            for group in (context.due_today, context.attendance_today, context.in_progress)
            for row in group
        ]
    return checkin_mod.compose(context, writer), offered


async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask once, in the evening, what actually happened (plan Section 10)."""
    app = context.application
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    if database.get_config(conn, "quiet_evening", "0") == "1":
        logger.info("Evening check-in suppressed by /quiet evening")
        return

    settings: Settings = app.bot_data[KEY_SETTINGS]
    try:
        built = await asyncio.to_thread(_build_checkin, app)
    except Exception as exc:  # noqa: BLE001 - the job must never die silently
        raise AssistantError(
            E.CHECKIN_FAILED, "Couldn't put the evening check-in together.", cause=exc
        ) from exc

    if built is None:
        return
    text, offered = built

    try:
        await context.bot.send_message(settings.owner_telegram_id, text)
    except Exception as exc:  # noqa: BLE001
        raise AssistantError(
            E.CHECKIN_FAILED, "Built the check-in but couldn't send it.", cause=exc
        ) from exc

    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        checkin_mod.mark_sent(conn, datetime.now(ZoneInfo(timezone)), offered)
    logger.info("Evening check-in sent, offering %d task(s)", len(offered))


async def job_poll_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send any reminder whose time has passed, once (plan Section 8)."""
    app = context.application
    settings: Settings = app.bot_data[KEY_SETTINGS]
    conn: sqlite3.Connection = app.bot_data[KEY_DB]

    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
        due = repo.due_reminders(conn, datetime.now(ZoneInfo(timezone)))

    for reminder in due:
        try:
            await context.bot.send_message(
                settings.owner_telegram_id, reminder["text"]
            )
        except Exception as exc:  # noqa: BLE001
            log_error(
                AssistantError(
                    E.REMINDER_FIRE_FAILED,
                    f"Couldn't send reminder {reminder['id']}.",
                    cause=exc,
                )
            )
            continue  # leave it unsent so the next tick retries
        with _db_lock:
            repo.mark_reminder_sent(conn, reminder["id"])
        # "Did my reminder go out?" is the first thing the log gets asked, and
        # a successful send used to leave no trace at all.
        logger.info(
            "Reminder %d sent (due %s): %r",
            reminder["id"],
            reminder["fire_at"],
            reminder["text"][:60],
        )


def _is_transient(err: BaseException | None) -> bool:
    """True for connectivity blips the polling loop already retries.

    Telegram returns Bad Gateway and friends routinely; python-telegram-bot's
    network_retry_loop recovers without help. Escalating those to Kaan means a
    "something broke" text for every hiccup — and the reply usually fails too,
    because the network is exactly what's down.
    """
    from telegram.error import NetworkError, TimedOut

    return isinstance(err, (NetworkError, TimedOut))


def _is_duplicate_instance(err: BaseException | None) -> bool:
    """True when a second process is polling Telegram with the same token.

    Conflict descends straight from TelegramError, not NetworkError, so it fell
    past the transient check and went out as "Something broke on my end."
    Unlike a blip it does not clear on its own — but the running bot keeps
    working, so it belongs in /status rather than in a message at 3am.
    """
    from telegram.error import Conflict

    return isinstance(err, Conflict)


async def job_token_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily credential check that stays silent unless something is wrong.

    Plan Section 8: Kaan hears about a dead token, and never about a healthy
    one. A token that expires quietly is the worst case here — the calendar
    just goes blank in the brief with nothing saying why.
    """
    app = context.application
    settings: Settings = app.bot_data[KEY_SETTINGS]
    conn: sqlite3.Connection = app.bot_data[KEY_DB]

    token = settings.google_token_personal
    if not token.exists():
        return  # Calendar was never connected; nothing to report.

    failure = await asyncio.to_thread(
        google_calendar.verify, token, settings.google_client_secrets
    )

    with _db_lock:
        today = datetime.now(
            ZoneInfo(database.get_config(conn, "timezone", "America/Toronto"))
        ).date().isoformat()
        already = database.get_config(conn, "token_alerted_on", "")

        if failure is None:
            if already:
                database.set_config(conn, "token_alerted_on", "")
                recovered = True
            else:
                recovered = False
        else:
            recovered = False
            if already == today:
                return  # One alert a day is enough; it is already actionable.
            database.set_config(conn, "token_alerted_on", today)

    if failure is not None:
        log_error(failure)
        await context.bot.send_message(
            settings.owner_telegram_id, failure.user_message()
        )
        return

    if recovered:
        logger.info("Google credentials healthy again")
        await context.bot.send_message(
            settings.owner_telegram_id, "Google Calendar is working again."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single funnel for every failure: log the detail, tell Kaan the code."""
    err = context.error

    if _is_transient(err):
        logger.warning("[%s] Transient network error: %s", E.TRANSIENT_NETWORK, err)
        return

    if _is_duplicate_instance(err):
        # Another process is polling with the same token — usually a copy left
        # running on the laptop. The server recovers on its own, so this is a
        # thing to notice, not a thing to be woken for: two of these arrived as
        # "Something broke on my end" at 02:49 and 03:27.
        seen = context.application.bot_data.get(KEY_CONFLICTS, 0) + 1
        context.application.bot_data[KEY_CONFLICTS] = seen
        context.application.bot_data[KEY_CONFLICT_AT] = datetime.now()
        logger.warning(
            "[%s] Another bot instance is polling the same token (%d since start)",
            E.DUPLICATE_INSTANCE,
            seen,
        )
        return

    if isinstance(err, AssistantError):
        log_error(err)
        user_text = err.user_message()
    else:
        trigger = getattr(getattr(update, "effective_message", None), "text", None)
        wrapped = AssistantError(
            E.UNEXPECTED,
            "Something broke on my end.",
            cause=err,
            trigger=trigger,
        )
        log_error(wrapped)
        user_text = wrapped.user_message()

    settings: Settings = context.application.bot_data[KEY_SETTINGS]
    try:
        await context.bot.send_message(settings.owner_telegram_id, user_text)
    except Exception as exc:  # noqa: BLE001 - last resort; nothing left to escalate to
        log_error(
            AssistantError(
                E.TELEGRAM_SEND, "Couldn't deliver an error message.", cause=exc
            )
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _post_init(app: Application) -> None:
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    logger.info("Bot started; schema v%s", database.current_version(conn))


async def _post_shutdown(app: Application) -> None:
    conn: sqlite3.Connection | None = app.bot_data.get(KEY_DB)
    if conn is not None:
        conn.close()


def build_application(settings: Settings, conn: sqlite3.Connection) -> Application:
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data[KEY_SETTINGS] = settings
    app.bot_data[KEY_DB] = conn
    if settings.anthropic_api_key:
        app.bot_data[KEY_CLASSIFIER] = AnthropicClient(
            settings.anthropic_api_key,
            settings.claude_model,
            classify_model=settings.classify_model,
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set - message parsing is disabled.")

    owner_only = filters.User(user_id=settings.owner_telegram_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=owner_only))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner_only))
    app.add_handler(CommandHandler("recap", cmd_recap, filters=owner_only))
    app.add_handler(CommandHandler("quiet", cmd_quiet, filters=owner_only))
    app.add_handler(CommandHandler("backlog", cmd_backlog, filters=owner_only))
    app.add_handler(CommandHandler("email", cmd_email, filters=owner_only))
    app.add_handler(CommandHandler("help", cmd_help, filters=owner_only))
    app.add_handler(CommandHandler("terms", cmd_terms, filters=owner_only))
    app.add_handler(CommandHandler("teston", cmd_teston, filters=owner_only))
    app.add_handler(CommandHandler("testoff", cmd_testoff, filters=owner_only))
    app.add_handler(
        MessageHandler(owner_only & filters.Document.ALL, on_document)
    )
    app.add_handler(MessageHandler(owner_only & filters.PHOTO, on_photo))
    app.add_handler(
        MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, on_message)
    )
    # Anything from anyone else falls through to here.
    app.add_handler(MessageHandler(~owner_only, on_unknown_user))
    # Last resort, so nothing Kaan sends is ever dropped without a word.
    app.add_handler(MessageHandler(owner_only, on_unhandled))

    app.add_error_handler(on_error)
    _schedule_jobs(app, conn)
    return app


def _schedule_jobs(app: Application, conn: sqlite3.Connection) -> None:
    """Daily brief and the reminder poller, both driven by config values."""
    timezone = ZoneInfo(database.get_config(conn, "timezone", "America/Toronto"))
    raw = database.get_config(conn, "brief_send_time", "07:30")
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        send_at = time(hour, minute, tzinfo=timezone)
    except ValueError:
        logger.warning("brief_send_time %r isn't HH:MM; defaulting to 07:30", raw)
        send_at = time(7, 30, tzinfo=timezone)

    app.job_queue.run_daily(job_morning_brief, send_at, name="morning_brief")

    raw_check = database.get_config(conn, "token_check_time", "08:15")
    try:
        hour, minute = (int(part) for part in raw_check.split(":", 1))
        check_at = time(hour, minute, tzinfo=timezone)
    except ValueError:
        logger.warning("token_check_time %r isn't HH:MM; defaulting to 08:15", raw_check)
        check_at = time(8, 15, tzinfo=timezone)
    app.job_queue.run_daily(job_token_check, check_at, name="token_check")

    raw_evening = database.get_config(conn, "evening_checkin_time", "21:00")
    try:
        hour, minute = (int(part) for part in raw_evening.split(":", 1))
        evening_at = time(hour, minute, tzinfo=timezone)
    except ValueError:
        logger.warning(
            "evening_checkin_time %r isn't HH:MM; defaulting to 21:00", raw_evening
        )
        evening_at = time(21, 0, tzinfo=timezone)
    app.job_queue.run_daily(job_evening_checkin, evening_at, name="evening_checkin")

    interval = int(database.get_config(conn, "reminder_poll_seconds", "60"))
    app.job_queue.run_repeating(
        job_poll_reminders, interval=interval, first=interval, name="reminder_poller"
    )
    logger.info(
        "Scheduled morning brief at %s, evening check-in at %s (%s)",
        send_at.strftime("%H:%M"),
        evening_at.strftime("%H:%M"),
        timezone,
    )


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    setup_logging(settings.log_path)

    conn = database.connect(settings.db_path)
    applied = database.migrate(conn)
    if applied:
        logger.info("Applied migrations: %s", applied)

    build_application(settings, conn).run_polling()


if __name__ == "__main__":
    main()
