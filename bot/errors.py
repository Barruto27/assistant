"""Error codes and logging (plan Section 8).

Every failure that reaches Kaan carries a short plain-language message plus a
code in parentheses. The log file gets the full detail: timestamp, code, stack
trace, and the input that triggered it.

Usage from a handler::

    from bot.errors import AssistantError, E

    try:
        ...
    except sqlite3.Error as exc:
        raise AssistantError(E.DB_WRITE, "Couldn't save that.", cause=exc,
                             trigger=message.text) from exc

The top-level handler catches AssistantError, sends ``err.user_message()`` to
Telegram, and calls ``log_error(err)``.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

logger = logging.getLogger("assistant")


class E:
    """The Section 8 code scheme.

    E1xx parsing/classification, E2xx external APIs, E3xx auth,
    E4xx database, E5xx scheduler/jobs.
    """

    # E0xx — catch-all for anything that escaped a typed handler. Seeing this in
    # the log means a code path is missing its specific code; go add one.
    UNEXPECTED = "E001"

    # E1xx — parsing / classification
    INTENT_UNCLEAR = "E101"
    AMBIGUOUS_COURSE = "E102"
    MISSING_FIELD = "E103"
    UNPARSEABLE_DOCUMENT = "E104"

    # E2xx — external API failures
    CLAUDE = "E201"
    CALENDAR = "E202"
    GMAIL = "E203"
    TELEGRAM_SEND = "E204"
    WEATHER = "E205"
    # Transient connectivity. Logged, never escalated to Kaan: the polling
    # loop retries on its own, so a message about it is pure noise.
    TRANSIENT_NETWORK = "E206"

    # E3xx — auth
    TOKEN_EXPIRED = "E301"
    REFRESH_FAILED = "E302"

    # E4xx — database
    DB_WRITE = "E401"
    DB_READ = "E402"
    DB_MIGRATION = "E403"

    # E5xx — scheduler / jobs
    BRIEF_FAILED = "E501"
    REMINDER_FIRE_FAILED = "E502"
    CHECKIN_FAILED = "E503"


#: Codes that must be impossible to miss when they land in Telegram.
LOUD_CODES = {E.TOKEN_EXPIRED, E.REFRESH_FAILED}


class AssistantError(Exception):
    """An error with a code, a user-facing message, and debugging context."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        cause: BaseException | None = None,
        trigger: str | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.cause = cause
        self.trigger = trigger

    def user_message(self) -> str:
        """Short plain-language text for Telegram, with the code appended."""
        if self.code in LOUD_CODES:
            return f"⚠️ ACTION NEEDED — {self.message} ({self.code})"
        return f"{self.message} ({self.code})"


def setup_logging(log_path: str | Path, *, level: int = logging.INFO) -> None:
    """Configure the 'assistant' logger: rotating file + console."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # python-telegram-bot and httpx are chatty at INFO; keep the log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def log_error(err: AssistantError) -> None:
    """Write the full detail of an AssistantError to the log."""
    detail = f"[{err.code}] {err.message}"
    if err.trigger:
        detail += f" | trigger: {err.trigger!r}"
    logger.error(detail, exc_info=err.cause)
