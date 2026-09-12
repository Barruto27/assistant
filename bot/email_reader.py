"""University email over IMAP (plan Section 6).

York blocks Google Cloud API access for student accounts, so university mail is
forwarded to the personal Gmail account and read here over IMAP with an app
password. That is the fallback the plan documents, and it turns out to be the
better path anyway: no OAuth, no consent screen, and nothing that expires.

The inbox has thousands of messages, the large majority unread, so **filtering
is the design, not an optimisation**. Only mail from senders on the
``known_senders`` allowlist is fetched at all — an empty allowlist reads
nothing rather than everything, because the failure mode of scanning 5,000
personal emails through an LLM is both expensive and a privacy problem.

Nothing here writes to ``tasks``. Section 6 is explicit: findings are surfaced
as flagged items in the brief, and Kaan confirms before anything is saved.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from bot.errors import AssistantError, E, logger

IMAP_PORT = 993
#: Cap on messages pulled per run. A professor emailing more than this in the
#: window is not a case worth optimising for, and it bounds both cost and time.
MAX_MESSAGES = 40
#: How much of a message body to keep. Enough for an announcement; short enough
#: that a long forwarded thread doesn't dominate the extraction call.
BODY_CHARS = 2500


@dataclass(frozen=True)
class Email:
    sender: str
    subject: str
    received: datetime | None
    body: str
    course_label: str | None = None
    #: RFC 5322 Message-ID, or a digest standing in for one. Stable across
    #: fetches, which the model's wording of a flag is not.
    message_id: str = ""

    def summary(self) -> str:
        when = f"{self.received:%Y-%m-%d %H:%M}" if self.received else "unknown date"
        head = f"From: {self.sender} | {when} | Subject: {self.subject}"
        if self.course_label:
            head += f" | course: {self.course_label}"
        return f"{head}\n{self.body}"


def _message_id(message: Message, sender: str, subject: str, received) -> str:
    """The Message-ID header, or a digest that behaves like one.

    Only used to recognise the same email on a later scan, so it has to be
    stable rather than unguessable.
    """
    raw = (message.get("Message-ID") or "").strip()
    if raw:
        return raw
    stamp = received.isoformat() if received else ""
    digest = hashlib.sha256(f"{sender}|{subject}|{stamp}".encode()).hexdigest()
    return f"<digest:{digest[:32]}>"


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def _extract_body(message: Message) -> str:
    """Plain text if the message has any, else a crude strip of the HTML part."""
    text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                text = _payload(part)
                if text:
                    break
        if not text:
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    text = _strip_html(_payload(part))
                    break
    else:
        text = _payload(message)
        if message.get_content_type() == "text/html":
            text = _strip_html(text)

    collapsed = re.sub(r"\n{3,}", "\n\n", text).strip()
    return collapsed[:BODY_CHARS]


def _payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except (AssertionError, ValueError):
        return ""
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    without_blocks = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", without_blocks))


def _search_criteria(pattern: str) -> tuple[str, ...]:
    """Turn an allowlist pattern into IMAP search terms.

    Three forms, because matching only on sender misses the mail that matters
    most. Anything forwarded from the university address is university mail by
    definition, whoever actually sent it — a professor through eClass, a
    mailing list, the registrar. Gmail stamps X-Forwarded-For on every forward,
    so that header is a far better filter than guessing at sender domains.

        fwd:kaanoz@my.yorku.ca   -> forwarded from that address
        to:my.yorku.ca           -> addressed to that address
        yorku.ca                 -> from that sender (the default)
    """
    if pattern.startswith("fwd:"):
        return ("HEADER", "X-Forwarded-For", pattern[4:])
    if pattern.startswith("to:"):
        return ("TO", f'"{pattern[3:]}"')
    return ("FROM", f'"{pattern}"')


def known_senders(conn) -> list[tuple[str, str | None]]:
    """Active allowlist entries as (pattern, course_label)."""
    rows = conn.execute(
        "SELECT pattern, course_label FROM known_senders WHERE active = 1 "
        "ORDER BY pattern"
    ).fetchall()
    return [(row["pattern"], row["course_label"]) for row in rows]


def fetch(
    *,
    host: str,
    user: str,
    password: str,
    senders: list[tuple[str, str | None]],
    since: date,
    mailbox: str = "INBOX",
    limit: int = MAX_MESSAGES,
) -> list[Email]:
    """Recent mail from allowlisted senders. Never scans the whole inbox."""
    if not senders:
        logger.info("No known_senders configured; skipping the email scan")
        return []

    try:
        client = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=30)
    except OSError as exc:
        raise AssistantError(
            E.GMAIL, "Couldn't reach the mail server.", cause=exc
        ) from exc

    try:
        try:
            client.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise AssistantError(
                E.REFRESH_FAILED,
                "Gmail rejected the app password. Check GMAIL_APP_PASSWORD in "
                ".env, and that 2-Step Verification is still on for the account.",
                cause=exc,
            ) from exc

        client.select(mailbox, readonly=True)  # readonly: never marks anything read
        window = since.strftime("%d-%b-%Y")

        found: dict[bytes, str | None] = {}
        for pattern, course_label in senders:
            criteria = _search_criteria(pattern)
            status, data = client.search(None, "SINCE", window, *criteria)
            if status != "OK":
                logger.warning("IMAP search failed for pattern %r", pattern)
                continue
            for uid in data[0].split():
                found.setdefault(uid, course_label)

        results: list[Email] = []
        for uid in sorted(found, key=lambda u: int(u), reverse=True)[:limit]:
            status, data = client.fetch(uid, "(RFC822)")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            message = email.message_from_bytes(data[0][1])
            received: datetime | None
            try:
                received = email.utils.parsedate_to_datetime(message.get("Date", ""))
            except (TypeError, ValueError):
                received = None
            sender = _decode(message.get("From"))
            subject = _decode(message.get("Subject")) or "(no subject)"
            results.append(
                Email(
                    sender=sender,
                    subject=subject,
                    received=received,
                    body=_extract_body(message),
                    course_label=found[uid],
                    message_id=_message_id(message, sender, subject, received),
                )
            )
    except AssistantError:
        raise
    except imaplib.IMAP4.error as exc:
        raise AssistantError(E.GMAIL, "Couldn't read the mailbox.", cause=exc) from exc
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - logout failures are not interesting
            pass

    results.sort(key=lambda m: m.received or datetime.min.replace(tzinfo=None))
    logger.info("Fetched %d message(s) from %d allowlisted sender(s)", len(results), len(senders))
    return results


# ---------------------------------------------------------------------------
# Turning mail into flagged items
# ---------------------------------------------------------------------------

FLAG_TOOL: dict[str, Any] = {
    "name": "flag_items",
    "description": (
        "Report only the emails that change what Kaan has to do. Most mail "
        "changes nothing and should be left out entirely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "deadline_change",
                                "cancellation",
                                "new_work",
                                "announcement",
                            ],
                        },
                        "course": {"type": "string", "description": "Course code if identifiable."},
                        "summary": {
                            "type": "string",
                            "description": "One sentence, concrete. What changed and to what.",
                        },
                        "new_date": {
                            "type": "string",
                            "description": "YYYY-MM-DD if a date is being set or moved.",
                        },
                        "sender": {"type": "string", "description": "Who sent it."},
                        "source": {
                            "type": "integer",
                            "description": (
                                "The number in brackets above the email this came "
                                "from. Required, and must be one of the numbers shown."
                            ),
                        },
                    },
                    "required": ["kind", "summary", "source"],
                },
            }
        },
        "required": ["items"],
    },
}

FLAG_SYSTEM = """\
You are reading a student's course email to find the few messages that actually
change what he has to do.

Flag anything that changes what he has to do, or what he believes he has to
do:

- deadline changes, class or lab cancellations, room or time changes
- newly assigned work, and announcements carrying a concrete action or date
- corrections and clarifications, including something turning out NOT to be
  required, or not to be graded, or to be handed in differently than expected.
  A message that removes work counts as much as one that adds it - he cannot
  act on what he never hears.

Do not flag: routine reminders about work he already knows about, reading
postings, general course chatter with nothing to act on, administrative
newsletters, campus events, marketing, or anything with no date and no action.

A welcome message is chatter. A welcome message that names a room and a time
is an announcement with a concrete date.

Work only from the emails given. Never infer a date that isn't stated. If
nothing qualifies, return an empty list — that is the common and correct
outcome.

Today's date is {today}.
"""


@dataclass(frozen=True)
class FlaggedEmail:
    kind: str
    summary: str
    course: str | None = None
    new_date: str | None = None
    sender: str | None = None
    #: Which email this came from, so the same one is recognised on a later
    #: scan instead of being raised again as though it were new.
    message_id: str = ""

    def line(self) -> str:
        bits = [self.kind.replace("_", " ")]
        if self.course:
            bits.append(self.course)
        bits.append(self.summary)
        if self.new_date:
            bits.append(f"new date {self.new_date}")
        return " | ".join(bits)


def flag(emails: list[Email], client: Any, model: str, *, today: date | None = None) -> list[FlaggedEmail]:
    """Ask Claude which of these matter. Returns [] when none do."""
    if not emails:
        return []

    joined = "\n\n---\n\n".join(
        f"[{n}]\n{message.summary()}" for n, message in enumerate(emails, 1)
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=FLAG_SYSTEM.format(today=(today or date.today()).isoformat()),
            tools=[FLAG_TOOL],
            tool_choice={"type": "tool", "name": "flag_items"},
            messages=[{"role": "user", "content": joined}],
        )
    except Exception as exc:  # noqa: BLE001 - SDK raises a family of errors
        from bot.claude_client import _explain

        raise AssistantError(E.CLAUDE, _explain(exc), cause=exc) from exc

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "flag_items":
            flagged = []
            for raw in (block.input or {}).get("items", []):
                if not str(raw.get("summary", "")).strip():
                    continue
                flagged.append(
                    FlaggedEmail(
                        kind=str(raw.get("kind", "announcement")),
                        summary=str(raw["summary"]).strip(),
                        course=raw.get("course") or None,
                        new_date=raw.get("new_date") or None,
                        sender=raw.get("sender") or None,
                        message_id=_source_id(raw.get("source"), emails),
                    )
                )
            return flagged
    return []


def _source_id(source: Any, emails: list[Email]) -> str:
    """Resolve the model's 1-based index back to a Message-ID.

    An out-of-range or missing index leaves the id empty rather than pointing
    at the wrong email: an unidentified flag is still shown, it just cannot be
    remembered between scans.
    """
    try:
        index = int(source)
    except (TypeError, ValueError):
        return ""
    if 1 <= index <= len(emails):
        return emails[index - 1].message_id
    logger.warning("Flag cited email %r, which is not in the %d scanned", source, len(emails))
    return ""


def default_window(days: int = 3) -> date:
    """How far back to look. Short: the brief is about what changed recently."""
    return date.today() - timedelta(days=days)
