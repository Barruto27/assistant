"""Environment configuration (plan Section 1).

Secrets and paths come from ``.env``; everything behavioural (send times,
semester dates, thresholds) lives in the database ``config`` table instead —
see ``db.database.get_config``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _path(name: str, default: str) -> Path:
    raw = os.getenv(name, "").strip() or default
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_telegram_id: int
    anthropic_api_key: str | None
    claude_model: str
    db_path: Path
    log_path: Path
    google_client_secrets: Path
    google_token_personal: Path
    google_token_university: Path
    weather_latitude: float
    weather_longitude: float
    weather_timezone: str


def load_settings(*, require_anthropic: bool = False) -> Settings:
    """Read ``.env`` and validate what this run actually needs.

    ``require_anthropic`` is False during Sessions 1-3, where no Claude call is
    made yet — the bot should still start without a key.
    """
    load_dotenv(REPO_ROOT / ".env")

    owner_raw = _require("OWNER_TELEGRAM_ID")
    try:
        owner_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError(
            f"OWNER_TELEGRAM_ID must be a number, got {owner_raw!r}."
        ) from exc

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    if require_anthropic and not anthropic_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set but this run needs it.")

    def _float(name: str, default: str) -> float:
        raw = os.getenv(name, "").strip() or default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc

    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        owner_telegram_id=owner_id,
        anthropic_api_key=anthropic_key,
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-sonnet-5",
        db_path=_path("DB_PATH", "db/assistant.sqlite3"),
        log_path=_path("LOG_PATH", "logs/assistant.log"),
        google_client_secrets=_path(
            "GOOGLE_CLIENT_SECRETS", "secrets/google_client_secret.json"
        ),
        google_token_personal=_path(
            "GOOGLE_TOKEN_PERSONAL", "secrets/token_personal.json"
        ),
        google_token_university=_path(
            "GOOGLE_TOKEN_UNIVERSITY", "secrets/token_university.json"
        ),
        weather_latitude=_float("WEATHER_LATITUDE", "43.7735"),
        weather_longitude=_float("WEATHER_LONGITUDE", "-79.5019"),
        weather_timezone=os.getenv("WEATHER_TIMEZONE", "").strip() or "America/Toronto",
    )
