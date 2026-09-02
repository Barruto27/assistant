"""Bulk-load starting data (plan Section 3 — onboarding).

    python -m scripts.seed --gym scripts/data/gym.json
    python -m scripts.seed --config scripts/data/semester.json

Both files are plain JSON so they can be hand-edited and re-run. Seeding is
idempotent: gym rows upsert on day_of_week, config rows upsert on key.

Syllabus ingestion is deliberately *not* here — it goes through the Claude
extraction pipeline in Session 5 and then writes tasks via the same code path
the bot uses, so there's only one place that can get course tagging wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bot.errors import setup_logging
from db import database

DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def seed_gym(conn, path: Path) -> int:
    """Load ``{"monday": "Push", "tuesday": "Pull", ...}`` into the gym table."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for day, split in data.items():
        key = day.strip().lower()
        if key not in DAY_NAMES:
            raise SystemExit(f"Unrecognised day {day!r} in {path}")
        rows.append((DAY_NAMES[key], str(split).strip()))

    with database.transaction(conn):
        conn.executemany(
            "INSERT INTO gym (day_of_week, split_name) VALUES (?, ?) "
            "ON CONFLICT(day_of_week) DO UPDATE SET split_name = excluded.split_name",
            rows,
        )
    return len(rows)


def seed_config(conn, path: Path) -> int:
    """Load ``{"semester_start_date": "2026-09-07", ...}`` into the config table."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in data.items():
        database.set_config(conn, key, str(value))
    return len(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gym", type=Path, help="JSON map of weekday -> split name")
    parser.add_argument("--config", type=Path, help="JSON map of config key -> value")
    parser.add_argument("--db", help="SQLite path; defaults to DB_PATH from .env")
    args = parser.parse_args(argv)

    if not args.gym and not args.config:
        parser.error("nothing to do — pass --gym and/or --config")

    if args.db:
        db_path = Path(args.db)
        log_path = Path("logs/assistant.log")
    else:
        from bot.config import ConfigError, load_settings

        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 1
        db_path, log_path = settings.db_path, settings.log_path

    setup_logging(log_path)
    conn = database.connect(db_path)
    try:
        if args.gym:
            print(f"gym: {seed_gym(conn, args.gym)} day(s) loaded")
        if args.config:
            print(f"config: {seed_config(conn, args.config)} key(s) set")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
