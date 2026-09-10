#!/usr/bin/env bash
# Ship code to the server. Never runtime state.
#
# Written after a deploy that tarred the whole db/ directory and overwrote the
# live database with a local development copy. Worse, the running bot still had
# the original open, so SQLite replayed its WAL into the replacement and the
# result was a corrupt hybrid: 66 tasks and no courses.
#
# Two rules come out of that, and this script enforces both:
#   1. Only source is ever copied. Never db/, secrets/, logs/, or .env.
#   2. The service is stopped before files move and started after, so nothing
#      is holding a file open while it changes underneath.
#
#   ./scripts/deploy.sh            deploy and restart
#   ./scripts/deploy.sh --dry-run  list exactly what would be sent

set -euo pipefail

HOST="${DEPLOY_HOST:-assistant-server}"
REMOTE="${DEPLOY_PATH:-/home/assistant/assistant}"

# The allowlist. Adding a directory here is a deliberate act; anything not
# named is not shipped, which is the safe default for a repo that also holds
# the database and the credentials.
PATHS=(bot db/migrations scripts tests deploy site README.md requirements.txt)

for p in "${PATHS[@]}"; do
  case "$p" in
    db|db/|secrets*|logs*|.env*)
      echo "refusing to deploy runtime state: $p" >&2
      exit 1
      ;;
  esac
done

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "would send to ${HOST}:${REMOTE}"
  tar czf - "${PATHS[@]}" --exclude='__pycache__' --exclude='*.pyc' \
    | tar tzf - | sed 's/^/  /'
  exit 0
fi

echo "==> stopping service"
ssh -o BatchMode=yes "$HOST" 'systemctl --user stop assistant'

echo "==> backing up the database first"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE && cp db/assistant.sqlite3 \
  db/backups/assistant-\$(date +%Y%m%d-%H%M%S).sqlite3 2>/dev/null || \
  { mkdir -p db/backups && cp db/assistant.sqlite3 \
    db/backups/assistant-\$(date +%Y%m%d-%H%M%S).sqlite3; }"

echo "==> sending code"
tar czf - "${PATHS[@]}" --exclude='__pycache__' --exclude='*.pyc' \
  | ssh -o BatchMode=yes "$HOST" "tar xzf - -C $REMOTE"

echo "==> applying migrations"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE && .venv/bin/python -m scripts.init_db"

echo "==> running tests on the server"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3"

echo "==> starting service"
ssh -o BatchMode=yes "$HOST" 'systemctl --user start assistant && sleep 5 && systemctl --user is-active assistant'

echo "==> integrity check"
ssh -o BatchMode=yes "$HOST" "cd $REMOTE && .venv/bin/python -c \"
import sqlite3
c = sqlite3.connect('db/assistant.sqlite3')
print('  integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('  tasks    :', c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
\""

echo "done"
