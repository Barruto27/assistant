"""Create or upgrade the database.

    python -m scripts.init_db            # uses DB_PATH from .env
    python -m scripts.init_db --db x.db  # or an explicit path

Safe to re-run: only migrations newer than the recorded version are applied.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot.errors import AssistantError, setup_logging
from db import database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        help="Path to the SQLite file. Defaults to DB_PATH from .env.",
    )
    args = parser.parse_args(argv)

    if args.db:
        db_path = Path(args.db)
        log_path = Path("logs/assistant.log")
    else:
        # Import lazily so --db works without a populated .env.
        from bot.config import ConfigError, load_settings

        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            print("Tip: pass --db to point at a file directly.", file=sys.stderr)
            return 1
        db_path = settings.db_path
        log_path = settings.log_path

    setup_logging(log_path)

    conn = database.connect(db_path)
    try:
        before = database.current_version(conn)
        applied = database.migrate(conn)
        after = database.current_version(conn)
    except AssistantError as exc:
        print(exc.user_message(), file=sys.stderr)
        return 1
    finally:
        conn.close()

    if applied:
        print(f"{db_path}: v{before} -> v{after} (applied {applied})")
    else:
        print(f"{db_path}: already at v{after}, nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
