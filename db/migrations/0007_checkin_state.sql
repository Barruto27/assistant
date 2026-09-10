-- Migration 0007 — evening check-in state (plan Section 10).
--
-- A check-in is a question, so the system has to remember it asked. Without
-- that, Kaan's answer is just another message and gets classified as a task or
-- a note, and the whole point — one reply, no back-and-forth — is lost.
--
-- The task ids offered are stored alongside, so applying his answer is a lookup
-- rather than a fuzzy match on titles. Matching the wrong row here would
-- quietly corrupt his record of the term.

INSERT INTO config (key, value) VALUES ('checkin_sent_at', '')
    ON CONFLICT(key) DO NOTHING;
INSERT INTO config (key, value) VALUES ('checkin_task_ids', '')
    ON CONFLICT(key) DO NOTHING;
