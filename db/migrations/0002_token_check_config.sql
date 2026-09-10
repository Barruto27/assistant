-- Migration 0002 — config for the daily credential self-check (plan Section 8).
--
-- A token that dies quietly is the worst failure mode here: the calendar simply
-- goes blank in the brief and nothing says why. This adds the schedule for a
-- daily check that messages Kaan only when something is actually wrong.

INSERT INTO config (key, value) VALUES
    ('token_check_time', '08:15')     -- local HH:MM, shortly after the morning brief
    ON CONFLICT(key) DO NOTHING;

INSERT INTO config (key, value) VALUES
    ('token_alerted_on', '')          -- YYYY-MM-DD of the last alert, so it fires once a day at most
    ON CONFLICT(key) DO NOTHING;
