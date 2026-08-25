"""Bundestag committee Tagesordnungen RSS (#11, the committee-agenda prize).

`https://www.bundestag.de/static/appdata/includes/rss/tagesordnungen.rss`
announces upcoming committee sittings (one item per TO publication; a sitting
gets extra items for each Änderungs-/Ergänzungsmitteilung). Titles are
free-form per committee secretariat, e.g.:

    Inneres: 39. Sitzung am Dienstag, dem 4. August 2026, 13.00 Uhr - nicht öffentlich
    Auswärtiges: Tagesordnung der 28. Sitzung des Auswärtigen Ausschusses am 10. Juli 2026, 14:00 Uhr
    Haushalt: 4. Änderungs-/Ergänzungsmitteilung zur 44. Sitzung am 8. Juli 2026

so extraction is deliberately tolerant: committee = text before the first
colon, date = first German-month date after it, time = first "H.MM Uhr" /
"H:MM Uhr", session = the number directly before "Sitzung" (which skips the
Mitteilung counters). Items without a parseable date (e.g. "Parlament:
Tagesordnung komplett") are skipped — the Sitzungskalender skeleton covers
the plenum. external_id is committee+session so every Mitteilung amends the
same event instead of spawning a new one; the linked PDF holds the TOPs
(not fetched — the dated sitting itself is the calendar value).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Iterable

from ..models import RawItem
from .base import SourceConfig, content_hash

MONTHS_DE = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}

DATE_RE = re.compile(
    r"(\d{1,2})\.\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember)\s*(\d{4})", re.IGNORECASE)
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})\s*Uhr")
SESSION_RE = re.compile(r"(\d+)\.\s*(?:\(alt\s*\d+\.?\)\s*)?Sitzung")


def parse_bundestag_to_rss(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    root = ET.fromstring(content)
    for item in root.iter("item"):
        original = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if ":" not in original:
            continue
        committee, _, rest = original.partition(":")
        committee = committee.strip()
        date_m = DATE_RE.search(rest)
        session_m = SESSION_RE.search(rest)
        if not (committee and date_m and session_m):
            continue  # e.g. "Parlament: Tagesordnung komplett" — no date
        day, month_name, year = date_m.groups()
        start = f"{int(year):04d}-{MONTHS_DE[month_name.lower()]:02d}-{int(day):02d}"
        time_m = TIME_RE.search(rest)
        if time_m:
            start += f"T{int(time_m.group(1)):02d}:{time_m.group(2)}:00"
        session = session_m.group(1)
        yield RawItem(
            content_hash=content_hash(cfg.id, original, link),
            source_id=cfg.id,
            # One event per sitting: Ergänzungsmitteilungen share this id and
            # therefore amend the same record instead of duplicating it.
            external_id=f"{committee}|{session}",
            title=f"{committee}: {session}. Sitzung (Bundestag-Ausschuss)",
            url=link,
            date_text=date_m.group(0) + (f", {time_m.group(0)}" if time_m else ""),
            start=start,
            description=original,
            # No verify_strings on purpose: `link` is the Tagesordnung PDF
            # and can never contain the RSS headline, so any would be a
            # guaranteed mismatch — read downstream as possible fabrication.
        )
