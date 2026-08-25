"""Bundespräsident Terminkalender — Tier 2 deterministic scrape.

The Termine node redirects to a Terminsuche result page (2026-08 structure):
div.c-teaser-search-result blocks with h3.c-teaser-search-result__headline,
span.c-topline__element.is-type (location, trailing comma),
span.c-topline__element.is-date ("26. Juli 2026") and a teaser text. The
detail URL path also encodes the date (…/2026/09/260904-Buergerfest.html) —
used as a cross-check-free fallback when the date span is missing. Forward-
looking (state visits, receptions with military honours, accreditations),
which is what makes it the state-visit source (user request 2026-08-03).
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ...models import RawItem
from ..base import SourceConfig, content_hash

MONTHS_DE = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}
DATE_DE = re.compile(
    r"(\d{1,2})\.\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember)\s*(\d{4})", re.IGNORECASE)
PATH_DATE = re.compile(r"/(\d{4})/\d{2}/(\d{2})(\d{2})(\d{2})-")


def _iso_from_text(text: str) -> str | None:
    m = DATE_DE.search(text)
    if not m:
        return None
    day, month, year = m.groups()
    return f"{int(year):04d}-{MONTHS_DE[month.lower()]:02d}-{int(day):02d}"


def _iso_from_path(path: str) -> str | None:
    m = PATH_DATE.search(path)
    if not m:
        return None
    year, yy, mm, dd = m.groups()
    if year[2:] != yy:  # century sanity: /2026/09/260904- → 26 == 26
        return None
    return f"{year}-{mm}-{dd}"


def parse_bundespraesident(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    tree = HTMLParser(content)
    # the page sets <base href="https://www.bundespraesident.de/"> — relative
    # hrefs resolve against it, not against the (redirected) page URL
    base_node = tree.css_first("base[href]")
    base = (base_node.attributes.get("href") if base_node else None) or cfg.url
    seen: set[str] = set()
    for block in tree.css("div.c-teaser-search-result"):
        heading = block.css_first("h3.c-teaser-search-result__headline")
        link = block.css_first("a.c-teaser-search-result__main-link") \
            or block.css_first("a[href]")
        if heading is None or link is None:
            continue
        href = (link.attributes.get("href") or "").split("?")[0]
        if not href or href in seen:
            continue
        seen.add(href)
        title = re.sub(r"\s+", " ", heading.text()).strip()
        url = urljoin(base, href)
        date_node = block.css_first("span.c-topline__element.is-date")
        date_text = re.sub(r"\s+", " ", date_node.text()).strip() if date_node else ""
        # the aural duplicate doubles the string; one DATE_DE match is enough
        start = _iso_from_text(date_text) or _iso_from_path(href)
        location_node = block.css_first("span.c-topline__element.is-type")
        location = (location_node.text(strip=True).rstrip(", ") or None
                    if location_node else None)
        text_node = block.css_first(".c-teaser-search-result__text")
        description = (re.sub(r"\s+", " ", text_node.text()).strip()[:500] or None
                       if text_node else None)

        verify = [title]
        if date_text:
            verify.append(DATE_DE.search(date_text).group(0)
                          if DATE_DE.search(date_text) else date_text)

        yield RawItem(
            content_hash=content_hash(cfg.id, title, start or date_text),
            source_id=cfg.id,
            external_id=href,
            title=title,
            url=url,
            date_text=date_text or None,
            start=start,
            description=description,
            location=location,
            verify_strings=verify,
        )
