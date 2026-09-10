"""Error funnel behaviour (plan Section 8)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot.errors import E, setup_logging
from bot.main import _is_transient

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class TransientErrorTestCase(unittest.TestCase):
    """A Telegram hiccup must not become a text message."""

    def test_network_errors_are_transient(self) -> None:
        from telegram.error import NetworkError, TimedOut

        self.assertTrue(_is_transient(NetworkError("Bad Gateway")))
        self.assertTrue(_is_transient(TimedOut()))

    def test_real_failures_are_not_transient(self) -> None:
        from telegram.error import TelegramError

        self.assertFalse(_is_transient(ValueError("bad data")))
        self.assertFalse(_is_transient(TelegramError("chat not found")))
        self.assertFalse(_is_transient(None))

    def test_transient_code_exists(self) -> None:
        self.assertEqual(E.TRANSIENT_NETWORK, "E206")
