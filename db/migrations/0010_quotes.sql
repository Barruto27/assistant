-- Quotations already used, so the morning brief never repeats one.
--
-- Kaan asked for a real attributed quotation to open the brief - closest to
-- the Shaw line the original instruction gestured at and then forbade. A
-- quotation only works if it is new to him; the same three lines cycling round
-- would be worse than the vague aphorisms it replaces, because at least those
-- were different every day.
--
-- The lookup key is the normalised text rather than the text itself: the same
-- quotation comes back with different punctuation, capitalisation and
-- occasionally a different translation, and none of that should count as new.

CREATE TABLE quotes (
    id         INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,                      -- lowercased, letters and digits only
    text       TEXT NOT NULL,
    author     TEXT NOT NULL,
    used_on    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d', 'now', 'localtime'))
);

CREATE INDEX idx_quotes_used_on ON quotes (used_on DESC);
