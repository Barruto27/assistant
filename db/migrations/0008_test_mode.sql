-- Migration 0008 — test mode (from real use).
--
-- Testing the bot against the live database left a fabricated reminder due to
-- fire that evening and two real tasks marked done, one of them an attendance
-- mark for a lecture that had not happened yet. Nothing distinguished those
-- rows from Kaan's own, and the next brief would have been confidently wrong.

INSERT INTO config (key, value) VALUES ('test_mode', '0')
    ON CONFLICT(key) DO NOTHING;
INSERT INTO config (key, value) VALUES ('test_mode_started_at', '')
    ON CONFLICT(key) DO NOTHING;
