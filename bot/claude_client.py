"""The single Claude call that classifies and extracts (plan Section 4).

``Classifier`` is a Protocol so the router can be tested against a scripted
fake with no network and no API key — see ``tests/fakes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from bot.errors import AssistantError, E
from bot.intents import (
    ANSWER_QUERY,
    ASK_CLARIFICATION,
    CHECKIN_REPLY,
    INTENT_NAMES,
    INTENT_TOOLS,
    JUST_CHAT,
    ParsedIntent,
)
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

Your job right now is narrow: read one message from Kaan and call the tools
that capture what he wants. You are not replying to him — a separate step
writes the reply. Call tools; do not write prose.

{context}

Rules:
- Always call at least one tool.
- One message often holds more than one instruction, and each one gets its own
  tool call. "Remind me today and tomorrow to upload the forms" is two
  reminders. "Add the essay for Friday and remind me tonight to start it" is a
  task and a reminder. Do not drop the second half, and do not fold two
  different things into one call.
- Call a tool once per distinct thing he wants. Do not split a single
  instruction into several calls, and do not repeat the same call.
- Fill in every field you can reasonably infer. A missing detail you can default
  sensibly is not a reason to ask.
- Resolve all relative dates and times against the current date and time above.
- Prefer a confident write over a question. ask_clarification exists for the
  case where a wrong guess would save bad data he might not notice — most often
  two courses that both fit. Use it sparingly, and on its own.
- A message reporting that something is finished — "iclicker done in class",
  "handed in the essay" — is update_task, even when no check-in is open. Telling
  you something happened is an instruction to record it, not small talk.
"""


class Classifier(Protocol):
    """Anything that can turn a message into the intents it contains."""

    def classify(
        self, message: str, context: PromptContext
    ) -> list[ParsedIntent]: ...


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


#: Upper bound on how many things one message may ask for. Sized for a full
#: week of gym split, which is a legitimate seven instructions because
#: set_gym_split takes one weekday per call; anything past that is the model
#: fragmenting a single instruction. A cap that truncates is the same silent
#: data loss this change exists to remove, so it is set above the real ceiling
#: rather than at it.
MAX_INTENTS = 8

#: How long any one Claude call may take before it is given up on.
#:
#: The SDK default is a 600s read timeout with two retries, so a wedged request
#: can hang for thirty minutes - and it hangs holding the database lock, which
#: stops reminders firing and every other message being answered. One request
#: really did go quiet for three and a half minutes on Sep 11 while the API was
#: otherwise healthy. The slowest honest call measured is the morning brief at
#: about eight seconds.
REPLY_TIMEOUT_SECONDS = 40.0

#: Intents that answer in prose, each costing a second model call. One message
#: gets at most one of them: two questions asked together are one question, and
#: running both would double a reply time that is already the main complaint.
PROSE_INTENTS = frozenset({ANSWER_QUERY, CHECKIN_REPLY, JUST_CHAT})


def parse_tool_uses(blocks: list[Any]) -> list[ParsedIntent]:
    """Pull every tool call out of a Claude response, in order.

    A message can hold more than one instruction — "remind me today and
    tomorrow" is two reminders — and taking only the first silently dropped the
    rest.

    Shared by the real client and the fake so both fail the same way on a
    response with no tool call.
    """
    intents: list[ParsedIntent] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = block.name
        if name not in INTENT_NAMES:
            raise AssistantError(E.CLAUDE, f"Claude called an unknown tool {name!r}.")
        fields = dict(block.input or {})
        # The same call twice is the model repeating itself, not Kaan asking
        # twice; acting on it would double-write.
        key = (name, repr(sorted(fields.items(), key=lambda kv: kv[0])))
        if key in seen:
            continue
        seen.add(key)
        intents.append(ParsedIntent(name=name, fields=fields))

    if not intents:
        # No tool call. Treat as unclassifiable rather than inventing an intent.
        return [
            ParsedIntent(
                name=ASK_CLARIFICATION,
                fields={
                    "question": (
                        "Not sure what to do with that — task, reminder, or just "
                        "chatting?"
                    ),
                    "reason": "intent_unclear",
                },
            )
        ]

    # A question is an answer to the whole message, not one item in a list.
    clarification = next(
        (i for i in intents if i.name == ASK_CLARIFICATION), None
    )
    if clarification is not None:
        return [clarification]

    kept: list[ParsedIntent] = []
    prose_seen = False
    for intent in intents:
        if intent.name in PROSE_INTENTS:
            if prose_seen:
                continue
            prose_seen = True
        kept.append(intent)

    return kept[:MAX_INTENTS]


class AnthropicClient:
    """The real client. Constructed lazily so the bot starts without a key.

    Implements both Classifier (tool-use, for parsing messages) and Writer
    (plain prose, for the morning brief).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 1024,
        classify_model: str | None = None,
        timeout: float = REPLY_TIMEOUT_SECONDS,
        max_retries: int = 1,
    ) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self._model = model
        # Classification is a constrained tool call against a short prompt, and
        # it sits in front of every single message. On Sonnet it cost ~2.8s of
        # the ~10s Kaan was waiting; a smaller model does the same job in a
        # fraction of that. Prose still goes to the full model.
        self._classify_model = classify_model or model
        self._max_tokens = max_tokens

    def classify(self, message: str, context: PromptContext) -> list[ParsedIntent]:
        try:
            response = self._client.messages.create(
                model=self._classify_model,
                max_tokens=self._max_tokens,
                system=build_system_prompt(context),
                tools=INTENT_TOOLS,
                # "any" forces a tool call and never prose, while still allowing
                # several when the message holds several instructions.
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": message}],
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises a family of errors
            raise AssistantError(
                E.CLAUDE, _explain(exc), cause=exc, trigger=message
            ) from exc

        return parse_tool_uses(response.content)

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
    if type(exc).__name__ in ("APITimeoutError", "APIConnectionError"):
        return (
            "Claude took too long to answer that one. Nothing was saved - "
            "send it again."
        )
    return "Couldn't reach Claude to read that."
