# Personal Telegram Assistant

A Claude-powered Telegram bot for school and life planning. See
[telegram-assistant-plan.md](telegram-assistant-plan.md) for the full build plan
— each numbered section there is one working session.

**Current state:** Sessions 1, 2, 4, 6 (calendar half) and 7 built. Sessions 3,
5, and 8–11 not started.

Text the bot and it saves what you said:

```
you:  test April 13, psyc 3040, ch 3-5
bot:  Saved — Test 2, PSYC 3040, due Mon Apr 13, 20%, P1, ch 3-5
```

Two things it deliberately can't do yet: answer questions about your data
(needs Session 7's context assembly) and hold a conversation (Session 10). It
says so plainly rather than guessing.

## Layout

```
bot/        bot code — entry point, config, error codes
db/         schema and migrations
  migrations/   NNNN_description.sql, applied in order
scripts/    one-off operational scripts (init_db, seed)
tests/      unittest suite (no extra dependencies)
deploy/     systemd unit
```

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
cp .env.example .env                        # then fill it in
python -m scripts.init_db
python -m bot.main
```

Then message the bot `/start` and `/status`.

### What goes in `.env`

| Variable | Needed by | How to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | now | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_TELEGRAM_ID` | now | [@userinfobot](https://t.me/userinfobot), or message the running bot and read the `Ignored message from non-owner id=…` line in the log |
| `ANTHROPIC_API_KEY` | Session 4 | console.anthropic.com |
| `GOOGLE_*` | Session 6 | see below |

The bot starts fine without the Claude and Google values — it only needs them
once those sessions are built.

## Configuration lives in two places

- **`.env`** — secrets and file paths. Read once at startup.
- **`config` table in SQLite** — anything behavioural: semester dates, brief
  send time, evening check-in time, backlog threshold, quiet toggles. Changed at
  runtime without a redeploy. Defaults are seeded by migration `0001`.

```bash
python -m scripts.seed --config scripts/data/semester.example.json
python -m scripts.seed --gym scripts/data/gym.example.json
python -m scripts.seed --courses scripts/data/courses.example.json
```

All three are idempotent upserts, so edit the JSON and re-run. Load `--courses`
early: the pipeline uses that list to work out which course a message refers
to, and without it every task gets tagged with whatever label you happened to
type.

## Schema changes

Add a new `db/migrations/NNNN_description.sql`; never edit an applied one. The
runner wraps each file in a transaction, so a failure rolls the whole file back
and leaves the recorded version untouched. Migration files must not contain
their own `BEGIN`/`COMMIT` or `PRAGMA foreign_keys` — the runner owns both.

```bash
python -m scripts.init_db   # applies anything pending; safe to re-run
```

## Tests

```bash
python -m unittest discover -s tests
```

## Deployment

`deploy/assistant.service` is a systemd unit with restart-on-crash and
start-on-boot. Edit `User` and `WorkingDirectory`, then:

```bash
sudo cp deploy/assistant.service /etc/systemd/system/ && sudo systemctl enable --now assistant
```

## Error codes

Every failure carries a code (plan Section 8). Kaan sees a short message plus
the code; `logs/assistant.log` gets the stack trace and triggering input.

| Range | Meaning |
| --- | --- |
| `E001` | catch-all — means a code path is missing a specific code |
| `E1xx` | parsing / classification |
| `E2xx` | external API (Claude, Calendar, Gmail, Telegram, weather) |
| `E3xx` | auth — `E301`/`E302` are flagged loudly in the user-facing message |
| `E4xx` | database |
| `E5xx` | scheduler / jobs |

Defined in [bot/errors.py](bot/errors.py).

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Confirms the bot is alive |
| `/status` | Schema version, whether parsing is on, row counts |
| `/recap` | Regenerates the morning brief now, from current data |
| `/quiet` | Toggles the scheduled morning brief off and on |

Any other text goes through the classify -> handle -> receipt pipeline.

## Scheduled jobs

Both are started in `_schedule_jobs` and driven by `config` values, so changing
a time needs no code edit:

- **Morning brief** at `brief_send_time` (default 07:30) in `timezone`.
- **Reminder poller** every `reminder_poll_seconds` (default 60), sending any
  reminder whose `fire_at` has passed. A send failure leaves the row unsent so
  the next tick retries rather than dropping it.

The bot must actually be running for either to fire. On Windows that means
leaving `python -m bot.main` open in a terminal; on the home server that is what
the systemd unit is for.

## Google Calendar

One-time setup in the [Google Cloud console](https://console.cloud.google.com):

1. Create a project, then enable the **Google Calendar API**.
2. **APIs & Services > Credentials > Create OAuth client ID > Desktop app.**
   Download the JSON to `secrets/google_client_secret.json`.
3. **Publish the OAuth consent screen.** Left in Testing, Google expires the
   refresh token every 7 days and the calendar silently goes stale. Publishing
   an unverified single-user app is fine and takes one click.

Then authorise once:

```bash
python -m scripts.google_auth
```

The brief degrades gracefully: with no token it simply omits the calendar
sections, and if the token later fails it says so rather than pretending the day
was empty.

## Remaining Session 1 checklist

These need your accounts and can't be done from code:

- [ ] Create the bot with @BotFather, put the token in `.env`
- [ ] Put your Telegram user ID in `.env`
- [ ] Create the Google Cloud project; enable Calendar API and Gmail API (readonly)
- [ ] **Publish the OAuth consent screen right away** — apps left in "testing"
      expire refresh tokens every 7 days
- [ ] Separate OAuth credentials for the university account; confirm whether the
      Workspace admin policy allows third-party API access at all. If blocked,
      fall back to a forwarding rule or IMAP (plan Section 6)
- [ ] Install the systemd unit on the home server
