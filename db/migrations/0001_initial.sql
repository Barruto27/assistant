-- Migration 0001 — initial schema
-- Personal Telegram Assistant. See telegram-assistant-plan.md Section 2.
--
-- Conventions:
--   * All timestamps are ISO-8601 text in the configured local timezone
--     (config key 'timezone'), stored as 'YYYY-MM-DD HH:MM:SS' or a bare
--     'YYYY-MM-DD' where only a date matters (task due dates).
--   * Booleans are INTEGER 0/1.
--   * Enumerations are enforced with CHECK constraints so a bad write fails
--     loudly (principle 5) rather than silently storing garbage.
--   * No transaction control or PRAGMAs here — the migration runner owns both.

-- ---------------------------------------------------------------------------
-- courses — one row per enrolled course this semester
-- ---------------------------------------------------------------------------
CREATE TABLE courses (
    id                        INTEGER PRIMARY KEY,
    code                      TEXT NOT NULL UNIQUE,          -- e.g. 'PSYC 3040'
    name                      TEXT,                          -- full title
    semester_start_week_offset INTEGER NOT NULL DEFAULT 0,   -- weeks this course starts after the global semester start
    created_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

-- course_weeks — week_number -> topic, linked to a course
CREATE TABLE course_weeks (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    topic       TEXT,
    UNIQUE (course_id, week_number)
);

-- ---------------------------------------------------------------------------
-- tasks — gradable items and to-dos
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'other'
                    CHECK (type IN ('assignment', 'test', 'exam', 'homework', 'reading', 'other')),
    course        TEXT,                                      -- course code label; required on every creation path (see plan cross-cutting notes)
    due_date      TEXT,                                      -- 'YYYY-MM-DD' or full timestamp
    tentative     INTEGER NOT NULL DEFAULT 0,                -- 1 if the source phrased the date as "subject to change"
    weight_pct    REAL,                                      -- grade weight, 0-100
    priority      INTEGER NOT NULL DEFAULT 2
                    CHECK (priority IN (1, 2, 3)),           -- 1 = highest, exempt from auto-demotion
    status        TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started', 'in_progress', 'done', 'stale', 'archived')),
    week_number   INTEGER,                                   -- links to the course's weekly topic
    notes         TEXT,
    source        TEXT NOT NULL DEFAULT 'text'
                    CHECK (source IN ('text', 'syllabus', 'email', 'seed')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX idx_tasks_due     ON tasks (due_date);
CREATE INDEX idx_tasks_status  ON tasks (status);
CREATE INDEX idx_tasks_course  ON tasks (course);

-- Keep updated_at honest without app-side bookkeeping. The WHEN guard stops the
-- trigger re-firing on its own write regardless of the recursive_triggers pragma.
CREATE TRIGGER trg_tasks_updated_at
AFTER UPDATE ON tasks
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE tasks
       SET updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')
     WHERE id = OLD.id;
END;

-- ---------------------------------------------------------------------------
-- reminders — fixed / relative / event-based, resolved to an absolute time
-- ---------------------------------------------------------------------------
CREATE TABLE reminders (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    fire_at    TEXT NOT NULL,                                -- absolute timestamp the poller compares against
    sent       INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'text'
                 CHECK (source IN ('text', 'seed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX idx_reminders_pending ON reminders (fire_at) WHERE sent = 0;

-- ---------------------------------------------------------------------------
-- gym — weekly split, one row per weekday
-- ---------------------------------------------------------------------------
CREATE TABLE gym (
    id          INTEGER PRIMARY KEY,
    day_of_week INTEGER NOT NULL UNIQUE
                  CHECK (day_of_week BETWEEN 0 AND 6),       -- 0 = Monday ... 6 = Sunday
    split_name  TEXT NOT NULL                                -- e.g. 'Push', 'Pull', 'Legs', 'Rest'
);

-- ---------------------------------------------------------------------------
-- notes — free-form captures
-- ---------------------------------------------------------------------------
CREATE TABLE notes (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    tags       TEXT,                                         -- comma-separated
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

-- ---------------------------------------------------------------------------
-- known_senders — email/domain allowlist for the Gmail filter (Session 6)
-- ---------------------------------------------------------------------------
CREATE TABLE known_senders (
    id           INTEGER PRIMARY KEY,
    pattern      TEXT NOT NULL UNIQUE,                       -- full address or bare domain e.g. 'prof@yorku.ca' or 'yorku.ca'
    course_label TEXT,                                       -- course this sender maps to, if any
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

-- ---------------------------------------------------------------------------
-- goals — personal goals, separate from school obligations (Session 11)
-- ---------------------------------------------------------------------------
CREATE TABLE goals (
    id               INTEGER PRIMARY KEY,
    text             TEXT NOT NULL,
    tier             TEXT NOT NULL
                       CHECK (tier IN ('daily', 'weekly', 'monthly')),
    status           TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'done', 'dropped')),
    expires_at       TEXT,                                   -- when this goal stops being "current"
    last_progress_at TEXT,                                   -- updated when Kaan reports movement; drives stalled-goal nudges
    missed_mentioned INTEGER NOT NULL DEFAULT 0,             -- 1 after the single soft mention of a missed daily goal
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX idx_goals_active ON goals (tier) WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- config — key/value store for everything that must not be hardcoded
-- ---------------------------------------------------------------------------
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO config (key, value) VALUES
    ('semester_start_date',   ''),          -- 'YYYY-MM-DD', Monday of week 1 — set during onboarding (Session 3)
    ('semester_end_date',     ''),
    ('reading_week_start',    ''),
    ('reading_week_end',      ''),
    ('exam_period_start',     ''),
    ('exam_period_end',       ''),
    ('timezone',              'America/Toronto'),
    ('brief_send_time',       '07:30'),     -- local HH:MM for the morning brief
    ('evening_checkin_time',  '21:00'),     -- local HH:MM for the evening check-in
    ('backlog_threshold_days','7'),         -- overdue-by-N-days before a non-P1 task goes stale
    ('quiet_morning',         '0'),         -- 1 suppresses the morning brief
    ('quiet_evening',         '0'),         -- 1 suppresses the evening check-in
    ('reminder_poll_seconds', '60');        -- how often the reminder poller runs

-- Note: the schema_migrations bookkeeping table is created and maintained by
-- the runner in db/database.py, not by any migration file.
