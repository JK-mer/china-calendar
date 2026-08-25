"""NPC Observer RSS — deterministic NPCSC session dates (#37).

https://npcobserver.com/feed/ is the authoritative English tracker of China's
legislature (WordPress, full post body in content:encoded). Only the
"NPCSC Session Watch" posts are parsed: they announce a scheduled session in
a rigid sentence —

    China's top legislature, the 14th NPC Standing Committee (NPCSC), will
    convene for its 23rd session from June 23 to 26, the Council of
    Chairpersons decided on Tuesday, June 16, 2026.

so session number and date range extract by regex; anything without an
explicit range is skipped (the monthly "NPC Calendar" posts are prose without
day-level dates — month-level projection lives in seeds/glossary, not here).
The announcement year is not in the sentence; it is taken from pubDate, with
a December→January rollover guard. external_id = npcsc|{n} so a re-announced
or corrected session amends the same record.
"""

from __future__ import annotations

import html as html_lib
import re
import xml.etree.ElementTree as ET
from typing import Iterable

from ..models import RawItem
from .base import SourceConfig, content_hash

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

MONTHS_EN = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
_MONTH_RE = "|".join(m.capitalize() for m in MONTHS_EN)

SESSION_RE = re.compile(
    rf"will convene for its (\d+)\s*(?:st|nd|rd|th)\s+session"
    rf" from ({_MONTH_RE}) (\d{{1,2}}) to (?:({_MONTH_RE}) )?(\d{{1,2}})")
PUBDATE_YEAR_RE = re.compile(r"\b(\d{4})\b")
PUBDATE_MONTH_RE = re.compile(rf"\b({'|'.join(m[:3].capitalize() for m in MONTHS_EN)})\b")


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _plain_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def parse_npcobserver(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    root = ET.fromstring(content)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if "session watch" not in title.lower():
            continue
        link = (item.findtext("link") or "").strip()
        body = item.findtext(CONTENT_NS) or item.findtext("description") or ""
        text = _plain_text(body)
        m = SESSION_RE.search(text)
        if not m:
            continue  # post without an announced date range — nothing verifiable
        session, start_month, start_day, end_month, end_day = m.groups()

        pubdate = item.findtext("pubDate") or ""
        year_m = PUBDATE_YEAR_RE.search(pubdate)
        month_m = PUBDATE_MONTH_RE.search(pubdate)
        if not year_m:
            continue  # no year anywhere — cannot build an ISO date honestly
        year = int(year_m.group(1))
        start_mnum = MONTHS_EN[start_month.lower()]
        if month_m:
            pub_mnum = [m[:3].capitalize() for m in MONTHS_EN].index(month_m.group(1)) + 1
            if start_mnum < pub_mnum - 6:  # announced in December for January
                year += 1
        end_mnum = MONTHS_EN[(end_month or start_month).lower()]
        end_year = year + 1 if end_mnum < start_mnum else year

        start = f"{year:04d}-{start_mnum:02d}-{int(start_day):02d}"
        end = f"{end_year:04d}-{end_mnum:02d}-{int(end_day):02d}"
        # the literal fragment as it appears in the post body (plain text,
        # no markup inside) — this is what re-fetches string-match against
        evidence = m.group(0)[m.group(0).index("session from"):]
        yield RawItem(
            content_hash=content_hash(cfg.id, title, link),
            source_id=cfg.id,
            external_id=f"npcsc|{session}",
            title=f"NPCSC: {_ordinal(int(session))} session",
            url=link,
            date_text=m.group(0),
            start=start,
            end=end,
            description=text[:280],
            verify_strings=[evidence],
        )
