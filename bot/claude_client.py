"""The single Claude call that classifies and extracts (plan Section 4).

``Classifier`` is a Protocol so the router can be tested against a scripted
fake with no network and no API key — see ``tests/fakes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from bot.errors import AssistantError, E
from bot.intents import ASK_CLARIFICATION, INTENT_NAMES, INTENT_TOOLS, ParsedIntent
from bot.voice import VOICE

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class PromptContext:
    """Everything the parse call needs to resolve a message without asking.

    Populated fresh per message — a stale date silently produces wrong due
    dates, which is the failure mode hardest to notice after the fact.
    """

    now: datetime
    timezone: str = "America/Toronto"
    courses: list[str] = field(default_factory=list)
    week_number: int | None = None
    #: Today's remaining events, so "after my next class" resolves to a time
    #: instead of coming back as an unanswerable anchor.
    upcoming_events: list[tuple[str, str]] = field(default_factory=list)
    #: True when tonight's check-in is still awaiting an answer, so a bare
    #: "did the reflection, skipped the reading" routes there rather than
    #: being filed as a new task.
    checkin_pending: bool = False

    def render(self) -> str:
        lines = [
            f"Current date and time: {self.now:%Y-%m-%d %H:%M} "
            f"({WEEKDAYS[self.now.weekday()]}, {self.timezone}).",
        ]
        if self.week_number is not None:
            lines.append(f"It is week {self.week_number} of the semester.")
        if self.courses:
            lines.append("Kaan's courses this semester: " + ", ".join(self.courses) + ".")
            lines.append(
                "Match any course mentioned to that list, including informal names "
                "and misspellings. If a message could plausibly mean two of them, "
                "use ask_clarification with reason 'ambiguous_course' rather than "
                "guessing."
            )
        else:
            lines.append(
                "No courses are on file yet, so accept whatever course label he uses."
            )

        if self.checkin_pending:
            lines.append("")
            lines.append(
                "An evening check-in was sent and is still unanswered. A message "
                "that reads as an answer to it - what he did or didn't get to - "
                "is checkin_reply, not a new task."
            )

        if self.upcoming_events:
            lines.append("")
            lines.append("Still to come today:")
            lines.extend(f"- {when} {what}" for when, what in self.upcoming_events)
            lines.append(
                "Use these to resolve a relative reminder - 'after my next "
                "class' means shortly after that class ends. Only fall back to "
                "anchor when nothing here settles it."
            )
        return "\n".join(lines)


SYSTEM_PROMPT = """\
{voice}

Your job right now is narrow: read one message from Kaan and call exactly one
tool that captures what he wants. You are not replying to him — a separate step
writes the reply. Call a tool; do not write prose.

{context}

Rules:
- Call exactly one tool, always.
- Fill in every field you can reasonably infer. A missing detail you can default
  sensibly is not a reason to ask.
- Resolve all relative dates and times against the current date and time above.
- Prefer a confident write over a question. ask_clarification exists for the
  case where a wrong guess would save bad data he might not notice — most often
  two courses that both fit. Use it sparingly.
- One message can only be one intent. If he mentions several things, pick the
  one he is actually asking you to act on.
"""


class Classifier(Protocol):
    """Anything that can turn a message into a ParsedIntent."""

    def classify(self, message: str, context: PromptContext) -> ParsedIntent: ...


class Writer(Protocol):
    """Anything that can write prose from a system prompt and a data block."""

    def compose(self, system: str, user: str, *, max_tokens: int = 1024) -> str: ...


class ToolCaller(Protocol):
    """Anything that can run one named tool and hand back its input."""

    def call_tool(
        self, system: str, user: str, tool: dict, *, max_tokens: int = 1024
    ) -> dict: ...


def build_system_prompt(context: PromptContext) -> str:
    return SYSTEM_PROMPT.format(voice=VOICE, context=context.render())


def parse_tool_use(blocks: list[Any]) -> ParsedIntent:
    """Pull the tool call out of a Claude response.

    Shared by the real client and the fake so both fail the same way on a
    response with no tool call.
    """
    for block in blocks:
        if getattr(block, "type", None) == "tool_use":
            name = block.name
            if name not in INTENT_NAMES:
                raise AssistantError(
                    E.CLAUDE, f"Claude called an unknown tool {name!r}."
                )
            return ParsedIntent(name=name, fields=dict(block.input or {}))

    # No tool call. Treat as unclassifiable rather than inventing an intent.
    return ParsedIntent(
        name=ASK_CLARIFICATION,
        fields={
            "question": (
                "Not sure what to do with that — task, reminder, or just chatting?"
            ),
            "reason": "intent_unclear",
        },
    )


class AnthropicClient:
    """The real client. Constructed lazily so the bot starts without a key.

    Implements both Classifier (tool-use, for parsing messages) and Writer
    (plain prose, for the morning brief).
    """

    def __init__(self, api_key: str, model: str, *, max_tokens: int = 1024) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def classify(self, message: str, context: PromptContext) -> ParsedIntent:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=build_system_prompt(context),
                tools=INTENT_TOOLS,
                tool_choice={"type": "any"},  # force a tool call, never prose
                messages=[{"role": "user", "content": message}],
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises a family of errors
            raise AssistantError(
                E.CLAUDE, _explain(exc), cause=exc, trigger=message
            ) from exc

        return parse_tool_use(response.content)

    def call_tool(
        self, system: str, user: str, tool: dict, *, max_tokens: int = 1024
    ) -> dict:
        """Run one named tool and return its input.

        Structured extraction goes through the tool schema rather than asking
        for JSON in prose: the schema is enforced, and there is no parsing step
        to get wrong.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a family of errors
            raise AssistantError(
                E.CLAUDE, _explain(exc), cause=exc, trigger=user[:200]
            ) from exc

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                return dict(block.input or {})
        return {}


    def compose(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        """Free prose, no tools. Used by the brief and check-in."""
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a family of errors
            raise AssistantError(
                E.CLAUDE, _explain(exc), cause=exc, trigger=user[:200]
            ) from exc

        return "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()


def _explain(exc: BaseException) -> str:
    """Turn an SDK exception into something actionable.

    "Couldn't reach Claude" is misleading for a billing or key problem, which
    needs a specific fix in the Anthropic console rather than a retry. The
    account-level cases are worth naming; everything else stays generic and the
    detail goes to the log.
    """
    status = getattr(exc, "status_code", None)
    detail = str(exc).lower()

    if "credit balance is too low" in detail:
        return (
            "Out of Anthropic API credits. Add some at console.anthropic.com "
            "under Plans & Billing — note that a Claude.ai subscription is "
            "billed separately and doesn't cover API use."
        )
    if status == 401:
        return "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
    if status == 403:
        return "That API key isn't allowed to use this model."
    if status == 429:
        return "Hit the Anthropic rate limit. Try again in a moment."
    return "Couldn't reach Claude to read that."
