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

from bot import router
from bot.claude_client import AnthropicClassifier
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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single funnel for every failure: log the detail, tell Kaan the code."""
    err = context.error
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
        app.bot_data[KEY_CLASSIFIER] = AnthropicClassifier(
            settings.anthropic_api_key, settings.claude_model
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set - message parsing is disabled.")

    owner_only = filters.User(user_id=settings.owner_telegram_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=owner_only))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner_only))
    app.add_handler(
        MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, on_message)
    )
    # Anything from anyone else falls through to here.
    app.add_handler(MessageHandler(~owner_only, on_unknown_user))

    app.add_error_handler(on_error)
    return app


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
