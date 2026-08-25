"""IFES ElectionGuide homepage — presidential races only (Tier 2, #28).

The homepage renders upcoming (and recent) elections as table rows: a
<strong> date ("Aug 13 2026") with a <small> status marker ("(d)" declared /
"(t)" tentative), a link to the election ("Zambian Presidency") and one to
the country. This source deliberately yields ONLY elections whose name
contains "presiden" — parliaments come from IPU Parline (`ipu-elections`)
and taking both here would duplicate them. The election id in the URL is
the stable identity, so a moved date amends the same record. Coverage is
the homepage window, which rolls forward; the daily sweep keeps up.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash

MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DATE_EN = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+(\d{1,2}),?\s+(\d{4})",
                     re.IGNORECASE)


def parse_electionguide(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    seen: set[str] = set()
    for row in tree.css("tbody tr"):
        election_link = row.css_first('a[href^="/elections/id/"]')
        country_link = row.css_first('a[href^="/countries/id/"]')
        date_node = row.css_first("strong")
        if not (election_link and country_link and date_node):
            continue
        name = election_link.text(strip=True)
        if "presiden" not in name.lower():
            continue
        href = election_link.attributes.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        country = country_link.text(strip=True)
        date_text = date_node.text(strip=True)
        m = DATE_EN.search(date_text)
        if not m:
            continue
        month, day, y = m.groups()
        start = f"{int(y):04d}-{MONTHS_EN[month.lower()[:3]]:02d}-{int(day):02d}"
        marker = row.css_first("small")
        status_note = marker.text(strip=True) if marker else ""

        yield RawItem(
            content_hash=content_hash(cfg.id, href, start),
            source_id=cfg.id,
            external_id=href,
            title=f"{name} — {country}",
            url=urljoin("https://www.electionguide.org/", href),
            date_text=f"{date_text} {status_note}".strip(),
            start=start,
            location=country,
            description=f"Presidential election per IFES ElectionGuide"
                        + (f"; date status {status_note}" if status_note else ""),
            verify_strings=[name, date_text],
        )
