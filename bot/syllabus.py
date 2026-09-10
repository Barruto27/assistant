"""Syllabus extraction (plan Section 5).

A PDF goes to Claude as a document, and comes back as structured rows: the
course, its weekly topics, and every gradable item with a date and a weight.

Two things this is deliberately careful about:

*   **Tentative dates.** Syllabi are full of "subject to change" and "week 10
    (approx)". Those are flagged rather than silently stored as fact, so the
    brief can say a date is provisional instead of asserting it.
*   **Course tagging.** Every extracted task carries the course. An untagged
    task collides with other courses later, which is the failure the plan's
    cross-cutting notes call out specifically.

Nothing here writes to the database — ``ingest`` does that, so extraction stays
testable without one.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from bot import repository as repo
from bot.errors import AssistantError, E, logger
from db.database import transaction

MAX_PDF_BYTES = 25 * 1024 * 1024

EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_syllabus",
    "description": "Record everything gradable and scheduled from a course syllabus.",
    "input_schema": {
        "type": "object",
        "properties": {
            "course_code": {
                "type": "string",
                "description": "Course code as written, e.g. 'PSYC 3040'. Normalise spacing.",
            },
            "course_name": {"type": "string", "description": "Full course title."},
            "items": {
                "type": "array",
                "description": "Every gradable item: assignments, tests, exams, quizzes, participation.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "assignment",
                                "test",
                                "exam",
                                "homework",
                                "reading",
                                "other",
                            ],
                        },
                        "due_date": {
                            "type": "string",
                            "description": (
                                "YYYY-MM-DD. Use the syllabus's own year; if only a "
                                "month and day are given, infer the year from the "
                                "term dates. Omit if genuinely no date is given."
                            ),
                        },
                        "tentative": {
                            "type": "boolean",
                            "description": (
                                "True if the syllabus hedges the date at all: "
                                "'subject to change', 'approximately', 'TBD', "
                                "'week 10 (approx)'."
                            ),
                        },
                        "weight_pct": {
                            "type": "number",
                            "description": "Percentage of the final grade.",
                        },
                        "week_number": {
                            "type": "integer",
                            "description": "Course week it falls in, if the syllabus says.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Chapters, topics, or format worth keeping. Keep it short.",
                        },
                    },
                    "required": ["title"],
                },
            },
            "weekly_topics": {
                "type": "array",
                "description": "The week-by-week schedule of topics, if the syllabus has one.",
                "items": {
                    "type": "object",
                    "properties": {
                        "week_number": {"type": "integer"},
                        "topic": {"type": "string"},
                    },
                    "required": ["week_number", "topic"],
                },
            },
        },
        "required": ["course_code", "items"],
    },
}

SYSTEM = """\
You are reading a course syllabus and recording what it commits the student to.

Call record_syllabus exactly once. Work only from the document — never invent an
item, a date, or a weight that isn't there.

Be exhaustive about gradable items: every assignment, test, exam, quiz, lab,
presentation, and participation component, including ones mentioned only in a
grading-breakdown table. If a weight is given without a date, or a date without
a weight, record what you have and leave the rest out.

Mark tentative generously. Syllabi hedge constantly, and a date recorded as
firm when the syllabus called it approximate is worse than one flagged as
provisional.

Today's date is {today}. Use it to resolve any year the syllabus leaves implicit.
"""


@dataclass
class SyllabusItem:
    title: str
    type: str = "other"
    due_date: str | None = None
    tentative: bool = False
    weight_pct: float | None = None
    week_number: int | None = None
    notes: str | None = None


@dataclass
class Syllabus:
    course_code: str
    course_name: str | None = None
    items: list[SyllabusItem] = field(default_factory=list)
    weekly_topics: list[tuple[int, str]] = field(default_factory=list)

    @property
    def total_weight(self) -> float:
        return sum(item.weight_pct or 0 for item in self.items)


#: Mirrors the CHECK constraint on tasks.type. A tool schema enum is a strong
#: hint, not a guarantee — Claude returned "lab" for a syllabus full of studio
#: work and the constraint rejected the entire import.
VALID_TYPES = frozenset(
    {"assignment", "test", "exam", "homework", "reading", "other"}
)

#: Common out-of-enum answers worth mapping rather than flattening to "other",
#: so priority defaults and receipts stay meaningful.
TYPE_ALIASES = {
    "lab": "assignment",
    "project": "assignment",
    "presentation": "assignment",
    "essay": "assignment",
    "paper": "assignment",
    "quiz": "test",
    "midterm": "test",
    "final": "exam",
    "participation": "other",
    "discussion": "other",
}


def _clean(value: Any) -> Any:
    return None if value in ("", None) else value


def _coerce_type(raw: Any) -> str:
    """Map whatever came back onto the enum the database will accept."""
    value = str(_clean(raw) or "other").strip().lower()
    if value in VALID_TYPES:
        return value
    mapped = TYPE_ALIASES.get(value)
    if mapped:
        logger.info("Mapped syllabus item type %r to %r", value, mapped)
        return mapped
    logger.warning("Unknown syllabus item type %r; recording as 'other'", value)
    return "other"



def parse_extraction(payload: dict[str, Any]) -> Syllabus:
    """Turn the tool input into a Syllabus. Shared by the real and fake clients."""
    code = str(payload.get("course_code", "")).strip()
    if not code:
        raise AssistantError(
            E.UNPARSEABLE_DOCUMENT,
            "Couldn't find a course code in that document.",
        )

    items = [
        SyllabusItem(
            title=str(raw["title"]).strip(),
            type=_coerce_type(raw.get("type")),
            due_date=_clean(raw.get("due_date")),
            tentative=bool(raw.get("tentative", False)),
            weight_pct=_clean(raw.get("weight_pct")),
            week_number=_clean(raw.get("week_number")),
            notes=_clean(raw.get("notes")),
        )
        for raw in payload.get("items", [])
        if str(raw.get("title", "")).strip()
    ]

    topics = [
        (int(entry["week_number"]), str(entry["topic"]).strip())
        for entry in payload.get("weekly_topics", [])
        if _clean(entry.get("topic")) and entry.get("week_number") is not None
    ]

    return Syllabus(
        course_code=code,
        course_name=_clean(payload.get("course_name")),
        items=items,
        weekly_topics=topics,
    )


def extract(pdf_bytes: bytes, client: Any, model: str, *, today: date | None = None) -> Syllabus:
    """Send the PDF to Claude and parse the structured result."""
    if not pdf_bytes:
        raise AssistantError(E.UNPARSEABLE_DOCUMENT, "That file was empty.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise AssistantError(
            E.UNPARSEABLE_DOCUMENT,
            f"That PDF is {len(pdf_bytes) // (1024 * 1024)} MB, which is past the "
            f"{MAX_PDF_BYTES // (1024 * 1024)} MB limit.",
        )

    encoded = base64.standard_b64encode(pdf_bytes).decode("ascii")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM.format(today=(today or date.today()).isoformat()),
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_syllabus"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": encoded,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Record everything gradable in this syllabus.",
                        },
                    ],
                }
            ],
        )
    except AssistantError:
        raise
    except Exception as exc:  # noqa: BLE001 - SDK raises a family of errors
        from bot.claude_client import _explain

        raise AssistantError(E.CLAUDE, _explain(exc), cause=exc) from exc

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_syllabus":
            return parse_extraction(dict(block.input or {}))

    raise AssistantError(
        E.UNPARSEABLE_DOCUMENT,
        "Claude read that document but didn't find a syllabus in it.",
    )


def ingest(conn: sqlite3.Connection, syllabus: Syllabus) -> dict[str, int]:
    """Write the extraction to the database. Returns what changed.

    Re-importing the same syllabus replaces that course's syllabus-sourced tasks
    rather than duplicating them. Anything Kaan added by text is left alone —
    a re-import must not quietly undo his own edits.
    """
    counts = {"tasks": 0, "topics": 0, "replaced": 0}
    try:
        with transaction(conn):
            cursor = conn.execute(
                "INSERT INTO courses (code, name) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = COALESCE(excluded.name, name)",
                (syllabus.course_code, syllabus.course_name),
            )
            row = conn.execute(
                "SELECT id FROM courses WHERE code = ?", (syllabus.course_code,)
            ).fetchone()
            course_id = row["id"] if row else cursor.lastrowid

            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE course = ? AND source = 'syllabus'",
                (syllabus.course_code,),
            ).fetchone()["n"]
            if existing:
                conn.execute(
                    "DELETE FROM tasks WHERE course = ? AND source = 'syllabus'",
                    (syllabus.course_code,),
                )
                counts["replaced"] = existing

            for item in syllabus.items:
                conn.execute(
                    "INSERT INTO tasks (title, type, course, due_date, tentative, "
                    "weight_pct, priority, notes, week_number, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'syllabus')",
                    (
                        item.title,
                        item.type,
                        syllabus.course_code,
                        item.due_date,
                        int(item.tentative),
                        item.weight_pct,
                        default_priority(item),
                        item.notes,
                        item.week_number,
                    ),
                )
                counts["tasks"] += 1

            for week, topic in syllabus.weekly_topics:
                conn.execute(
                    "INSERT INTO course_weeks (course_id, week_number, topic) "
                    "VALUES (?, ?, ?) ON CONFLICT(course_id, week_number) "
                    "DO UPDATE SET topic = excluded.topic",
                    (course_id, week, topic),
                )
                counts["topics"] += 1
    except sqlite3.Error as exc:
        raise AssistantError(
            E.DB_WRITE, "Couldn't save that syllabus.", cause=exc
        ) from exc

    logger.info(
        "Imported %s: %d tasks, %d topics (%d replaced)",
        syllabus.course_code,
        counts["tasks"],
        counts["topics"],
        counts["replaced"],
    )
    return counts


def default_priority(item: SyllabusItem) -> int:
    """Priority by stakes, matching what the message pipeline does.

    Exams and heavily weighted work stay visible even when overdue (the backlog
    rule exempts priority 1); readings and unweighted prep can fall away.
    """
    if item.type == "exam" or (item.weight_pct or 0) >= 20:
        return 1
    if item.type == "reading" or (item.weight_pct or 0) == 0:
        return 3
    return 2


def receipt(syllabus: Syllabus, counts: dict[str, int]) -> str:
    """What Kaan sees after an import: enough detail to spot a bad extraction."""
    header = syllabus.course_code
    if syllabus.course_name:
        header += f" — {syllabus.course_name}"

    lines = [f"{header}", ""]
    dated = sorted(
        (i for i in syllabus.items if i.due_date), key=lambda i: i.due_date or ""
    )
    undated = [i for i in syllabus.items if not i.due_date]

    for item in dated:
        bits = [f"{item.due_date}", item.title]
        if item.weight_pct is not None:
            bits.append(f"{item.weight_pct:g}%")
        if item.tentative:
            bits.append("tentative")
        lines.append("  " + " · ".join(bits))

    for item in undated:
        weight = f" · {item.weight_pct:g}%" if item.weight_pct is not None else ""
        lines.append(f"  no date · {item.title}{weight}")

    total = syllabus.total_weight
    summary = f"{counts['tasks']} items"
    if counts["topics"]:
        summary += f", {counts['topics']} weekly topics"
    summary += f", {total:g}% of the grade accounted for"
    if total and abs(total - 100) > 1:
        # Worth surfacing: it usually means something was missed in the PDF.
        summary += " (not 100 — check I didn't miss anything)"
    if counts["replaced"]:
        summary += f". Replaced {counts['replaced']} earlier items from this syllabus"

    lines.extend(["", summary])
    return "\n".join(lines)


def read_pdf(path: str | Path) -> bytes:
    p = Path(path)
    if not p.exists():
        raise AssistantError(E.UNPARSEABLE_DOCUMENT, f"No file at {p}.")
    return p.read_bytes()
