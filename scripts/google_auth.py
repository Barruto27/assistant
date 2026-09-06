"""One-time Google Calendar authorisation (plan Section 1 / Section 6).

    python -m scripts.google_auth

Opens a browser, asks you to sign in to the Google account whose calendar you
want read, and caches the resulting token. Re-run it if the token ever stops
working.

Before this will work you need, in the Google Cloud console:
  1. A project with the Google Calendar API enabled.
  2. An OAuth client of type "Desktop app", with the JSON downloaded to the
     path in GOOGLE_CLIENT_SECRETS (default secrets/google_client_secret.json).
  3. The OAuth consent screen PUBLISHED. Left in "Testing", Google expires the
     refresh token after 7 days and the bot silently stops seeing your calendar
     until you re-authorise. Publishing an unverified single-user app is fine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot.google_calendar import SCOPES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account",
        choices=("personal", "university"),
        default="personal",
        help="Which token file to write. Defaults to personal.",
    )
    args = parser.parse_args(argv)

    from bot.config import ConfigError, load_settings

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    secrets: Path = settings.google_client_secrets
    if not secrets.exists():
        print(f"Missing OAuth client secrets at {secrets}", file=sys.stderr)
        print(
            "Download it from the Google Cloud console: APIs & Services > "
            "Credentials > Create OAuth client ID > Desktop app, then save the "
            f"JSON to that path.",
            file=sys.stderr,
        )
        return 1

    token_path = (
        settings.google_token_personal
        if args.account == "personal"
        else settings.google_token_university
    )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(port=0, prompt="consent")

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {args.account} calendar token to {token_path}")

    if not creds.refresh_token:
        print(
            "\nWARNING: Google did not return a refresh token, so this will stop "
            "working when the access token expires in about an hour. Revoke the "
            "app's access at myaccount.google.com/permissions and re-run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
