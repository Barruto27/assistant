"""Google Calendar read access (plan Section 6).

Read-only, personal account. Authorisation happens once via
``python -m scripts.google_auth``; after that the cached refresh token is used
silently and refreshed as needed.

The refresh token expiring every 7 days is the classic failure here: it happens
when the OAuth consent screen is left in "testing" mode. That is a console
setting, not something code can work around, so ``E301`` says so explicitly
rather than just reporting an auth failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.errors import AssistantError, E, logger

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


@dataclass(frozen=True)
class CalendarEvent:
    summary: str
    start: datetime | date
    end: datetime | date | None
    all_day: bool
    recurring: bool
    location: str | None = None

    @property
    def is_lecture(self) -> bool:
        """Recurring events are treated as lectures/standing commitments.

        The brief's weekly summary excludes these — a repeating class every week
        is not news, whereas a one-off appointment is.
        """
        return self.recurring

    def when(self) -> str:
        if self.all_day:
            return "all day"
        return f"{self.start:%H:%M}"


def _load_credentials(token_path: Path, client_secrets: Path):
    """Return valid credentials, refreshing if needed. Never opens a browser."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        raise AssistantError(
            E.TOKEN_EXPIRED,
            "Google Calendar isn't connected yet. Run: python -m scripts.google_auth",
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, KeyError) as exc:
        raise AssistantError(
            E.TOKEN_EXPIRED,
            f"The saved Google token at {token_path.name} is unreadable. "
            "Re-run: python -m scripts.google_auth",
            cause=exc,
        ) from exc

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AssistantError(
                E.REFRESH_FAILED,
                "Google refused to refresh the calendar token. This usually means "
                "the OAuth consent screen is still in Testing mode, which expires "
                "refresh tokens every 7 days — publish it in the Google Cloud "
                "console, then re-run: python -m scripts.google_auth",
                cause=exc,
            ) from exc
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Refreshed Google Calendar token")
        return creds

    raise AssistantError(
        E.TOKEN_EXPIRED,
        "The Google Calendar token is no longer valid. Re-run: "
        "python -m scripts.google_auth",
    )


def _service(token_path: Path, client_secrets: Path):
    from googleapiclient.discovery import build

    creds = _load_credentials(token_path, client_secrets)
    # cache_discovery=False avoids a noisy warning and a stale on-disk cache.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_endpoint(raw: dict) -> tuple[datetime | date, bool]:
    """Google gives either {'date': ...} for all-day or {'dateTime': ...}."""
    if "date" in raw:
        return date.fromisoformat(raw["date"]), True
    return datetime.fromisoformat(raw["dateTime"]), False


def list_events(
    token_path: Path,
    client_secrets: Path,
    *,
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
    max_results: int = 50,
) -> list[CalendarEvent]:
    """Events between two moments, expanded from recurrences, in time order."""
    from googleapiclient.errors import HttpError

    service = _service(token_path, client_secrets)
    try:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,  # expand recurring series into instances
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
    except HttpError as exc:
        raise AssistantError(
            E.CALENDAR, "Couldn't read your calendar.", cause=exc
        ) from exc

    events: list[CalendarEvent] = []
    for item in response.get("items", []):
        if item.get("status") == "cancelled":
            continue
        start_at, all_day = _parse_endpoint(item["start"])
        end_at, _ = _parse_endpoint(item["end"]) if item.get("end") else (None, False)
        events.append(
            CalendarEvent(
                summary=item.get("summary", "(no title)"),
                start=start_at,
                end=end_at,
                all_day=all_day,
                # An expanded instance of a repeating series carries this key.
                recurring="recurringEventId" in item,
                location=item.get("location"),
            )
        )
    return events


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Midnight-to-midnight for one local day, timezone-aware."""
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def events_for_day(
    token_path: Path, client_secrets: Path, day: date, timezone: str
) -> list[CalendarEvent]:
    start, end = day_bounds(day, timezone)
    return list_events(token_path, client_secrets, start=start, end=end)


def events_for_week(
    token_path: Path, client_secrets: Path, day: date, timezone: str
) -> list[CalendarEvent]:
    """The seven days beginning on ``day``."""
    start, _ = day_bounds(day, timezone)
    return list_events(token_path, client_secrets, start=start, end=start + timedelta(days=7))
