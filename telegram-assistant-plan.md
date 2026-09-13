# Personal Telegram Assistant — Project Plan

**Owner:** Kaan
**Purpose:** A Claude API–powered Telegram bot that acts as a personal assistant for school and life planning — calendar, tasks, reminders, gym tracking, goals, and daily check-ins. Built to minimize the number of texts required, and to talk like a supportive friend who knows what matters to you, not a productivity app.

**How to use this document:** Each numbered section below is a self-contained working session. Work through them roughly in order — later sessions depend on earlier ones being solid. Each session should end with something *working and testable*, not just written. Don't start a session until the previous one is stable — bugs compound in a system this interconnected.

---

## Status — 12 September 2026

Sections 1-11 are built and running on the home server. Section 12 is
deliberately untouched: the plan says to leave the nice-to-haves until the core
is trusted in daily use, and it is only a few days old.

Four boxes below are still open on purpose:

- **Section 3, verifying extractions by hand.** The three syllabi on file were
  imported before the extraction started recording what it was unsure about, so
  their `courses.verify_notes` are empty. Re-sending them would populate those.
  NATS 1505 has never been imported at all.
- **Section 5, writing due dates to Google Calendar.** Listed as optional.
  Skipped on purpose: read-only access is easier to trust, and it means the bot
  can never damage a calendar he maintains by hand.
- **Section 11, the check-in asking for tomorrow's goal when none is set.** The
  check-in captures a goal if he volunteers one, but never asks.
- **Section 12** in full.

One thing in Section 4 is worth naming rather than hiding behind a tick: the
intent list there includes `log_gym`, and there is no such intent. `set_gym_split`
records which split falls on which weekday, so the brief can say what today is,
but nothing records whether he actually went. Building it means a schema change,
so it is a decision rather than an oversight.

Two things are built that the plan never asked for, both because real use
demanded them:

- **Test mode** (`/teston` / `/testoff`). Testing against the live database left
  a fabricated reminder due to fire that evening and two real tasks marked done.
- **`/email` and the `check_email` intent.** Section 6 surfaces email in the
  brief, which left no way to ask about it; asking went to the query path, which
  sees only saved data and so always answered that there was nothing.

---

## 0. Guiding principles (apply to every session)

These aren't optional per-feature choices — they're constraints every subsystem should follow:

1. **Minimize round-trips.** Default to one text in, one informational receipt back. Only ask a clarifying question when the ambiguity genuinely can't be resolved from context (e.g., two courses both have "Assignment 3" and neither was named). Never chain multiple confirmation questions in one flow.
2. **Receipts, not approval gates.** After a write (new task, reminder, syllabus import), reply with what was understood and saved. Don't block the write on a "yes/no, confirm?" — let Kaan correct after the fact if something's wrong.
3. **Facts and trade-offs, not persuasion.** The bot should lay out the real situation (what's due, what time is left, what the cost of a choice is) and let Kaan reach his own conclusion. No moralizing, no manufactured urgency, no guilt.
4. **Stay in the now.** Old, low-priority overdue items should quietly fall out of daily view (backlog) rather than nag forever. High-priority items (exams, major weighted work) stay visible even overdue.
5. **Every write path needs a defined failure behavior.** Clean success / ambiguous (ask) / unparseable (say so plainly) — never silent failure, never a guessed write with no visibility.
6. **Every external write is logged with a custom error code** (see Section 8) so failures are debuggable without re-reading stack traces from scratch.
7. **Build for solo, self-hosted, low-maintenance operation.** No unnecessary services, no accounts beyond what's needed, no auth complexity beyond what Google/Telegram require.

---

## 1. Foundations & environment setup

**Goal:** Repo, hosting environment, and account/API access all working before any feature code is written.

- [x] Create GitHub repo, basic structure (`/bot`, `/db`, `/scripts`, `.env.example`, `.gitignore` excluding `.env` and the SQLite file)
- [x] Set up Python environment on the home server (venv, dependency management)
- [x] Create Telegram bot via BotFather, get bot token, store in `.env`
- [x] Create Google Cloud project; enable Calendar API and Gmail API (readonly scope)
- [x] **Publish the OAuth consent screen immediately** (even unverified/single-user) — apps left in "testing" mode expire refresh tokens every 7 days. Do this now, not after hitting the bug.
- [x] Set up separate OAuth flow/credentials for the university Gmail account; test whether the university Workspace admin policy allows third-party API access at all. If blocked, fall back to email forwarding rules or IMAP (documented in Section 6).
- [x] Decide and document the process manager (systemd service recommended) so the bot auto-restarts on crash and starts on server boot
- [x] Confirm the bot responds to a basic `/start` message end-to-end (Telegram → server → reply)

**Definition of done:** A bot that's alive, restart-resilient, and authenticated against both Google accounts (or has a documented fallback if the university one is blocked).

---

## 2. Data model & database

**Goal:** Finalized SQLite schema, built once, extended carefully.

- [x] `tasks` — id, title, type (assignment/test/exam/homework), course, due_date, weight_pct, priority (1/2/3), status (not_started/in_progress/done/stale/archived), created_at, notes/week-topic link
- [x] `reminders` — id, text, fire_at, sent (bool), created_at
- [x] `gym` — id, day_of_week, split_name
- [x] `notes` — id, text, created_at, tags
- [x] `known_senders` — email/domain, course label (for email filtering)
- [x] `goals` — id, text, tier (daily/weekly/monthly), created_at, expires_at, status (active/done/dropped)
- [x] `courses` — id, name/code, semester_start_week_offset, weekly_topics (JSON or linked table: week_number → topic)
- [x] `config` — key/value store: semester start date, brief send time, evening check-in time, backlog threshold days, etc. (avoid hardcoding these in code)
- [x] Write a migration script or at least a documented schema versioning approach — you will change this schema; don't paint yourself into a corner
- [x] Seed script for the onboarding session (Section 3) to bulk-load initial data

**Definition of done:** Schema created, a few rows insertable/queryable manually to confirm it holds the shapes you expect.

---

## 3. Onboarding / setup session

**Goal:** One-time bulk load of Kaan's existing structured info, before any daily-use feature needs it.

- [x] Build a simple ingestion path (script or Telegram document upload, whichever's faster to build first) for:
  - [x] Gym split table → `gym`
  - [x] Syllabus documents (one per course) → run through the extraction pipeline from Section 5, populate `courses` + initial `tasks`
  - [x] York academic calendar (semester start/end, reading week, exam period dates) → `config`
- [ ] Manually verify extracted data against the real syllabi before trusting it — this is the shakedown period, do it here before relying on any of it

**Definition of done:** Database populated with real, verified starting data for the current semester.

---

## 4. Core message pipeline (intent classification & routing)

**Goal:** The backbone every text-based feature routes through.

- [x] Single Claude API call that classifies an incoming Telegram message into: `add_task | add_reminder | log_gym | update_task | set_goal | query | note | chat`
- [x] Router that dispatches to the right handler based on classification
- [x] Each handler follows the receipt pattern: parse → write → short informational reply
- [x] Ambiguity handling: if required fields are missing or a course match is unclear, ask one targeted clarifying question (not a general "can you clarify?") — then resolve and confirm in the same follow-up
- [x] Unparseable-message fallback: plain "not sure what to do with that — task, reminder, or just chatting?" response
- [x] Restrict the bot to respond only to Kaan's Telegram user ID (security — do this before anything else is live)

**Definition of done:** You can text the bot "test April 13, psyc 3040, ch 3-5" and get a correct, single-reply receipt with the task saved.

---

## 5. Syllabus upload & extraction

**Goal:** Document upload → structured task/calendar data.

- [x] Accept file uploads via Telegram (PDF/doc)
- [x] Claude document-input call: extract every gradable item (name, due date, weight %, associated week/topic), return structured JSON only
- [x] Flag any date phrased as tentative ("subject to change") distinctly in the output
- [x] Bulk-insert into `tasks` (and `courses.weekly_topics`), reply with a receipt listing what was added — not a yes/no gate
- [ ] Optionally create matching Google Calendar events for due dates

**Definition of done:** Upload a real syllabus, get back an accurate list of extracted deadlines/weights, confirm they match the actual document.

---

## 6. Calendar & email integration

**Goal:** Live read access to both Google accounts, filtered appropriately.

- [x] Google Calendar read integration (personal account) — pull today's/this week's events
- [x] Logic to distinguish recurring "lecture" events from one-off events (for the "weekly summary excluding lectures" brief section)
- [x] Gmail read integration (university account, or documented fallback: forwarding rule to personal Gmail, or IMAP) — filtered to `known_senders` and non-promotional categories
- [x] Extraction call: pull deadline changes, cancellations, announcements from the filtered email subset; ignore routine emails
- [x] Surfaced as a flagged item in the brief, not auto-written to `tasks` — Kaan confirms via a normal reply before it's written

**Definition of done:** Brief can correctly report "today's events" and flag at least one real deadline-change email from an actual professor.

---

## 7. Morning brief generator

**Goal:** The core daily deliverable, matching Kaan's specified format and tone.

- [x] Context-assembly step: pull today's calendar, gym split, weather (min/max/wind/feels-like), tasks by priority and due date, carried-over reminders, tomorrow's prep info, flagged emails, active goals, current week number (computed from `config` semester start date)
- [x] System prompt encoding: friend-like supportive tone, "emotional logic" communication style (facts and trade-offs, not persuasion), section order per Kaan's spec, empty-section skipping, Claude-generated original grounding line/quote in the "unreasonable man" style
- [x] Weather API integration
- [x] Week-number calculation logic
- [x] Section order (adjustable later, start with):
  1. Grounding line/quote
  2. Date + week number
  3. Goals (daily/weekly/monthly)
  4. Weather
  5. Priority reminders/tasks/emails
  6. Weekly summary (non-lecture events, assignments, weekly course topics)
  7. Today's gym split
  8. Carried-over reminders
  9. What's due today
  10. Tomorrow's prep
  11. Professor email flags
- [x] Scheduled job to send at a configured time daily
- [x] `/recap` command — regenerates a fresh version on demand from current data (not cached)

**Definition of done:** A real morning brief generated from real data, matching the spec, sent on schedule, and independently regenerable via `/recap`.

---

## 8. Reminders & error handling

**Goal:** Reliable fixed/relative/vague reminders, and a debuggable failure system underneath everything.

- [x] Fixed-time reminder parsing ("remind me at 4pm to...")
- [x] Relative reminder parsing ("remind me in 20 min")
- [x] Vague/event-based reminder parsing ("remind me after my next class") — needs calendar context injected into the parse call; ask if unresolvable
- [x] Minute-interval poller checking `fire_at` against current time, sending due reminders
- [x] Error code scheme implemented across all subsystems:
  - `E1xx` parsing/classification (E101 intent unclear, E102 ambiguous course, E103 missing required field)
  - `E2xx` external API failures (E201 Claude, E202 Calendar, E203 Gmail, E204 Telegram send)
  - `E3xx` auth (E301 token expired, E302 refresh failed)
  - `E4xx` database (E401 write failed, E402 read failed)
  - `E5xx` scheduler/jobs (E501 brief generation failed, E502 reminder fire failed)
- [x] User-facing errors: short plain-language message + code in parentheses
- [x] Log file: full detail (timestamp, code, stack trace, triggering input)
- [x] `E301` gets a distinct, hard-to-miss alert — plus a daily self-check that only messages Kaan if a token is invalid, not routinely

**Definition of done:** Every write path in the system fails loudly and specifically instead of silently; a forced token expiry produces an unmistakable alert.

---

## 9. Priority & backlog logic

**Goal:** "Stay in the now" — automatic triage so old low-stakes items stop cluttering daily briefs.

- [x] `priority` field set explicitly by Kaan or defaulted by Claude at creation (shown in the receipt either way)
- [x] Auto-demote rule: items overdue past a configurable threshold (default ~7 days), or superseded by a newer week's items in the same course, move to `status = stale` and drop out of daily brief sections
- [x] Priority-1 items are exempt from auto-demotion — stay visible even overdue
- [x] Weekly (not daily) low-visibility backlog surface: "you have N backlogged items, review?"
- [x] Confirm-before-archive flow for genuinely stale, low-stakes, ungraded items (e.g., old readings) — never auto-delete

**Definition of done:** A test task set to a due date 3+ weeks in the past correctly disappears from the daily brief but still exists in a `/backlog`-style query.

---

## 10. Evening check-in

**Goal:** One-shot daily reflection that updates task status and informs tomorrow's plan — the more complex, later-phase feature.

- [x] Scheduled prompt at a configured evening time, listing what the system *believes* happened today (based on task status) as a light check, not a demand
- [x] Single free-text reply from Kaan parsed in one rich Claude call — extracts: what got done, what didn't (and why, if given), what he wants to do tomorrow
- [x] Updates `tasks.status` accordingly; adjusts next-day plan/backlog
- [x] Optional follow-up Q&A (e.g., "do I have time for a game session before X") — user-initiated, not required; answered with honest trade-off math (time remaining, what's stacked, real cost), not guilt or manufactured pressure
- [x] `/quiet` for evening check-ins specifically (separate toggle from morning `/quiet`)

**Definition of done:** A real evening reply correctly updates task statuses and the next morning's brief reflects the change, with zero required back-and-forth.

---

## 11. Goals feature

**Goal:** Daily/weekly/monthly personal goals, separate from school obligations.

- [ ] Daily goal settable via direct text anytime, or asked by the evening check-in if unset for tomorrow
- [x] Weekly/monthly goals settable via direct text
- [x] Goals section in the morning brief, placed near the top (grouped with the grounding line, since it's about Kaan rather than obligations)
- [x] Stalled-goal detection (lightweight heuristic — e.g., no related update in N days) triggering a gentle, occasional nudge for weekly/monthly goals
- [x] Missed daily goal: exactly one soft mention the next brief, never repeated

**Definition of done:** Set a weekly goal, let a few days pass with no update, confirm the nudge fires once and isn't naggy.

---

## 12. Nice-to-haves (build after core system is stable and trusted)

Not part of the main build order — pick up only once Sections 1–11 are working reliably in daily use.

- [ ] Spotify focus-playlist link in the brief (link only, no playback control)
- [ ] Meal-plan/pantry-ingredient suggestions
- [ ] Web UI (only if a specific, concrete pain point emerges from actual use — e.g., bulk-editing misparsed tasks is genuinely annoying via chat)

---

## Explicitly out of scope

Don't build these unless Kaan revisits the decision:
- Financial/expense tracking
- Sleep/study-hour tracking
- Self-adjusting/adaptive parsing logic (always-confirm-when-ambiguous is the permanent approach, not a v1 placeholder)
- Any Spotify playback control (link-only, see Section 12)

---

## Cross-cutting reminders for every session

- Test against real data (real calendar, real syllabus, real gym schedule) before considering a session done — synthetic test data hides the bugs that matter.
- Every new external write path gets an error code from the Section 8 scheme, even if Section 8 hasn't been formally built yet — retrofit later is worse than doing it inline.
- Course tagging is required, not optional, on every task-creation path (text parse, email, syllabus) — this is what prevents cross-course collisions.
- Keep the tone consistent everywhere the bot writes to Kaan — one shared system prompt fragment for voice/style, reused across morning brief, evening check-in, and receipts, not reinvented per feature.
