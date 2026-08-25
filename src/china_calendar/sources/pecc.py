"""PECC event calendar — the Asia-Pacific multilateral cluster (#39).

Two-hop, because the listing cannot be parsed on its own: `/event-calendar/
upcoming-events` names each event and links a detail page, but carries no
JSON-LD and renders its dates as prose ("From November 10, 2026 until
November 12, 2026"). Each detail page carries exactly one schema.org Event
block with ISO dates. `tier2/apa.py` is the JSON-LD precedent; the two-hop
fetch follows `bundesrat_to.py`.

Two traps, both found on the live site 2026-08-11:

- **The JSON-LD time is an artefact.** The 49th ASEAN Summit reads
  `2026-11-10T11:20:00+08:00` — a CMS publication timestamp, not an 11:20
  start. Only the date survives, and items land as all-day spans.
- **The url must be the detail page, not the listing.** `verify_strings` have
  to be findable at the item's own url (house rule), and the JSON-LD lives on
  the detail page only. Pointing url at the listing would guarantee a
  mismatch, which the nightly re-check reads as evidence of fabrication.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..models import RawItem
from .base import SourceConfig, content_hash

BASE = "https://www.pecc.org/"
_EVENT_PATH = "/upcoming-events/event/"


def parse_listing(content: bytes, base: str = BASE) -> list[str]:
    """Absolute detail-page urls, de-duplicated, listing order preserved."""
    tree = HTMLParser(content)
    urls: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if _EVENT_PATH in href:
            url = urljoin(base, href)
            if url not in urls:
                urls.append(url)
    return urls


def parse_event(cfg: SourceConfig, content: bytes, url: str) -> Iterator[RawItem]:
    tree = HTMLParser(content)
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except (ValueError, TypeError):
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict) or entry.get("@type") not in ("Event", "BusinessEvent"):
                continue
            name = (entry.get("name") or "").strip()
            start = (entry.get("startDate") or "").strip()[:10]
            if not name or len(start) != 10:
                continue
            end = (entry.get("endDate") or "").strip()[:10] or None
            location = entry.get("location") or {}
            if isinstance(location, dict):
                location = (location.get("name") or "").strip() or None
            else:
                location = str(location).strip() or None
            yield RawItem(
                content_hash=content_hash(cfg.id, name, start, end or ""),
                source_id=cfg.id,
                external_id=url,
                title=name,
                url=url,
                date_text=f"startDate {start}" + (f" endDate {end}" if end else ""),
                start=start,
                end=end,
                description=None,
                location=location,
                # The date is matched without its time: the stored evidence has
                # to survive the CMS rewriting that timestamp, which it does
                # routinely, while the date itself is the thing we assert.
                verify_strings=[name, start],
            )


def fetch_pecc(cfg: SourceConfig, fetcher) -> Iterable[RawItem]:
    """Two-hop fetch. The listing is fetched fresh — it is the index of what
    exists, and a 304 there would hide an event added since the last run."""
    listing = fetcher.get(cfg.id, cfg.url, force=True,
                          ignore_robots=cfg.ignore_robots)
    for url in parse_listing(listing.content, cfg.url):
        yield from parse_event(cfg, fetcher.fetch_raw(url).content, url)
