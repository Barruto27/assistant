"""Small shared formatters.

Kept in one place because the same number is shown to Kaan by the brief, the
receipts, and the syllabus import, and they must agree.
"""

from __future__ import annotations


def pct(value: float | int | None) -> str | None:
    """A grade weight, readable.

    Dividing a term weight across occurrences produces things like
    5/11 = 0.4545454545, and "%g" renders that in full. Two decimals is past
    the point of caring about a single check-in, and trailing zeros are noise:
    17.0 is "17", 0.4545 is "0.45", 12.5 stays "12.5".
    """
    if value is None:
        return None
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"
