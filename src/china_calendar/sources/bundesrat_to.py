"""Bundesrat Tagesordnungen — the first high-volume gated source (issue #11).

Two-hop fetch: the landing page names the current session's Tagesordnung
("10. Juli 2026 | 1067. Plenarsitzung" + link); the TO page carries the TOPs
(li.top-item with TOP number and Drucksache title). Each TOP becomes a raw
item for the selection gate; an accepted TOP does not become a new event but
ENRICHES the skeleton Plenarsitzung event for that date (gate.accept_item,
via cfg.enrich_actor).
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator

from selectolax.parser import HTMLParser

from ..models import RawItem
from .base import SourceConfig, content_hash
from .dates import MONTHS

BASE = "https://www.bundesrat.de/"

_SESSION_RE = re.compile(
    rf"(\d{{1,2}})\.\s*({'|'.join(m for m in MONTHS if m[0].isalpha())})\s+(\d{{4}})\s*\|\s*(\d+)\.\s*Plenarsitzung",
    re.IGNORECASE,
)
_TO_LINK_RE = re.compile(r'href="((?:/)?SharedDocs/TO/(\d+)/tagesordnung-\d+\.html)"')


def parse_landing(content: bytes) -> list[tuple[str, str, str]]:
    """(session_number, session_date_iso, absolute_to_url) for every
    Tagesordnung the landing page links with a dated announcement."""
    html = content.decode("utf-8", errors="replace")
    dates: dict[str, str] = {}
    from datetime import date as date_cls

    for day, month, year, number in _SESSION_RE.findall(html):
        dates[number] = date_cls(int(year), MONTHS[month.lower()], int(day)).isoformat()
    sessions = []
    for link, number in set(_TO_LINK_RE.findall(html)):
        if number in dates:
            sessions.append((number, dates[number], BASE + link.lstrip("/")))
    return sessions


def parse_to_page(cfg: SourceConfig, session: str, session_date: str,
                  content: bytes) -> Iterator[RawItem]:
    tree = HTMLParser(content)
    seen: set[str] = set()
    for node in tree.css("li.top-item"):
        number_node = node.css_first("h2.top-number")
        title_node = node.css_first(".top-header-content h2 a") or node.css_first(".top-header-content h2")
        if number_node is None or title_node is None:
            continue
        top = number_node.text(strip=True)  # "TOP 86"
        if top in seen:
            continue  # the page renders numeric and thematic views of the same TOPs
        seen.add(top)
        title = title_node.text(strip=True)
        if not title:
            continue
        top_number = top.replace("TOP", "").strip()
        url = f"{BASE}SharedDocs/TO/{session}/tagesordnung-{session}.html?topNr={top_number}#top-{top_number}"
        yield RawItem(
            content_hash=content_hash(cfg.id, session, top, title),
            source_id=cfg.id,
            external_id=f"{session}/{top}",
            title=f"{top} ({session}. Sitzung): {title}",
            url=url,
            date_text=f"{session}. Plenarsitzung, {session_date}",
            start=session_date,
            end=None,
            description=None,
            location="Bundesrat",
            verify_strings=[title],
        )


def fetch_bundesrat_to(cfg: SourceConfig, fetcher) -> Iterable[RawItem]:
    """Two-hop fetch. The landing page is always fetched fresh (it is small,
    and a 304 there would hide TOPs added to an already-linked TO)."""
    landing = fetcher.get(cfg.id, cfg.url, force=True)
    for session, session_date, url in parse_landing(landing.content):
        to_page = fetcher.fetch_raw(url)
        yield from parse_to_page(cfg, session, session_date, to_page.content)
