"""Test doubles for the Claude call.

The pipeline is tested against a scripted classifier so the suite runs with no
network, no API key, and no cost. ``FakeAnthropicResponse`` exists so the real
client's response-unpacking is exercised too, not just the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.claude_client import PromptContext
from bot.intents import ParsedIntent


class ScriptedClassifier:
    """Returns queued intents in order, recording the contexts it was given.

    One queue entry is one message. Pass a list as an entry to script a message
    that holds several instructions, the way "remind me today and tomorrow"
    does.
    """

    def __init__(self, *intents: ParsedIntent | list[ParsedIntent]) -> None:
        self._queue = list(intents)
        self.calls: list[tuple[str, PromptContext]] = []

    def classify(self, message: str, context: PromptContext) -> list[ParsedIntent]:
        self.calls.append((message, context))
        if not self._queue:
            raise AssertionError("ScriptedClassifier ran out of queued intents")
        entry = self._queue.pop(0)
        return list(entry) if isinstance(entry, list) else [entry]


class ExplodingClassifier:
    """Raises whatever it was given, for testing failure paths."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def classify(self, message: str, context: PromptContext) -> list[ParsedIntent]:
        raise self._error


@dataclass
class FakeToolUseBlock:
    """Shaped like an Anthropic tool_use content block."""

    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"
