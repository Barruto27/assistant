-- Migration 0003 — attendance-graded work (from real use).
--
-- The first real brief listed iClicker participation as "due today" alongside a
-- written reflection. But an iClicker mark is not something you submit — it is
-- a mark for being physically in the lecture. Presented as a deadline it is
-- both useless (there is no artifact to produce) and quietly stressful.
--
-- Flagging it lets the brief tie it to the lecture and say the useful thing:
-- showing up is worth marks, and missing it cannot be made up.

ALTER TABLE tasks ADD COLUMN attendance INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_tasks_attendance ON tasks (attendance) WHERE attendance = 1;
