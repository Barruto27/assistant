"""Course email over IMAP (plan Section 6)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import brief, email_reader as er  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from db import database  # noqa: E402
from fakes import FakeTextBlock, FakeToolUseBlock  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")


class FakeResponse:
    def __init__(self, blocks) -> None:
        self.content = blocks


class FakeAnthropic:
    def __init__(self, blocks, *, error: BaseException | None = None) -> None:
        self._blocks = blocks
        self._error = error
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeResponse(self._blocks)


def build_message(*, plain: str | None = None, html: str | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Prof Smith <smith@yorku.ca>"
    message["Subject"] = "Assignment 2 moved"
    if plain is not None:
        message.set_content(plain)
    if html is not None:
        if plain is None:
            message.set_content("fallback")
        message.add_alternative(html, subtype="html")
    return message


class BodyExtractionTestCase(unittest.TestCase):
    """Real course mail is messy; the extractor must not choke on it."""

    def test_prefers_plain_text(self) -> None:
        message = build_message(plain="A2 is now due Friday.", html="<p>ignored</p>")
        self.assertIn("A2 is now due Friday.", er._extract_body(message))

    def test_falls_back_to_stripped_html(self) -> None:
        message = EmailMessage()
        message["From"] = "prof@yorku.ca"
        message.set_content("<h1>Class cancelled</h1><p>See you Monday.</p>", subtype="html")
        body = er._extract_body(message)
        self.assertIn("Class cancelled", body)
        self.assertNotIn("<h1>", body)

    def test_script_and_style_are_dropped(self) -> None:
        html = "<style>p{color:red}</style><script>alert(1)</script><p>Real text</p>"
        stripped = er._strip_html(html)
        self.assertIn("Real text", stripped)
        self.assertNotIn("alert", stripped)
        self.assertNotIn("color:red", stripped)

    def test_body_is_truncated(self) -> None:
        message = build_message(plain="x" * (er.BODY_CHARS * 3))
        self.assertLessEqual(len(er._extract_body(message)), er.BODY_CHARS)

    def test_encoded_headers_are_decoded(self) -> None:
        self.assertEqual(er._decode("=?utf-8?q?Caf=C3=A9_hours?="), "Café hours")
        self.assertEqual(er._decode(None), "")


class AllowlistTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_allowlist_reads_nothing(self) -> None:
        """The important guarantee: no allowlist means no inbox scan at all.

        The mailbox has thousands of personal messages. Defaulting to "read
        everything" would be both expensive and a privacy problem.
        """
        self.assertEqual(er.known_senders(self.conn), [])
        self.assertEqual(
            er.fetch(
                host="imap.invalid", user="u", password="p",
                senders=[], since=date(2026, 9, 1),
            ),
            [],
            "must return before opening a connection",
        )

    def test_inactive_senders_are_excluded(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO known_senders (pattern, course_label, active) "
                "VALUES ('yorku.ca', NULL, 1)"
            )
            self.conn.execute(
                "INSERT INTO known_senders (pattern, course_label, active) "
                "VALUES ('spam.example', NULL, 0)"
            )
        self.assertEqual(er.known_senders(self.conn), [("yorku.ca", None)])

    def test_course_labels_come_back_with_the_pattern(self) -> None:
        with database.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO known_senders (pattern, course_label) "
                "VALUES ('smith@yorku.ca', 'PSYC 3265')"
            )
        self.assertEqual(er.known_senders(self.conn), [("smith@yorku.ca", "PSYC 3265")])


class FlagTestCase(unittest.TestCase):
    SAMPLE = [
        er.Email(
            sender="smith@yorku.ca",
            subject="A2 moved",
            received=datetime(2026, 9, 9, 10, 0),
            body="Assignment 2 is now due Friday Oct 9 instead of Oct 2.",
            course_label="PSYC 3265",
        )
    ]

    def test_no_emails_means_no_api_call(self) -> None:
        client = FakeAnthropic([])
        self.assertEqual(er.flag([], client, "claude-sonnet-5"), [])
        self.assertEqual(client.calls, [])

    def test_parses_flagged_items(self) -> None:
        client = FakeAnthropic([
            FakeToolUseBlock(
                name="flag_items",
                input={"items": [{
                    "kind": "deadline_change",
                    "course": "PSYC 3265",
                    "summary": "Assignment 2 moved to Oct 9",
                    "new_date": "2026-10-09",
                    "sender": "smith@yorku.ca",
                }]},
            )
        ])
        flagged = er.flag(self.SAMPLE, client, "claude-sonnet-5")
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].kind, "deadline_change")
        self.assertEqual(flagged[0].new_date, "2026-10-09")
        self.assertIn("deadline change", flagged[0].line())

    def test_empty_result_is_normal(self) -> None:
        client = FakeAnthropic([
            FakeToolUseBlock(name="flag_items", input={"items": []})
        ])
        self.assertEqual(er.flag(self.SAMPLE, client, "claude-sonnet-5"), [])

    def test_items_without_a_summary_are_dropped(self) -> None:
        client = FakeAnthropic([
            FakeToolUseBlock(
                name="flag_items",
                input={"items": [{"kind": "announcement", "summary": "  "}]},
            )
        ])
        self.assertEqual(er.flag(self.SAMPLE, client, "claude-sonnet-5"), [])

    def test_no_tool_call_yields_nothing(self) -> None:
        client = FakeAnthropic([FakeTextBlock(text="Nothing important.")])
        self.assertEqual(er.flag(self.SAMPLE, client, "claude-sonnet-5"), [])

    def test_api_failure_raises_with_a_code(self) -> None:
        client = FakeAnthropic([], error=RuntimeError("boom"))
        with self.assertRaises(AssistantError) as ctx:
            er.flag(self.SAMPLE, client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.CLAUDE)


class BriefIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_flagged_email_reaches_the_facts_marked_unsaved(self) -> None:
        flagged = [er.FlaggedEmail(
            kind="deadline_change",
            summary="Assignment 2 moved to Oct 9",
            course="PSYC 3265",
            new_date="2026-10-09",
        )]
        context = brief.assemble(
            self.conn, now=datetime(2026, 9, 14, 7, 30), flagged_emails=flagged
        )
        facts = brief.render_facts(context)
        self.assertIn("Assignment 2 moved to Oct 9", facts)
        self.assertIn("not saved", facts, "Section 6: Kaan confirms before writing")

    def test_nothing_flagged_omits_the_section(self) -> None:
        context = brief.assemble(self.conn, now=datetime(2026, 9, 14, 7, 30))
        self.assertNotIn("FLAGGED IN EMAIL", brief.render_facts(context))

    def test_flagged_email_is_never_written_to_tasks(self) -> None:
        """Section 6 is explicit: surfaced only, never auto-saved."""
        flagged = [er.FlaggedEmail(kind="new_work", summary="New quiz next week")]
        brief.assemble(self.conn, now=datetime(2026, 9, 14, 7, 30), flagged_emails=flagged)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
