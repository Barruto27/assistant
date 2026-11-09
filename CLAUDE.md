# Working on this bot

Kaan's personal Telegram assistant: coursework, calendar, email, reminders, a
morning brief and an evening check-in. Read
[telegram-assistant-plan.md](telegram-assistant-plan.md) for what it is meant to
be and [README.md](README.md) for how it runs. This file is for whoever picks
the work up next.

---

## Read this before anything else

**The Anthropic API key is rejected — 401 `invalid x-api-key`.** Nothing that
needs a model has worked since **16 September 2026**. Fix this before touching
anything else; most of what looks broken downstream is this.

What that means right now:

- **Morning briefs still arrive**, but as the plain fallback — accurate data,
  no quotation, no prose, no email section. Check `logs/assistant.log` for
  `[E501] Falling back to the plain brief` immediately before each send.
- **Evening check-ins fail outright** with `[E503]`. There is no fallback on
  that path, unlike the brief. That is a real gap worth closing.
- **The bot cannot read messages at all.** It replies with the top-up link.
- **Reminders still fire.** The poller needs no API.

The database has been frozen since September as a result: 58 tasks, exactly one
marked done. That is not a bug, it is two months of a bot nobody could talk to.

Put a working key in `ANTHROPIC_API_KEY` in `/home/assistant/assistant/.env`,
`systemctl --user restart assistant`, then confirm with a `/recap` in Telegram —
if the opener is a quotation, the model path is alive again.

---

## Where things are

| | |
|---|---|
| Server | `ssh assistant-server`, code at `/home/assistant/assistant` |
| Service | user systemd: `systemctl --user {status,restart} assistant` |
| Logs | `logs/assistant.log`, and `journalctl --user -u assistant -f` |
| Database | `db/assistant.sqlite3`, schema **v10**, backups in `db/backups/` |
| Secrets | `.env` and `secrets/` — gitignored, never deployed |
| Deploy | `./scripts/deploy.sh` from a checkout (`--dry-run` to see what ships) |
| Tests | `python -m unittest discover -s tests` — 454, no network, no key |

**Git is not how code reaches the server.** `deploy.sh` copies files by
allowlist; the git repo sitting on the server is stale and irrelevant. Deploy
from your own checkout.

## Branch state

`conversation-hardening` is **deployed and 6 commits ahead of `main`**, which is
unusual and deliberate — it was tested but never merged. It holds the
clarification memory, the check-in routing fixes and the email-injection
hardening. `bot/clarify.py`, `bot/quotes.py` and `checkin.addressable()` are on
the server, so it is live.

Merge it or discard it, but do not leave it in this state much longer. To roll
back: `git checkout main && ./scripts/deploy.sh`.

`scripts/lab/` was committed after that deploy and is **not** on the server.

---

## Rules that came from real damage

Each of these cost something. They are not style preferences.

**Never say you recorded something unless you did.** On 10 September the bot
told Kaan it had marked two attendance marks done and had marked neither — no
row in the database had ever been `done`. A bot that quietly does nothing is a
nuisance; one that confirms a write it did not make is worse than no bot,
because he stops checking. The read-only paths (`query.answer`, `just_chat`) are
explicitly forbidden from claiming writes. Keep it that way.

**Use `/teston` before poking at the live bot.** Testing against the live
database once left a fabricated reminder due to fire that evening and two real
tasks marked done, one for a lecture that had not happened. `/testoff` discards
everything since. Anything genuine entered meanwhile is discarded too.

**Never deploy runtime state.** A deploy that tarred the whole `db/` directory
overwrote the live database, and SQLite replayed the running bot's WAL into the
replacement — a corrupt hybrid, 66 tasks and no courses. `deploy.sh` now ships
source by allowlist and refuses `db/`, `secrets/`, `logs/`, `.env`. Do not widen
that list casually.

**Copy `-wal` and `-shm` with any SQLite file, or delete them first.** The same
hybrid bit the test harness: it copied the database per case but left the
previous case's WAL, which replayed onto the fresh copy. One test answered with
another test's data and looked like a model failure for an hour.

**Dry-running the morning brief writes to the database.** `_build_brief` records
flagged emails, marks them raised and consumes a quotation. Run it against a
copy, or tomorrow's brief treats today's mail as old news. (Done it. Had to undo
it.)

**Do not put code with backslashes or em dashes through a shell heredoc.** It
collapses `\n` into real newlines and mangles non-ASCII, silently. Write the
patch to a file and run the file. Always `ast.parse` after writing and assert
your anchors before `write_text`, so a missed anchor fails before it corrupts
anything.

**A function that takes a `now` must write it.** `set_goal` accepted a `now`,
used it for `expires_at`, and left `created_at` to the column default —
`strftime('now','localtime')`, the real clock. `stalled_goals` measures quiet
time from `created_at`, so a test asking for a goal created thirty days ago got
one created this morning. It passed in September because the fixture date and
the real date were weeks apart, and broke in November when they were not, with
nothing changed in between. Production never saw it: there `now` defaults to
`datetime.now()`, so the written value matched the default anyway. That is what
made it survive — the bug was invisible exactly where it did no harm.

`goals.created_at` was the only stored timestamp driving time-based logic, and
`set_goal` the only writer ignoring its own `now`; `add_task`, `add_reminder`,
`save_note` and `remember_flagged` take no `now` and nothing compares their
stamps against a clock. Keep it that way, and if you add a column that both
defaults to the real clock and later gets compared to a passed-in time, write it
explicitly.

**A test that fails with nothing changed is usually telling you the truth.**
That one looked like date drift to be waited out. It was a real defect, and
`deploy.sh` caught it on the server and rolled back before it went live — which
is the whole reason the test gate is there. Do not reach for `--no-verify`
thinking or wave a red suite through because the diff looks unrelated.

**Test against real data.** Synthetic rows hide the bugs that matter — every
finding in `scripts/lab/` came from a copy of the real database. A copy starts
with one task already `done`, so assert on what *changed*, not on row counts.

## How Kaan wants this run

> "Priority is to keep everything controlled though and if uncertain always ask
> me what you should do."

Concretely: email access is **read-only**, the mailbox is opened `readonly=True`
and nothing is ever marked read. Google Calendar is **read-only** — the bot
never writes to a calendar he maintains by hand. Nothing found in email becomes
a task without him confirming. There is no delete path for tasks at all;
archiving is the most destructive thing available.

---

## Known gaps

**The database lock is held across model calls.** `work()` in `bot/main.py`
holds `_db_lock` for the whole of `handle_message`, including the Claude call.
While it runs, nothing else touches the database — no reminder fires, no other
message is answered. A 40s timeout bounds the damage (it was 600s with two
retries, so half an hour), but the freeze is structural and unfixed. This is the
most worthwhile piece of engineering left.

**The evening check-in has no fallback.** The brief degrades to `render_plain`
when the model is unavailable; the check-in just dies with `[E503]`. Two months
of evidence that this matters.

**`log_gym` does not exist.** Plan Section 4 lists it among the intents.
`set_gym_split` records which split falls on which weekday, so the brief can say
what today is, but nothing records whether he went. Needs a schema change, so it
is a decision rather than an oversight.

**Only three courses are on file** — CMDS 1630, DATT 1200, PSYC 3265. NATS 1505
has never been imported. All three `courses.verify_notes` are empty because they
were imported before the extraction started recording what it was unsure about;
re-sending the PDFs would populate them.

**A check-in correction may not find its target.** "did the reflection" then
"wait no I didn't, I started it" routes correctly but can land on the wrong row.
Three attempts at prompt wording failed. What is enforced instead is the
invariant — an attendance mark has no half-way state, so marking one `started`
is refused outright. Wrong data became no data; the correction itself is still
imperfect.

## Testing

`python -m unittest discover -s tests` — 454 tests, no network, no API key, runs
in seconds. The deploy script runs it on the server and refuses to restart if
anything fails.

Several test files exist because of one specific bug and say so at the top.
Read the docstring before changing the code it covers: `test_conversation.py`,
`test_clarify.py`, `test_correction.py`, `test_flagged.py`, `test_weeks.py`,
`test_testmode.py`.

`scripts/lab/` is separate and deliberately not in that suite — it drives the
**real** classifier against a copy of the real database, so it costs credits and
needs `.env`. A scripted classifier proves the plumbing carries an intent; it
cannot prove the model picks the right one, and both serious findings in
September were the second kind. 54 cases across four files, all passing as of 13
September. See `scripts/lab/README.md`.

## When you have changed something

```bash
python -m unittest discover -s tests   # must be green before deploying
./scripts/deploy.sh --dry-run          # confirm what ships
./scripts/deploy.sh                    # backs up the DB, tests on the server, restarts
```

Then check the service came back and the log is clean:

```bash
ssh assistant-server 'systemctl --user is-active assistant; tail -5 /home/assistant/assistant/logs/assistant.log'
```

**Only one process may poll a given bot token.** Two copies fight over updates
and replies vanish at random. A local `python -m bot.main` on Kaan's laptop
caused two 03:00 error texts in September. `/status` now reports polling
conflicts since start; if that number is not zero, something else is running.
