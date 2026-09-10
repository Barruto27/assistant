-- Migration 0004 — record what the extraction was unsure about.
--
-- A syllabus import is a reading of a PDF, not a transcription of it. Some
-- things are genuinely ambiguous: a date given only as "week 6", a weight that
-- doesn't total 100, a component described in prose rather than a table.
--
-- Storing the uncertainty means the bot can say which parts of which syllabus
-- are worth checking against the original, instead of presenting everything it
-- extracted with equal confidence.

ALTER TABLE courses ADD COLUMN verify_notes TEXT;
