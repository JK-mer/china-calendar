"""BDI Veranstaltungen — Tier 2 deterministic scrape.

Page structure (2026-08): li.bdi-event blocks with h3.bdi-event__title > a
(text carries soft hyphens — strip U+00AD), span.bdi-event__date--begin /
--end holding DD.MM.YYYY, p.bdi-event__location and
div.bdi-event__description inside the collapsed more-info section.
"""

from __future__ import annotations

import re
from typing import Iterable

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash

NUMERIC_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _iso(text: str | None) -> str | None:
    m = NUMERIC_DATE.search(text or "")
    if not m:
        return None
    day, month, year = m.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("­", "")).strip()


def parse_bdi(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    for node in tree.css("li.bdi-event"):
        link = node.css_first(".bdi-event__title a")
        if link is None:
            continue
        title = _clean(link.text())
        url = link.attributes.get("href") or cfg.url
        begin_node = node.css_first(".bdi-event__date--begin")
        end_node = node.css_first(".bdi-event__date--end")
        begin_text = begin_node.text(strip=True) if begin_node else ""
        end_text = end_node.text(strip=True) if end_node else ""
        start, end = _iso(begin_text), _iso(end_text)
        if end == start:
            end = None
        location_node = node.css_first(".bdi-event__location")
        description_node = node.css_first(".bdi-event__description")

        # Title only (#76), same defect as OAV: begin_text comes from the
        # LISTING while url points at the DETAIL page, which does not carry
        # that date string. Verified live 2026-08-11 — "Tax Forum Berlin"
        # matches, "13.04.2027" does not.
        verify = [title]

        yield RawItem(
            content_hash=content_hash(cfg.id, title, start or begin_text, end),
            source_id=cfg.id,
            external_id=url if url != cfg.url else None,
            title=title,
            url=url,
            date_text=" – ".join(filter(None, [begin_text, end_text])) or None,
            start=start,
            end=end,
            description=(_clean(description_node.text())[:500]
                         if description_node else None),
            location=(location_node.text(strip=True) if location_node else None),
            verify_strings=verify,
        )
