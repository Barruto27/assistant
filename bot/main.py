"""Bot entry point (plan Sections 1 and 4).

Run from the repo root::

    python -m bot.main

Answers /start and /status, and routes any other text through the classify ->
handle -> receipt pipeline in ``bot.router``. Everything is restricted to
OWNER_TELEGRAM_ID; other senders are logged and ignored without a reply.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import (
    brief,
    email_reader,
    google_calendar,
    repository as repo,
    router,
    syllabus as syl,
)
from bot.claude_client import AnthropicClient
from bot.config import ConfigError, Settings, load_settings
from bot.errors import AssistantError, E, log_error, logger, setup_logging
from db import database

# Stashed on Application.bot_data so handlers can reach them without globals.
KEY_SETTINGS = "settings"
KEY_DB = "db"
KEY_CLASSIFIER = "classifier"

# The Claude call is blocking, so it runs in a worker thread. This lock keeps two
# messages from interleaving their DB transactions while it does.
_db_lock = threading.Lock()


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data[KEY_SETTINGS]


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.application.bot_data[KEY_DB]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Up and running. Text me a task, a reminder, or a goal and I'll save it. "
        "/status shows what's on file."
    )


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
    rows = "\n".join(f"  {name}: {n}" for name, n in counts.items())
    await update.effective_message.reply_text(
        f"<b>Schema</b> v{version}\n"
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

    def work() -> str:
        with _db_lock:
            return router.handle_message(conn, classifier, text)

    # Off the event loop: the Anthropic SDK call is synchronous and would
    # otherwise stall every other update while it waits.
    reply = await asyncio.to_thread(work)
    await update.effective_message.reply_text(reply)


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

    await message.chat.send_action("typing")
    telegram_file = await document.get_file()
    pdf_bytes = bytes(await telegram_file.download_as_bytearray())

    conn = _db(context)

    def work() -> str:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key)
        extracted = syl.extract(pdf_bytes, client, settings.claude_model)
        with _db_lock:
            counts = syl.ingest(conn, extracted)
        return syl.receipt(extracted, counts)

    await message.reply_text(await asyncio.to_thread(work))


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerate the brief on demand, always from current data."""
    await update.effective_message.chat.send_action("typing")
    text = await asyncio.to_thread(_build_brief, context.application)
    await update.effective_message.reply_text(text)


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the morning brief off or on."""
    conn = _db(context)
    with _db_lock:
        now_quiet = database.get_config(conn, "quiet_morning", "0") == "1"
        database.set_config(conn, "quiet_morning", "0" if now_quiet else "1")
    await update.effective_message.reply_text(
        "Morning brief back on." if now_quiet else "Morning brief off. /quiet again to undo."
    )


def _gather_email(app: Application, conn: sqlite3.Connection) -> list:
    """Fetch and flag course email. Returns [] rather than raising.

    Kept out of brief.assemble because flagging needs a Claude call and stage 1
    stays free of those. Failures are logged and reported as an unavailable
    subsystem, never allowed to take the whole brief down.
    """
    settings: Settings = app.bot_data[KEY_SETTINGS]
    if not (settings.gmail_imap_user and settings.gmail_app_password):
        return []

    with _db_lock:
        senders = email_reader.known_senders(conn)
    if not senders:
        return []

    messages = email_reader.fetch(
        host=settings.gmail_imap_host,
        user=settings.gmail_imap_user,
        password=settings.gmail_app_password,
        senders=senders,
        since=email_reader.default_window(),
    )
    if not messages or not settings.anthropic_api_key:
        return []

    from anthropic import Anthropic

    return email_reader.flag(
        messages, Anthropic(api_key=settings.anthropic_api_key), settings.claude_model
    )


def _build_brief(app: Application) -> str:
    """Assemble and write the brief. Blocking; call via asyncio.to_thread."""
    settings: Settings = app.bot_data[KEY_SETTINGS]
    conn: sqlite3.Connection = app.bot_data[KEY_DB]
    writer = app.bot_data.get(KEY_CLASSIFIER)

    flagged: list = []
    email_failed = False
    try:
        flagged = _gather_email(app, conn)
    except AssistantError as err:
        log_error(err)
        email_failed = True

    with _db_lock:
        timezone = database.get_config(conn, "timezone", "America/Toronto")
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
        )
    if email_failed:
        context.unavailable.append("email")
    return brief.generate(context, writer)


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


def _is_transient(err: BaseException | None) -> bool:
    """True for connectivity blips the polling loop already retries.

    Telegram returns Bad Gateway and friends routinely; python-telegram-bot's
    network_retry_loop recovers without help. Escalating those to Kaan means a
    "something broke" text for every hiccup — and the reply usually fails too,
    because the network is exactly what's down.
    """
    from telegram.error import NetworkError, TimedOut

    return isinstance(err, (NetworkError, TimedOut))


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
            settings.anthropic_api_key, settings.claude_model
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set - message parsing is disabled.")

    owner_only = filters.User(user_id=settings.owner_telegram_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=owner_only))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner_only))
    app.add_handler(CommandHandler("recap", cmd_recap, filters=owner_only))
    app.add_handler(CommandHandler("quiet", cmd_quiet, filters=owner_only))
    app.add_handler(
        MessageHandler(owner_only & filters.Document.ALL, on_document)
    )
    app.add_handler(
        MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, on_message)
    )
    # Anything from anyone else falls through to here.
    app.add_handler(MessageHandler(~owner_only, on_unknown_user))

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

    interval = int(database.get_config(conn, "reminder_poll_seconds", "60"))
    app.job_queue.run_repeating(
        job_poll_reminders, interval=interval, first=interval, name="reminder_poller"
    )
    logger.info("Scheduled morning brief at %s (%s)", send_at.strftime("%H:%M"), timezone)


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
