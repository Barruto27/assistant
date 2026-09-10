-- Migration 0006 — goal nudge bookkeeping (plan Section 11).
--
-- The plan is specific about restraint: a stalled weekly or monthly goal gets
-- a gentle, occasional nudge, and a missed daily goal gets exactly one soft
-- mention and is never raised again. Both need a record of what has already
-- been said, or the brief turns into the nagging this is meant to avoid.
--
-- missed_mentioned already exists from migration 0001; this adds the other half.

ALTER TABLE goals ADD COLUMN nudged_at TEXT;
