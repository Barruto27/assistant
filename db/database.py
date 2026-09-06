"""SQLite connection and migration runner (plan Section 2).

Schema versioning approach
--------------------------
Migrations are plain ``.sql`` files in ``db/migrations/`` named
``NNNN_description.sql``. The runner applies every file whose number is higher
than the highest recorded in ``schema_migrations``, in ascending order, each in
its own transaction. To change the schema, add a new numbered file — never edit
an applied one.

Migration files must not contain their own ``BEGIN``/``COMMIT``/``ROLLBACK``:
the runner wraps each file in a transaction itself. (``executescript`` commits
any *pending* transaction before it runs, so the ``BEGIN`` has to live inside
the script text rather than in a surrounding Python block.) They also must not
contain ``PRAGMA foreign_keys`` — SQLite ignores it inside a transaction, and
``connect()`` sets it on every connection anyway.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from bot.errors import AssistantError, E, logger

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this project assumes everywhere."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False because the bot runs the blocking Claude call in a
    # worker thread and touches the DB there. sqlite3.threadsafety is 3
    # (serialized) so sharing the connection is safe at the driver level; callers
    # that run explicit BEGIN/COMMIT must still serialize themselves so two
    # transactions can't interleave (bot.main holds a lock for exactly this).
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # survives an ungraceful restart
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a transaction, rolling back on any exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for entry in sorted((directory or MIGRATIONS_DIR).glob("*.sql")):
        match = _MIGRATION_RE.match(entry.name)
        if not match:
            raise AssistantError(
                E.DB_MIGRATION,
                f"Migration file {entry.name!r} doesn't match NNNN_description.sql.",
            )
        found.append((int(match.group(1)), entry))

    versions = [version for version, _ in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise AssistantError(
            E.DB_MIGRATION, f"Duplicate migration numbers: {sorted(duplicates)}."
        )
    return found


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration number, or 0 on a fresh database."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
        )
        """
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] or 0


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply every pending migration. Returns the versions applied."""
    applied_now: list[int] = []
    version = current_version(conn)

    for number, path in _discover_migrations(directory):
        if number <= version:
            continue
        script = (
            "BEGIN;\n"
            f"{path.read_text(encoding='utf-8')}\n"
            f"INSERT INTO schema_migrations (version) VALUES ({number});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise AssistantError(
                E.DB_MIGRATION,
                f"Migration {path.name} failed; database left at version {version}.",
                cause=exc,
            ) from exc
        logger.info("Applied migration %s", path.name)
        applied_now.append(number)
        version = number

    return applied_now


# ---------------------------------------------------------------------------
# config key/value helpers — nothing schedule- or semester-related is hardcoded
# ---------------------------------------------------------------------------


def get_config(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return default
    return row["value"]


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
