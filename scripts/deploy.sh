#!/usr/bin/env bash
# Ship code to the server. Never runtime state.
#
# Written after a deploy that tarred the whole db/ directory and overwrote the
# live database with a local development copy. Worse, the running bot still had
# the original open, so SQLite replayed its WAL into the replacement and the
# result was a corrupt hybrid: 66 tasks and no courses.
#
# Three rules come out of that, and this script enforces all of them:
#   1. Only source is copied. Never db/*.sqlite3, secrets/, logs/, or .env.
#   2. The service stops before files move and starts after, so nothing holds a
#      file open while it changes underneath.
#   3. The database is backed up before anything is touched.
#
#   ./scripts/deploy.sh            deploy and restart
#   ./scripts/deploy.sh --dry-run  list exactly what would be sent

set -uo pipefail

HOST="${DEPLOY_HOST:-assistant-server}"
REMOTE="${DEPLOY_PATH:-/home/assistant/assistant}"

# The allowlist. Adding a path here is a deliberate act; anything not named is
# not shipped, which is the safe default for a repo that also holds the
# database and the credentials.
# db/ is named file by file, never as a directory: the live database and its
# backups live there too, and tarring the directory is what corrupted it once
# already. Leaving db/database.py out was the opposite mistake - bot/ shipped
# expecting a helper the server did not have, and the bot would not start.
PATHS=(bot db/__init__.py db/database.py db/migrations scripts tests deploy
       site README.md requirements.txt)

for p in "${PATHS[@]}"; do
  case "$p" in
    db|db/|secrets|secrets/*|logs|logs/*|.env*|*.sqlite3*|db/backups*)
      echo "refusing to deploy runtime state: $p" >&2
      exit 1
      ;;
  esac
done

# --exclude must precede every non-option argument, and "-" for stdout counts
# as one. GNU tar applies these positionally and only warns if they come later.
pack() {
  tar --exclude='__pycache__' --exclude='*.pyc' --exclude='*.sqlite3*' \
      -czf - "${PATHS[@]}"
}

if [ "${1:-}" = "--dry-run" ]; then
  echo "would send to ${HOST}:${REMOTE}"
  pack | tar tzf - | sed 's/^/  /'
  exit 0
fi

run() { ssh -o BatchMode=yes "$HOST" "$@"; }

echo "==> stopping service"
run 'systemctl --user stop assistant'

echo "==> backing up the database"
run "cd $REMOTE && mkdir -p db/backups && cp db/assistant.sqlite3 db/backups/assistant-\$(date +%Y%m%d-%H%M%S).sqlite3 && ls -t db/backups | head -1"

# Keep the code that is currently working, so a bad deploy can be undone.
# Source only - the same allowlist, so this can never capture the database.
echo "==> snapshotting the running code"
run "cd $REMOTE && mkdir -p .rollback && tar --exclude='__pycache__' --exclude='*.pyc' \
     --exclude='*.sqlite3*' -czf .rollback/previous.tar.gz ${PATHS[*]} 2>/dev/null; \
     ls -l .rollback/previous.tar.gz | awk '{print \"  \" \$5, \"bytes\"}'"

rollback() {
  # Only as good as the last state that actually ran. If the previous deploy
  # was itself broken, this restores broken code - it buys a known starting
  # point, not a guarantee.
  echo "==> rolling back to the previous code" >&2
  run "cd $REMOTE && tar xzf .rollback/previous.tar.gz"
  run 'systemctl --user reset-failed assistant 2>/dev/null || true'
  run 'systemctl --user start assistant && sleep 5 && systemctl --user is-active assistant'
}

echo "==> sending code"
if ! pack | run "tar xzf - -C $REMOTE"; then
  echo "TRANSFER FAILED - restoring and stopping here" >&2
  rollback
  exit 1
fi

echo "==> applying migrations"
run "cd $REMOTE && .venv/bin/python -m scripts.init_db 2>&1 | grep -v INFO"

# No pipe here. Piping unittest into grep hands the script grep's exit status,
# which is why a suite with six errors once sailed through and the bot was
# started on code that could not import.
echo "==> running tests on the server"
if ! run "cd $REMOTE && .venv/bin/python -m unittest discover -s tests > /tmp/deploy-tests.log 2>&1"; then
  echo "TESTS FAILED on the server - this code is not going live" >&2
  run "grep -E '^(FAIL|ERROR|Ran |FAILED)' /tmp/deploy-tests.log | head -20" >&2
  rollback
  exit 1
fi
run "grep -E '^(Ran |OK)' /tmp/deploy-tests.log"

echo "==> starting service"
run 'systemctl --user reset-failed assistant 2>/dev/null || true'
if ! run 'systemctl --user start assistant && sleep 5 && systemctl --user is-active assistant'; then
  echo "SERVICE DID NOT COME UP - restoring the previous code" >&2
  run 'journalctl --user -u assistant --since "-2 min" --no-pager | tail -20' >&2
  rollback
  exit 1
fi

echo "==> integrity"
run "cd $REMOTE && .venv/bin/python -c \"
import sqlite3
c = sqlite3.connect('db/assistant.sqlite3')
print('  integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('  tasks    :', c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
c.close()\""

echo "done"
