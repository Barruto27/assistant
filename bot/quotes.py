"""The quotation that opens the morning brief (plan Section 7).

Kaan's verdict on the original opener was that it was not motivating, and he
was right: the instruction asked for "a short, unsentimental line about doing
hard things on purpose", named a famous quotation as the model, then banned
quoting it. What that produced was aphorism-shaped filler - "Some weeks you
just show up and let the syllabus tell you what kind of semester it's going to
be." Two rewrites made it concrete but no better, because on a quiet day a line
derived from his tasks has nothing to say and restates the date.

So it is a real quotation now, attributed, chosen fresh each morning.

Picked by its own small tool call rather than left to the brief, for two
reasons. The brief writes free prose, and pulling a quotation back out of prose
to record it means parsing what the model happened to format - fragile in
exactly the way that matters, because a parse failure means a repeat. And the
list of what has already been used has to go in as input, which is easier to do
honestly in a call that does nothing else.

Never asserts a quotation is real. It is a model's recollection of one, which
is worth saying out loud: attributions drift, and a misattributed line in a
morning text is a small thing but still a false one.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from bot.errors import AssistantError, logger
from db.database import transaction

#: How many recent quotations to show the picker so it avoids them. Long
#: enough to cover a term of mornings without making the prompt silly.
RECENT_LIMIT = 120

QUOTE_TOOL: dict[str, Any] = {
    "name": "choose_quote",
    "description": "Choose one real, attributed quotation to open the morning brief.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The quotation itself, without surrounding quotation marks."
                ),
            },
            "author": {
                "type": "string",
                "description": "Who said or wrote it. A real, named person.",
            },
            "why": {
                "type": "string",
                "description": (
                    "One short clause on what it has to do with his day. Not "
                    "sent to him; it is here to stop a quotation being chosen "
                    "at random."
                ),
            },
        },
        "required": ["text", "author", "why"],
    },
}

SYSTEM = """\
Choose one real quotation to open a university student's morning brief.

It has to be something a named person actually said or wrote. No proverbs, no
"anonymous", no lines you are reconstructing from a vague memory of the sense
of them. If you are not confident of both the wording and the attribution,
choose a different one you are sure of.

What makes a good one here:
- It bears on what today actually asks of him. {shape}
- It earns its place at 7:30am: something with a bit of edge, or a hard truth
  told plainly. He asked for motivating, and meant it.
- Range widely. Stoics, novelists, scientists, athletes, coaches, mathematicians,
  musicians, soldiers, anyone. Over a term this should not feel like one shelf.
- Short enough to read in a second. One or two sentences at most.

What ruins it:
- The single most famous line by that person. Not "the unexamined life", not
  "I have a dream". Reach past the obvious one.
- Anything that would work printed over a sunrise, and anything about
  believing in yourself.
- Hustle-culture grind quotes. He is a student with a reading week, not a
  founder.
- Anything already on the list below.

Already used, do not repeat any of these or anything close to them:
{used}
"""


@dataclass(frozen=True)
class Quote:
    text: str
    author: str

    def line(self) -> str:
        return f'"{self.text}" — {self.author}'


def fingerprint(text: str) -> str:
    """Lowercase letters and digits only.

    The same quotation comes back with different punctuation, curly quotes, or
    a slightly different translation, and none of that makes it new.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def recent(conn: sqlite3.Connection, limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT text, author FROM quotes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def already_used(conn: sqlite3.Connection, text: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM quotes WHERE fingerprint = ?", (fingerprint(text),)
        ).fetchone()
        is not None
    )


def record(conn: sqlite3.Connection, quote: Quote) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO quotes (fingerprint, text, author) VALUES (?, ?, ?)",
            (fingerprint(quote.text), quote.text, quote.author),
        )


def describe_day(context: Any) -> str:
    """One clause telling the picker what kind of day it is.

    Without this the choice is untethered and the quotation drifts toward
    generic encouragement, which is the failure the old opener had.
    """
    if getattr(context, "overdue", None):
        return "Something is overdue and still matters."
    if getattr(context, "due_today", None):
        return "Work is due today."
    if getattr(context, "attendance_today", None):
        return (
            "He has a lecture today that carries a mark simply for being there, "
            "and the useful thing is going when he does not feel like it."
        )
    if getattr(context, "upcoming", None):
        return "Nothing is due today, but deadlines land later this week."
    return "A quiet day with nothing due."


def pick(conn: sqlite3.Connection, caller: Any, context: Any = None) -> Quote | None:
    """One quotation, new to him, recorded so it is never repeated.

    Returns None rather than raising: a brief without an opening line is a
    small loss, and a brief that failed to send is not.
    """
    used = recent(conn)
    listing = (
        "\n".join(f'- "{row["text"]}" - {row["author"]}' for row in used)
        or "- (nothing yet)"
    )
    system = SYSTEM.format(shape=describe_day(context), used=listing)

    try:
        payload = caller.call_tool(
            system, "Choose today's quotation.", QUOTE_TOOL, max_tokens=400
        )
    except AssistantError as err:
        logger.warning("Couldn't choose a quotation: %s", err)
        return None

    text = str(payload.get("text", "")).strip().strip('"').strip("“”").strip()
    author = str(payload.get("author", "")).strip()
    if not text or not author:
        logger.warning("Quotation call returned nothing usable: %r", payload)
        return None

    if already_used(conn, text):
        # It was told not to. Rather than send a repeat, send none - the brief
        # reads perfectly well without one.
        logger.info("Quotation %r was already used; skipping today's", text[:50])
        return None

    quote = Quote(text=text, author=author)
    record(conn, quote)
    logger.info("Quotation chosen: %s", quote.line()[:90])
    return quote
