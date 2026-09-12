-- Remember what was flagged in email, so it outlives the message it appeared in.
--
-- Until now a flag existed only inside one morning brief. If Kaan was asleep,
-- busy, or simply did not act on it, it was gone: the evening check-in had no
-- idea it had ever been raised, and the next morning's scan either re-flagged
-- the same email as though it were new or, once outside the three-day window,
-- dropped it silently. A CMDS syllabus quiz arriving on a Friday could pass
-- through the whole system without ever being asked about.
--
-- Keyed on the source email's Message-ID rather than on the summary: the
-- summary is written fresh by the model each run and reads differently every
-- time, so it cannot recognise anything. Flags whose source could not be
-- identified are stored with an empty id and simply never deduplicate, which
-- is the harmless direction to fail in.
--
-- Nothing here is a task. Section 6 is explicit that Kaan confirms before
-- anything is written to tasks, and this table does not change that; it is a
-- record of what the mailbox said, not of what he has to do.

CREATE TABLE flagged_emails (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT NOT NULL,                             -- RFC 5322 Message-ID, or '' when unidentified
    kind        TEXT NOT NULL DEFAULT 'announcement'
                  CHECK (kind IN ('deadline_change', 'cancellation', 'new_work', 'announcement')),
    summary     TEXT NOT NULL,
    course      TEXT,
    new_date    TEXT,                                      -- 'YYYY-MM-DD' when the mail sets or moves one
    sender      TEXT,
    status      TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'raised', 'closed')),
    -- How many times it has been put in front of him. The brief raises it once
    -- and the check-in once; after that it stops, so an email he has decided to
    -- ignore cannot nag him forever. Same principle as the backlog rule.
    times_raised INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    last_raised TEXT
);

-- One row per source email. Unidentified flags (message_id = '') are exempt,
-- because they are not the same email as each other just by both being unknown.
CREATE UNIQUE INDEX idx_flagged_emails_message
    ON flagged_emails (message_id)
    WHERE message_id != '';

CREATE INDEX idx_flagged_emails_status ON flagged_emails (status, times_raised);
