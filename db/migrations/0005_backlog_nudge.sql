-- Migration 0005 — bookkeeping for the weekly backlog surface (plan Section 9).
--
-- Weekly, not daily. A daily count of things Kaan has already decided not to do
-- is exactly the nagging the backlog rule exists to prevent.

INSERT INTO config (key, value) VALUES ('backlog_nudged_on', '')
    ON CONFLICT(key) DO NOTHING;
