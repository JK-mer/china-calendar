"""OAV (Ostasiatischer Verein) Veranstaltungen — Tier 2 deterministic scrape.

Page structure (2026-08): div.news-item.type-event-item cards with an h2
title, p.news-item__date ("29. Oktober 2026, Südkorea"), teaser paragraphs
(which may carry the full range: "vom 29. bis 31. Oktober 2026") and a
detail link.
"""

from __future__ import annotations

from typing import Iterable

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash
from ..dates import parse_date_range


def parse_oav(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    for node in tree.css("div.news-item.type-event-item"):
        heading = node.css_first("h2, h3")
        if heading is None:
            continue
        title = heading.text(strip=True)
        date_node = node.css_first("p.news-item__date")
        date_text = date_node.text(strip=True) if date_node else ""
        teaser = " ".join(
            p.text(strip=True) for p in node.css("p")
            if "news-item__date" not in (p.attributes.get("class") or "")
        ).strip() or None
        link = node.css_first("a.news-item__link") or node.css_first("a[href]")
        url = link.attributes.get("href") if link else cfg.url

        # Prefer the range from the teaser (more precise), fall back to the
        # date line. Both are deterministic; unparseable stays date-text-only.
        start, end = parse_date_range(teaser or "")
        if start is None:
            start, end = parse_date_range(date_text)
        location = date_text.split(",", 1)[1].strip() if "," in date_text else None

        # Title only (#76). `date_text` is lifted from the LISTING, while `url`
        # points at the DETAIL page, which renders the date differently or not
        # at all — so the date string was unmatchable by construction. Verified
        # live 2026-08-11: title matches, date does not. That is not cosmetic:
        # recheck_unverified counts "fetched, evidence absent" as the
        # fabrication signal, so these drifted toward rumored nightly for a
        # parser defect. House rule in Sources.md — verify_strings must be
        # findable at the item's own url.
        verify = [title]

        yield RawItem(
            content_hash=content_hash(cfg.id, title, start or date_text, end),
            source_id=cfg.id,
            external_id=url if url != cfg.url else None,
            title=title,
            url=url,
            date_text=date_text or None,
            start=start,
            end=end,
            description=teaser,
            location=location,
            verify_strings=verify,
        )
