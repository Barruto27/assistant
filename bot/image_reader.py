"""Dated items from a photo or screenshot.

A screenshot of a course schedule, an eClass page, or a whiteboard is often the
fastest way to get dates into the system — faster than typing them, and it is
what Kaan reached for unprompted.

Unlike a syllabus import this **adds** tasks and never replaces. An image
usually shows one slice of a course (the check-in dates, say), so treating it
as authoritative for the whole course would delete everything it doesn't
mention.
"""

from __future__ import annotations

import base64
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from bot import repository as repo
from bot.errors import AssistantError, E, logger
from bot.syllabus import VALID_TYPES, _coerce_type, normalize_course_code

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MEDIA_TYPES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
}

EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_dated_items",
    "description": (
        "Record every dated item visible in the image: due dates, class dates, "
        "deadlines, appointments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "course": {
                "type": "string",
                "description": (
                    "Course code these belong to. Prefer the one named in the "
                    "caption; otherwise read it from the image."
                ),
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "due_date": {"type": "string", "description": "YYYY-MM-DD."},
                        "type": {"type": "string", "enum": sorted(VALID_TYPES)},
                        "weight_pct": {"type": "number"},
                        "notes": {
                            "type": "string",
                            "description": "Short. Topic or detail.",
                        },
                        "tentative": {"type": "boolean"},
                    },
                    "required": ["title", "due_date"],
                },
            },
        },
        "required": ["items"],
    },
}

SYSTEM = """\
You are reading a screenshot or photo a student sent, to pull out dated work.

Record every item that has a date: assignments, check-ins, quizzes, classes
with deliverables, appointments. Read dates exactly as shown and convert to
YYYY-MM-DD — never guess a year that isn't determinable, and never invent an
item that isn't visible.

If the caption names a course, use it for every item. Keep titles short and
recognisable: the row label, not a sentence.

Today's date is {today}.
"""


@dataclass(frozen=True)
class DatedItem:
    title: str
    due_date: str
    type: str = "other"
    weight_pct: float | None = None
    notes: str | None = None
    tentative: bool = False


def media_type(data: bytes) -> str:
    """Sniff the format from magic bytes; Telegram's mime hint can be absent."""
    for signature, mime in MEDIA_TYPES.items():
        if data.startswith(signature):
            return mime
    raise AssistantError(
        E.UNPARSEABLE_DOCUMENT, "That doesn't look like an image I can read."
    )


def extract(
    image: bytes,
    caption: str,
    client: Any,
    model: str,
    *,
    today: date | None = None,
) -> tuple[str | None, list[DatedItem]]:
    """Returns (course, items). Both may be empty."""
    if not image:
        raise AssistantError(E.UNPARSEABLE_DOCUMENT, "That image was empty.")
    if len(image) > MAX_IMAGE_BYTES:
        raise AssistantError(
            E.UNPARSEABLE_DOCUMENT,
            f"That image is {len(image) // (1024 * 1024)} MB, past the "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.",
        )

    mime = media_type(image)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=3072,
            system=SYSTEM.format(today=(today or date.today()).isoformat()),
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_dated_items"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": caption or "What dates are in this?"},
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
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "record_dated_items"
        ):
            payload = dict(block.input or {})
            raw_course = str(payload.get("course", "")).strip()
            course = normalize_course_code(raw_course) if raw_course else None
            items = [
                DatedItem(
                    title=str(raw["title"]).strip(),
                    due_date=str(raw["due_date"]).strip(),
                    type=_coerce_type(raw.get("type")),
                    weight_pct=raw.get("weight_pct"),
                    notes=(str(raw["notes"]).strip() if raw.get("notes") else None),
                    tentative=bool(raw.get("tentative", False)),
                )
                for raw in payload.get("items", [])
                if str(raw.get("title", "")).strip()
                and _DATE_RE.match(str(raw.get("due_date", "")).strip())
            ]
            return course, items

    return None, []


def ingest(conn: sqlite3.Connection, course: str | None, items: list[DatedItem]) -> int:
    """Add the items. Existing rows are never touched.

    An image shows one slice of a course, so replacing on its basis would
    delete everything it happens not to show.
    """
    added = 0
    for item in items:
        repo.add_task(
            conn,
            title=item.title,
            type=item.type,
            course=course,
            due_date=item.due_date,
            tentative=item.tentative,
            weight_pct=item.weight_pct,
            priority=2 if (item.weight_pct or 0) < 20 else 1,
            notes=item.notes,
            source="text",
        )
        added += 1
    logger.info(
        "Imported %d dated item(s) from an image for %s", added, course or "no course"
    )
    return added


def receipt(course: str | None, items: list[DatedItem]) -> str:
    if not items:
        return (
            "I couldn't find any dates in that image. If they're there, tell me "
            "what I'm looking at and I'll try again."
        )
    header = f"Added {len(items)} item(s)" + (
        f" to {course}" if course else " (no course)"
    )
    lines = [header, ""]
    for item in sorted(items, key=lambda i: i.due_date):
        bits = [item.due_date, item.title]
        if item.weight_pct is not None:
            bits.append(f"{item.weight_pct:g}%")
        if item.tentative:
            bits.append("tentative")
        lines.append("  " + " · ".join(bits))
    if not course:
        lines.append("")
        lines.append("No course on these — tell me which one and I'll tag them.")
    return "\n".join(lines)
