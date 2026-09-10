"""Import a syllabus PDF from the command line (plan Section 5).

    python -m scripts.import_syllabus path/to/syllabus.pdf
    python -m scripts.import_syllabus syllabus.pdf --dry-run

``--dry-run`` extracts and prints without writing, which is the right way to
check an extraction against the real document before trusting it — the plan's
Section 3 shakedown step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot import syllabus as syl
from bot.errors import AssistantError, setup_logging
from db import database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Path to the syllabus PDF")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and print without writing to the database",
    )
    parser.add_argument("--db", help="SQLite path; defaults to DB_PATH from .env")
    args = parser.parse_args(argv)

    from bot.config import ConfigError, load_settings

    try:
        settings = load_settings(require_anthropic=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.log_path)

    try:
        pdf_bytes = syl.read_pdf(args.pdf)
    except AssistantError as exc:
        print(exc.user_message(), file=sys.stderr)
        return 1

    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)

    print(f"Reading {args.pdf.name} ({len(pdf_bytes) // 1024} KB)...")
    try:
        extracted = syl.extract(pdf_bytes, client, settings.claude_model)
    except AssistantError as exc:
        print(exc.user_message(), file=sys.stderr)
        return 1

    if args.dry_run:
        counts = {
            "tasks": len(extracted.items),
            "topics": len(extracted.weekly_topics),
            "replaced": 0,
        }
        print()
        print(syl.receipt(extracted, counts))
        print()
        print("(dry run - nothing written)")
        return 0

    db_path = Path(args.db) if args.db else settings.db_path
    conn = database.connect(db_path)
    try:
        database.migrate(conn)
        counts = syl.ingest(conn, extracted)
    except AssistantError as exc:
        print(exc.user_message(), file=sys.stderr)
        return 1
    finally:
        conn.close()

    print()
    print(syl.receipt(extracted, counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
