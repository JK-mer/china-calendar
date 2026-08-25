"""Bundeswahlleiterin Wahltermine — Tier 2 deterministic scrape (#28).

One table (`Jahr | Datum | Land | Art | Turnus`); the year cell spans its
group, so rows carry 5 cells at a year boundary and 4 inside it. Dates are
"06.09." (day.month., year from the group); rows whose Datum is not yet a
real date ("Herbst", "Frühjahr") are skipped — the page states dates appear
"erst, nachdem er offiziell bekanntgegeben wurde", and once announced the
row's content hash changes and it flows through the gate normally.
"""

from __future__ import annotations

import re
from typing import Iterable

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash

DAY_MONTH = re.compile(r"^(\d{1,2})\.(\d{1,2})\.$")


def parse_wahltermine(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    table = tree.css_first("table")
    if table is None:
        return
    year = None
    for row in table.css("tr"):
        # the year is a <th> on the first row of its group (rowspan)
        th = row.css_first("th")
        if th is not None and th.text(strip=True).isdigit():
            year = int(th.text(strip=True))
        cells = [re.sub(r"\s+", " ", td.text()).strip() for td in row.css("td")]
        if len(cells) != 4 or year is None:
            continue  # header row or malformed
        datum, land, art, _turnus = cells
        m = DAY_MONTH.match(datum)
        if not m:
            continue  # "Herbst" etc. — no official date yet
        day, month = m.groups()
        start = f"{year:04d}-{int(month):02d}-{int(day):02d}"
        title = f"{art} {land} {year}"
        yield RawItem(
            content_hash=content_hash(cfg.id, title, start),
            source_id=cfg.id,
            external_id=f"{land}|{art}|{year}",
            title=title,
            url=cfg.url,
            date_text=f"{datum}{year}",
            start=start,
            location=land,
            description=f"{art} in {land}, Turnus {_turnus} (Bundeswahlleiterin Wahltermine)",
            verify_strings=[land, datum],
        )
