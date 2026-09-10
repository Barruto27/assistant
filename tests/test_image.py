"""Dated items from an image, and the handler gap that hid them."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import image_reader as ir  # noqa: E402
from bot.errors import AssistantError, E, setup_logging  # noqa: E402
from db import database  # noqa: E402
from fakes import FakeTextBlock, FakeToolUseBlock  # noqa: E402

setup_logging(Path(tempfile.gettempdir()) / "assistant-tests.log")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PAYLOAD = {
    "course": "AP/CMDS 1630 Section A",
    "items": [
        {"title": "Weekly Check-in: Week 1", "due_date": "2026-09-11", "notes": "Introductions"},
        {"title": "Weekly Check-in: Week 2", "due_date": "2026-09-18"},
        {"title": "Weekly Check-in: Week 3", "due_date": "2026-09-25"},
    ],
}


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


class MediaTypeTestCase(unittest.TestCase):
    """Telegram's mime hint can be missing, so the bytes decide."""

    def test_recognises_common_formats(self) -> None:
        self.assertEqual(ir.media_type(PNG), "image/png")
        self.assertEqual(ir.media_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(ir.media_type(b"GIF89a..."), "image/gif")

    def test_rejects_anything_else(self) -> None:
        with self.assertRaises(AssistantError) as ctx:
            ir.media_type(b"%PDF-1.4 this is a pdf")
        self.assertEqual(ctx.exception.code, E.UNPARSEABLE_DOCUMENT)


class ExtractTestCase(unittest.TestCase):
    def test_reads_items_and_normalises_the_course(self) -> None:
        client = FakeAnthropic(
            [FakeToolUseBlock(name="record_dated_items", input=PAYLOAD)]
        )
        course, items = ir.extract(PNG, "for cmds1630", client, "claude-sonnet-5")
        self.assertEqual(course, "CMDS 1630", "must match the code used elsewhere")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].due_date, "2026-09-11")

    def test_caption_is_sent_with_the_image(self) -> None:
        client = FakeAnthropic(
            [FakeToolUseBlock(name="record_dated_items", input=PAYLOAD)]
        )
        ir.extract(PNG, "for cmds1630, weekly check ins", client, "claude-sonnet-5")
        content = client.calls[0]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertIn("cmds1630", content[1]["text"])

    def test_unparseable_dates_are_discarded(self) -> None:
        client = FakeAnthropic([FakeToolUseBlock(name="record_dated_items", input={
            "items": [
                {"title": "Good", "due_date": "2026-09-11"},
                {"title": "Bad", "due_date": "Friday next week"},
                {"title": "Empty", "due_date": ""},
            ]})])
        _, items = ir.extract(PNG, "", client, "claude-sonnet-5")
        self.assertEqual([i.title for i in items], ["Good"])

    def test_empty_and_oversized_images_cost_no_api_call(self) -> None:
        client = FakeAnthropic([])
        for data in (b"", PNG + b"\x00" * ir.MAX_IMAGE_BYTES):
            with self.assertRaises(AssistantError):
                ir.extract(data, "", client, "claude-sonnet-5")
        self.assertEqual(client.calls, [])

    def test_no_tool_call_yields_nothing(self) -> None:
        client = FakeAnthropic([FakeTextBlock(text="I see a cat.")])
        self.assertEqual(ir.extract(PNG, "", client, "claude-sonnet-5"), (None, []))

    def test_api_failure_carries_a_code(self) -> None:
        client = FakeAnthropic([], error=RuntimeError("boom"))
        with self.assertRaises(AssistantError) as ctx:
            ir.extract(PNG, "", client, "claude-sonnet-5")
        self.assertEqual(ctx.exception.code, E.CLAUDE)


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self._tmp.name) / "t.sqlite3")
        database.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def items(self) -> list[ir.DatedItem]:
        return [
            ir.DatedItem(title="Weekly Check-in: Week 1", due_date="2026-09-11"),
            ir.DatedItem(title="Weekly Check-in: Week 2", due_date="2026-09-18"),
        ]

    def test_adds_tagged_tasks(self) -> None:
        self.assertEqual(ir.ingest(self.conn, "CMDS 1630", self.items()), 2)
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY due_date").fetchall()
        self.assertEqual([r["course"] for r in rows], ["CMDS 1630"] * 2)
        self.assertEqual([r["source"] for r in rows], ["text"] * 2)

    def test_never_replaces_existing_work(self) -> None:
        """An image shows one slice of a course; replacing would delete the rest."""
        from bot import repository as repo

        repo.add_task(
            self.conn, title="Final Essay", course="CMDS 1630",
            due_date="2026-12-08", weight_pct=35, source="syllabus",
        )
        ir.ingest(self.conn, "CMDS 1630", self.items())
        titles = {r["title"] for r in self.conn.execute("SELECT title FROM tasks")}
        self.assertIn("Final Essay", titles)
        self.assertEqual(len(titles), 3)


class ReceiptTestCase(unittest.TestCase):
    def test_lists_what_landed_in_date_order(self) -> None:
        items = [
            ir.DatedItem(title="Week 2", due_date="2026-09-18"),
            ir.DatedItem(title="Week 1", due_date="2026-09-11"),
        ]
        text = ir.receipt("CMDS 1630", items)
        self.assertIn("Added 2 item(s) to CMDS 1630", text)
        self.assertLess(text.index("Week 1"), text.index("Week 2"))

    def test_says_so_when_nothing_was_found(self) -> None:
        self.assertIn("couldn't find any dates", ir.receipt(None, []))

    def test_asks_for_a_course_when_none_was_identified(self) -> None:
        text = ir.receipt(None, [ir.DatedItem(title="Quiz", due_date="2026-10-01")])
        self.assertIn("No course on these", text)


class HandlerCoverageTestCase(unittest.TestCase):
    """The gap itself: every message kind must reach some handler."""

    def test_photo_and_catch_all_handlers_are_registered(self) -> None:
        import inspect

        from bot import main

        source = inspect.getsource(main.build_application)
        self.assertIn("filters.PHOTO", source, "captioned screenshots were dropped")
        self.assertIn("on_unhandled", source, "nothing may vanish without a reply")
        # The catch-all must be registered last, or it would swallow everything.
        self.assertGreater(
            source.index("on_unhandled"), source.index("filters.PHOTO")
        )


if __name__ == "__main__":
    unittest.main()
