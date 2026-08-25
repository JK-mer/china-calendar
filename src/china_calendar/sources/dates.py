"""Deterministic German/English date-string parsing for Tier 2 sources.

Handles the forms the seeded sources actually use. Anything these functions
cannot parse stays unparsed — the LLM extract() call is the fallback for
genuinely messy strings, never a replacement for this.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {
    # German
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
    # English
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}

_MONTH_RE = "|".join(MONTHS)

# "29. Oktober 2026" / "29 October 2026"
_SINGLE = re.compile(rf"(\d{{1,2}})\.?\s+({_MONTH_RE})\s+(\d{{4}})", re.IGNORECASE)

# "vom 29. bis 31. Oktober 2026" / "29. bis 31. Oktober 2026" / "29.–31. Oktober 2026"
_RANGE_SAME_MONTH = re.compile(
    rf"(\d{{1,2}})\.?\s*(?:bis|–|-|—)\s*(\d{{1,2}})\.?\s+({_MONTH_RE})\s+(\d{{4}})",
    re.IGNORECASE,
)

# "vom 30. September bis 2. Oktober 2026"
_RANGE_CROSS_MONTH = re.compile(
    rf"(\d{{1,2}})\.?\s+({_MONTH_RE})\s+(?:bis|–|-|—)\s+(\d{{1,2}})\.?\s+({_MONTH_RE})\s+(\d{{4}})",
    re.IGNORECASE,
)


def parse_date_range(text: str) -> tuple[str | None, str | None]:
    """(start_iso, end_iso) from a free-form date string, or (None, None)."""
    match = _RANGE_CROSS_MONTH.search(text)
    if match:
        d1, m1, d2, m2, year = match.groups()
        return (
            date(int(year), MONTHS[m1.lower()], int(d1)).isoformat(),
            date(int(year), MONTHS[m2.lower()], int(d2)).isoformat(),
        )
    match = _RANGE_SAME_MONTH.search(text)
    if match:
        d1, d2, month, year = match.groups()
        m = MONTHS[month.lower()]
        return (
            date(int(year), m, int(d1)).isoformat(),
            date(int(year), m, int(d2)).isoformat(),
        )
    match = _SINGLE.search(text)
    if match:
        day, month, year = match.groups()
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat(), None
    return None, None
