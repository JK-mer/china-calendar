"""APA (Asien-Pazifik-Ausschuss) events — Tier 2 via embedded schema.org
JSON-LD (BusinessEvent), which carries ISO startDate/endDate directly.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash


def _strip_html(text: str) -> str:
    """JSON-LD descriptions arrive with embedded markup (img tags etc.)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def parse_apa(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except (ValueError, TypeError):
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict) or entry.get("@type") not in ("BusinessEvent", "Event"):
                continue
            name = (entry.get("name") or "").strip()
            start = (entry.get("startDate") or "").strip() or None
            if not name or not start:
                continue
            end = (entry.get("endDate") or "").strip() or None
            location = entry.get("location") or {}
            if isinstance(location, dict):
                location = (location.get("name") or "").strip() or None
            url = (entry.get("url") or "").strip() or cfg.url

            verify = [name, start]
            yield RawItem(
                content_hash=content_hash(cfg.id, name, start, end),
                source_id=cfg.id,
                external_id=url if url != cfg.url else None,
                title=name,
                url=cfg.url,  # the listing page is where the JSON-LD lives
                date_text=f"startDate {start}" + (f" endDate {end}" if end else ""),
                start=start,
                end=end,
                description=_strip_html(entry.get("description") or "")[:500] or None,
                location=location,
                verify_strings=verify,
            )
