"""Intent schema for the message pipeline (plan Section 4).

Classification and field extraction happen in a *single* Claude call: each
intent is exposed as a tool, so the tool Claude picks is the classification and
its input is the extraction. One call, not two.

Adding an intent means adding a tool here and a handler in ``bot/router.py`` —
``tests/test_router.py`` asserts the two stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Intent names, also used as the tool names Claude sees.
ADD_TASK = "add_task"
ADD_REMINDER = "add_reminder"
SET_GYM_SPLIT = "set_gym_split"
UPDATE_TASK = "update_task"
SET_GOAL = "set_goal"
SAVE_NOTE = "save_note"
ANSWER_QUERY = "answer_query"
JUST_CHAT = "just_chat"
CHECKIN_REPLY = "checkin_reply"
ASK_CLARIFICATION = "ask_clarification"


@dataclass(frozen=True)
class ParsedIntent:
    """What one Claude call produced: which tool, and its arguments."""

    name: str
    fields: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.fields.get(key, default)
        # Claude occasionally emits empty strings for optional fields.
        return default if value in ("", None) else value


INTENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": ADD_TASK,
        "description": (
            "Record a new piece of coursework or a to-do. Use for anything with a "
            "deadline: assignments, tests, exams, homework, readings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name, e.g. 'Test 2' or 'Essay draft'.",
                },
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
                "course": {
                    "type": "string",
                    "description": (
                        "Course code, matched to the known course list in the system "
                        "prompt. Omit only if the message truly names no course."
                    ),
                },
                "due_date": {
                    "type": "string",
                    "description": (
                        "Resolved to YYYY-MM-DD using today's date from the system "
                        "prompt."
                    ),
                },
                "tentative": {
                    "type": "boolean",
                    "description": (
                        "True if the date was phrased as provisional (subject to "
                        "change, probably, around)."
                    ),
                },
                "weight_pct": {
                    "type": "number",
                    "description": "Percentage of the final grade, if stated.",
                },
                "priority": {
                    "type": "integer",
                    "enum": [1, 2, 3],
                    "description": (
                        "1 = highest. Default by stakes: exams and anything over 20% "
                        "of the grade are 1; normal graded work 2; readings and "
                        "ungraded prep 3."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Detail worth keeping, e.g. chapter numbers.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": ADD_REMINDER,
        "description": (
            "Set a reminder to be sent back at a specific time. Use when Kaan asks "
            "to be reminded, nudged, or told about something later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "What to remind him of, phrased as it should be sent back "
                        "to him."
                    ),
                },
                "fire_at": {
                    "type": "string",
                    "description": (
                        "Absolute time as YYYY-MM-DD HH:MM:SS, resolved from the "
                        "current date and time in the system prompt. Use this "
                        "whenever the time is knowable, including for relative "
                        "phrasing like 'in 20 minutes'."
                    ),
                },
                "anchor": {
                    "type": "string",
                    "description": (
                        "Last resort only. If the system prompt lists today's "
                        "remaining events and one of them is the event he means, "
                        "work out fire_at from its end time and leave this empty "
                        "— naming the event here instead of computing the time "
                        "just moves the problem. Use this only when no listed "
                        "event matches, e.g. an event on a later day."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": SET_GYM_SPLIT,
        "description": (
            "Set or correct which training split falls on a given weekday, e.g. "
            "'Wednesdays are legs now' or 'swap Friday to rest'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day_of_week": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 6,
                    "description": "0 = Monday through 6 = Sunday.",
                },
                "split_name": {
                    "type": "string",
                    "description": "Push, Pull, Legs, Rest, and so on.",
                },
            },
            "required": ["day_of_week", "split_name"],
        },
    },
    {
        "name": UPDATE_TASK,
        "description": (
            "Change an existing task: mark it done or started, move its due date, "
            "or change its priority."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_query": {
                    "type": "string",
                    "description": (
                        "How Kaan referred to the task, verbatim enough to match it."
                    ),
                },
                "course": {
                    "type": "string",
                    "description": "Course code, if he named one.",
                },
                "status": {
                    "type": "string",
                    "enum": ["not_started", "in_progress", "done", "archived"],
                },
                "due_date": {
                    "type": "string",
                    "description": "New due date as YYYY-MM-DD.",
                },
                "priority": {"type": "integer", "enum": [1, 2, 3]},
            },
            "required": ["task_query"],
        },
    },
    {
        "name": SET_GOAL,
        "description": (
            "Record a personal goal — something Kaan wants to do, separate from "
            "coursework obligations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tier": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "Infer from phrasing; default to daily if unstated.",
                },
            },
            "required": ["text", "tier"],
        },
    },
    {
        "name": SAVE_NOTE,
        "description": (
            "Store a piece of information with no deadline and no action attached — "
            "something to remember, not to do."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "A few lowercase keywords.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": ANSWER_QUERY,
        "description": (
            "Kaan is asking about his own data — what is due, what is on today, how "
            "much a thing is worth. Read-only; nothing is saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, normalised.",
                },
                "scope": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "tasks",
                            "reminders",
                            "goals",
                            "gym",
                            "courses",
                            "notes",
                        ],
                    },
                    "description": "Which data would answer it.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": JUST_CHAT,
        "description": (
            "Conversation with nothing to save and nothing to look up. Venting, "
            "thinking out loud, small talk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What he said, so the reply can respond to it.",
                }
            },
        },
    },
    {
        "name": CHECKIN_REPLY,
        "description": (
            "Kaan is answering the evening check-in. Use this ONLY when the "
            "system prompt says a check-in is awaiting a reply and this message "
            "reads as an answer to it — what he did or didn't get to today. If "
            "no check-in is pending, this intent does not apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "His answer, verbatim enough to parse in detail later.",
                }
            },
            "required": ["summary"],
        },
    },
    {
        "name": ASK_CLARIFICATION,
        "description": (
            "Use ONLY when the message cannot be resolved from context and a wrong "
            "guess would write bad data. Do not use for a missing detail you can "
            "reasonably default. Ask exactly one specific question naming the actual "
            "options — never a generic 'can you clarify?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One targeted question naming the actual options.",
                },
                "reason": {
                    "type": "string",
                    "enum": ["intent_unclear", "ambiguous_course", "missing_field"],
                },
            },
            "required": ["question", "reason"],
        },
    },
]

#: Clarification reason -> Section 8 error code, for logging.
CLARIFICATION_CODES = {
    "intent_unclear": "E101",
    "ambiguous_course": "E102",
    "missing_field": "E103",
}

INTENT_NAMES = frozenset(tool["name"] for tool in INTENT_TOOLS)
