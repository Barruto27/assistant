# Personal Telegram Assistant

A Claude-powered Telegram bot for school and life planning. See
[telegram-assistant-plan.md](telegram-assistant-plan.md) for the full build plan
— each numbered section there is one working session.

**Current state:** Sections 1–11 built and live on the home server. Section 12
(nice-to-haves) deliberately not started; the plan says to leave those until the
core is trusted in daily use.

Text the bot and it saves what you said:

```
you:  test April 13, psyc 3040, ch 3-5
bot:  Saved — Test 2, PSYC 3040, due Mon Apr 13, 20%, P1, ch 3-5
```

One message can hold more than one instruction — "remind me today and tomorrow
to upload the forms" sets two reminders, and each gets its own line in the
receipt.

It only knows what it has been told. Ask about a course with no syllabus on
file and it says so rather than guessing, and it never claims to have saved
something it hasn't.

## Layout

```
bot/        bot code — entry point, config, error codes
db/         schema and migrations
  migrations/   NNNN_description.sql, applied in order
scripts/    operational scripts (init_db, seed, import_syllabus, deploy.sh)
tests/      unittest suite (no extra dependencies)
deploy/     systemd units
site/       the privacy page Google's OAuth consent screen links to
secrets/    OAuth client secret and cached tokens (gitignored)
logs/       assistant.log (gitignored)
```

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
cp .env.example .env                        # then fill it in
python -m scripts.init_db
python -m bot.main
```

Then message the bot `/start` and `/help`.

### What goes in `.env`

| Variable | Needed for | How to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | everything | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_TELEGRAM_ID` | everything | [@userinfobot](https://t.me/userinfobot), or message the running bot and read the `Ignored message from non-owner id=…` line in the log |
| `ANTHROPIC_API_KEY` | reading messages at all | console.anthropic.com |
| `CLAUDE_MODEL` | optional | Defaults to `claude-sonnet-5`. Writes all the prose. |
| `CLASSIFY_MODEL` | optional | Defaults to `claude-haiku-4-5-20251001`. Every message pays for one classification before anything happens, and it is a constrained tool call against a short prompt — a smaller model took ~2.8s off a ~10s reply. |
| `DB_PATH`, `LOG_PATH` | optional | Sensible defaults inside the repo. |
| `GOOGLE_CLIENT_SECRETS`, `GOOGLE_TOKEN_*` | calendar | See [Google Calendar](#google-calendar) below. |
| `GMAIL_IMAP_USER`, `GMAIL_APP_PASSWORD`, `GMAIL_IMAP_HOST` | course email | See [Course email](#course-email) below. |
| `WEATHER_LATITUDE`, `WEATHER_LONGITUDE` | the brief's weather line | Open-Meteo needs no key. |

The bot starts without the Claude and Google values and says plainly which
features are off, rather than failing at the first message.

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

No network, no API key, no cost: the pipeline runs against a scripted
classifier. Several of the files exist because of a specific bug found in real
use, and say so at the top — `test_conversation.py`, `test_locking.py`,
`test_series.py` and `test_flagged.py` are worth reading before changing the
code they cover.

## Deployment

Live on the home server (Ubuntu 24.04) as a dedicated `assistant` account with
no sudo, under **user-level systemd** — so nothing about the running bot needs
root.

Day to day, deploying is one command from a checkout:

```bash
./scripts/deploy.sh              # ship, migrate, test, restart
./scripts/deploy.sh --dry-run    # list exactly what would be sent
```

It ships **source only, by allowlist**. It will not send `db/*.sqlite3`,
`secrets/`, `logs/` or `.env` — an earlier deploy tarred the whole `db/`
directory over the live database and SQLite replayed its WAL into the
replacement, leaving a corrupt hybrid. It also backs the database up first,
runs the suite on the server, and refuses to restart if anything fails.

First-time install:

```bash
mkdir -p ~/.config/systemd/user
cp ~/assistant/deploy/assistant-user.service ~/.config/systemd/user/assistant.service
systemctl --user daemon-reload
systemctl --user enable --now assistant
```

The one step that does need root, once, is letting that account's services run
with nobody logged in:

```bash
sudo loginctl enable-linger assistant
```

Without linger the bot only runs while someone is logged in as `assistant`,
which is never — it would look installed and simply never fire.

`deploy/assistant.service` remains for a system-wide install instead.

Watching it:

```bash
systemctl --user status assistant
journalctl --user -u assistant -f
```

**Only one instance may poll a given bot token.** Two copies fight over
incoming updates: Telegram hands each update to whichever asks first, so replies
go missing at random. The bot no longer messages you about this — being woken at
03:27 for it helped nobody — but `/status` reports how many polling conflicts it
has seen since it started. If that number is not zero, something else is running
with the same token, usually a `python -m bot.main` left open on a laptop.

## Error codes

Every failure carries a code (plan Section 8). You see a short message plus the
code; `logs/assistant.log` gets the stack trace and triggering input.

| Code | Meaning |
| --- | --- |
| `E001` | catch-all — means a code path is missing a specific code |
| `E101`–`E104` | parsing: intent unclear, ambiguous course, missing field, unreadable document |
| `E201`–`E205` | external API: Claude, Calendar, Gmail, Telegram send, weather |
| `E206` | transient network blip — logged, never sent to you, the poller retries |
| `E207` | another instance is polling the same token; surfaces in `/status` |
| `E301`, `E302` | auth — flagged loudly, and checked daily at 08:15 |
| `E401`–`E403` | database write, read, migration |
| `E501`–`E503` | jobs: brief, reminder fire, check-in |

Defined in [bot/errors.py](bot/errors.py).

## Commands

| Command | What it does |
| --- | --- |
| `/help` | Everything below, from inside Telegram |
| `/start` | Confirms the bot is alive |
| `/status` | Schema version, row counts, what's working, polling conflicts |
| `/recap` | Regenerates the morning brief now, from current data |
| `/backlog` | Work set aside as overdue and low-stakes |
| `/email` | Reads course email now and says what in it matters |
| `/terms` | Re-reads term dates and enrolment deadlines from your calendar |
| `/quiet` | Silences the morning brief; `/quiet evening` the check-in |
| `/teston` | Starts a throwaway session — `/testoff` discards everything since |

Send a **PDF** to import it as a syllabus, or a **screenshot** to pull dates out
of it. Any other text goes through classify → handle → receipt.

### Test mode

`/teston` snapshots the database; `/testoff` rolls it back and says what it
threw away. Every reply in between is prefixed so nothing is mistaken for real.
It exists because testing against the live database once left a fabricated
reminder due to fire that evening and two real tasks marked done, one of them an
attendance mark for a lecture that had not happened yet.

Anything genuine entered during a test session is discarded too. That is the
trade.

## Syllabus import

Send the PDF to the bot, or from the command line:

```bash
python -m scripts.import_syllabus path/to/syllabus.pdf --dry-run
```

`--dry-run` extracts and prints without writing — the right way to check an
extraction against the real document before trusting it.

Re-importing the same course **replaces** its syllabus-sourced tasks rather
than duplicating them, and leaves anything added by text alone: a re-import
must not quietly undo your own edits.

The receipt lists every item with its date and weight, and says what fraction
of the grade is accounted for. A total that isn't 100% usually means something
was missed in the PDF, so it says so. Where the extraction was unsure, the
course carries a note saying which parts are worth checking yourself.

## Scheduled jobs

All four are started in `_schedule_jobs` and driven by `config` values, so
changing a time needs no code edit:

- **Morning brief** at `brief_send_time` (default 07:30) in `timezone`.
- **Evening check-in** at `checkin_send_time` (default 21:00) — asks what
  happened today so tasks get marked without you having to think about it.
  Skipped on a day with nothing on it, unless there is flagged email nobody has
  asked you about yet.
- **Token check** at 08:15 — silent unless a Google token has actually died.
- **Reminder poller** every `reminder_poll_seconds` (default 60), sending any
  reminder whose `fire_at` has passed. A send failure leaves the row unsent so
  the next tick retries rather than dropping it.

The bot must actually be running for any of them to fire. On Windows that means
leaving `python -m bot.main` open in a terminal; on the home server that is what
the systemd unit is for.

## Google Calendar

Read-only. The bot never writes to your calendar.

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

On a headless server, run that on a desktop machine and copy the resulting
`secrets/token_personal.json` across — the flow needs a browser.

The brief degrades gracefully: with no token it omits the calendar sections, and
if the token later fails it says so rather than pretending the day was empty.

`/terms` reads academic dates — classes start and end, reading week, exams, and
the add/drop/withdraw deadlines — out of all-day calendar events, so week
numbers come from your real calendar rather than from anything hardcoded.

## Course email

York blocks Google Cloud API access for student accounts, so university mail is
forwarded to a personal Gmail and read over **IMAP with an App Password** — no
OAuth, no consent screen, nothing that expires.

1. Turn on 2-Step Verification for the personal account.
2. Generate an App Password at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Put it in `GMAIL_APP_PASSWORD`, with or without spaces.
4. Set up forwarding from the university account to that address.

Read-only, and it never marks anything read: the mailbox is opened
`readonly=True`. Only mail from senders in the `known_senders` allowlist is
fetched at all — an empty allowlist reads nothing rather than everything.

What it flags is anything that changes what you have to do: deadline moves,
cancellations, newly assigned work, and clarifications — including something
turning out *not* to be required, which is just as worth knowing. Campus
socials, newsletters and marketing are left out.

Flagged mail is **never written to tasks**. It is surfaced in the morning brief,
raised once more in the evening check-in if you have not dealt with it, and then
left alone — an email you have seen and not acted on is a decision, not
something to be nagged about. `/email` reads the mailbox on demand.

## What isn't built

- **Section 12 nice-to-haves** — Spotify link, meal suggestions, web UI. The
  plan says to wait until the core is trusted in daily use.
- **`log_gym`** — the plan's Section 4 lists it among the intents. The gym table
  holds the weekly split, so the brief can say what today is, but there is no
  record of whether you actually went.
- **Calendar writes** — Section 5 lists creating matching events for due dates as
  optional. Deliberately skipped; read-only is easier to trust.
