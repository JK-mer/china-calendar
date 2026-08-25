"""IPU Parline parliamentary elections — Tier 2 deterministic scrape (#28).

`https://data.ipu.org/elections/` renders one table over all ~190 member
parliaments; the last column is "Expected date of next elections" ("03 Nov
2026", occasionally a "01 Jun 2026 - 03 Jun 2026" range). One row per
chamber; suspended parliaments carry class `table__row--disabled` and are
skipped. Rows without a parseable expected date (many upper chambers, "-")
yield nothing. Covers parliamentary elections only — presidential races are
the ElectionGuide follow-up in #28. external_id is country+chamber so a
snap-election date change amends the same record with history.
"""

from __future__ import annotations

import re
from typing import Iterable

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash

MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DATE_EN = re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})",
                     re.IGNORECASE)


def _iso(m: re.Match) -> str:
    day, month, year = m.groups()
    return f"{int(year):04d}-{MONTHS_EN[month.lower()[:3]]:02d}-{int(day):02d}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_ipu_elections(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    for row in tree.css("tbody tr"):
        if "table__row--disabled" in (row.attributes.get("class") or ""):
            continue
        cells = row.css("td")
        if len(cells) < 4:
            continue
        country = _clean(cells[0].text())
        chamber = _clean(cells[1].text()).replace("(Suspended)", "").strip()
        expected_text = _clean(cells[-1].text())
        dates = list(DATE_EN.finditer(expected_text))
        if not (country and chamber and dates):
            continue
        start = _iso(dates[0])
        end = _iso(dates[-1]) if len(dates) > 1 else None
        if end == start:
            end = None
        last_election = _clean(cells[-3].text()) if len(cells) >= 5 else ""

        yield RawItem(
            content_hash=content_hash(cfg.id, country, chamber, start, end),
            source_id=cfg.id,
            external_id=f"{country}|{chamber}",
            title=f"{country} parliamentary election ({chamber})",
            url=cfg.url,
            date_text=expected_text,
            start=start,
            end=end,
            description=(f"Expected date of next elections per IPU Parline"
                         + (f"; last election {last_election}" if last_election and last_election != "-" else "")),
            verify_strings=[country, expected_text],
        )
