"""EP Open Data API — plenary KEY DEBATE items (#64).

The European Parliament's own machine-readable calendar, and the only route
to agenda-level detail: `ep-plenary-ical` emits part-session blocks and
structurally cannot carry what happens inside one. SOTEU 2026 was missing for
exactly that reason (#63).

Scope is deliberately narrow — **KEY DEBATE items only**. Measured on the live
September 2026 part-session, 2026-08-11: 65 agenda items across four sitting
days, of which exactly one is a KEY DEBATE (the State of the Union). That is
the ratio this parser exists for. Ordinary debates and votes belong on the
part-session record as enrichment rather than as standalone events, and that
needs the cross-source duplicate question (#68) settled first.

Shape of the data, all verified live:

- `/meetings?year=YYYY` lists sitting DAYS (`MTG-PL-2026-09-16`), not
  part-sessions. Holds 2020-2026; `year=2027` returns 204.
- `/meetings/{id}/foreseen-activities` is the draft agenda, and the EP
  publishes **one part-session at a time** — every sitting beyond the next one
  returns 204. Probing far ahead is therefore free but pointless, hence
  HORIZON_DAYS.
- A KEY DEBATE is a `MEETING_PART` whose `agendaLabel.en` says so; it carries
  the start time and names its real agenda items in `consists_of`. The child
  carries the label, the parent carries the clock.

Three traps, each of which silently produces garbage rather than an error:

- **The API content-negotiates to RDF/XML** and the fetcher sends no `Accept`
  header, so `?format=application%2Fld%2Bjson` is load-bearing in every URL
  including the one stored as evidence.
- **`activity_label` key order is unstable** across the language map, so no
  evidence string may span it. `"en":"..."` is contiguous and safe.
- **Item ids embed the agenda point** (`-OJ-ITM-D-63`) and renumber as the
  draft firms up, so they must not be the external_id — that would mint a
  second uid and orphan the first, the failure the feed-uid convention exists
  to prevent.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Iterable, Iterator

from ..models import RawItem
from .base import SourceConfig, content_hash

API = "https://data.europarl.europa.eu/api/v2"
JSON_LD = "format=application%2Fld%2Bjson"

# The next part-session is the only one with a published agenda; 60 days is
# generous cover for that with room for the EP publishing earlier than usual.
HORIZON_DAYS = 60

_KEY_DEBATE = "KEY DEBATE"


def meetings_url(year: int) -> str:
    return f"{API}/meetings?year={year}&limit=400&{JSON_LD}"


def agenda_url(sitting_id: str) -> str:
    return f"{API}/meetings/{sitting_id}/foreseen-activities?{JSON_LD}"


def _load(content: bytes) -> list[dict]:
    """The API answers 204 with an empty body for a sitting with no published
    agenda, which is the normal case rather than an error."""
    text = (content or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("data") if isinstance(data, dict) else data
    return [i for i in (items or []) if isinstance(i, dict)]


def _en(entry: dict, key: str) -> str:
    value = entry.get(key) or {}
    if isinstance(value, dict):
        return (value.get("en") or "").strip()
    return str(value).strip()


def sittings_in_window(content: bytes, today: date,
                       horizon_days: int = HORIZON_DAYS) -> list[str]:
    """Sitting ids from today to today+horizon, soonest first."""
    last = (today + timedelta(days=horizon_days)).isoformat()
    out = []
    for entry in _load(content):
        day = (entry.get("activity_date") or "")[:10]
        if entry.get("activity_id") and today.isoformat() <= day <= last:
            out.append((day, entry["activity_id"]))
    return [sid for _, sid in sorted(out)]


def _slug(label: str) -> str:
    """Stable external_id component. The agenda-point number is deliberately
    excluded — it renumbers as the draft firms up."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def parse_agenda(cfg: SourceConfig, content: bytes, sitting_id: str,
                 url: str | None = None) -> Iterator[RawItem]:
    entries = _load(content)
    by_id = {e.get("activity_id"): e for e in entries}
    source_url = url or agenda_url(sitting_id)

    for entry in entries:
        if _KEY_DEBATE not in _en(entry, "agendaLabel").upper():
            continue
        start = (entry.get("activity_start_date") or "")[:19] or None
        end = (entry.get("activity_end_date") or "")[:19] or None
        for ref in entry.get("consists_of") or []:
            child = by_id.get(str(ref).split("/")[-1])
            if child is None:
                continue
            label = _en(child, "activity_label")
            if not label:
                continue
            day = (child.get("activity_date") or "")[:10]
            yield RawItem(
                content_hash=content_hash(cfg.id, sitting_id, label, start or day),
                # Sitting + label, never the -OJ-ITM-D-nn id: that renumbers.
                external_id=f"{sitting_id}/{_slug(label)}",
                source_id=cfg.id,
                title=f"EP plenary key debate: {label}",
                url=source_url,
                date_text=f"{_KEY_DEBATE} {sitting_id} {start or day}",
                start=start or day,
                end=end,
                description=None,
                location="Strasbourg",
                # Contiguous and language-order independent; spanning the
                # label map would break on the next fetch.
                verify_strings=[f'"en":"{label}"'],
            )


# Item types worth carrying as enrichment. MEETING_PART and the TF-HHMM time
# frames are structural containers, not agenda items — they name no business.
ENRICHABLE_TYPES = ("PLENARY_DEBATE", "PLENARY_VOTE")


def parse_agenda_items(cfg: SourceConfig, content: bytes, sitting_id: str,
                       url: str | None = None) -> Iterator[RawItem]:
    """Ordinary debates and votes, for enrichment onto the part-session (#69).

    The bulk of an agenda: 65 items across the September 2026 part-session,
    of which 16 votes and 5 debates fell on the Wednesday alone. These are not
    standalone events — they are what happens inside a sitting — so they land
    as note lines plus a corroborating source on the part-session record, the
    same shape as Bundesrat TOPs.

    KEY DEBATE children are deliberately excluded here: they are emitted as
    their own timed events by `parse_agenda`, and carrying them twice would
    put SOTEU both in the calendar and in the part-session's note.
    """
    entries = _load(content)
    source_url = url or agenda_url(sitting_id)

    key_debate_children = set()
    for entry in entries:
        if _KEY_DEBATE in _en(entry, "agendaLabel").upper():
            key_debate_children.update(
                str(ref).split("/")[-1] for ref in entry.get("consists_of") or [])

    for entry in entries:
        kind = str(entry.get("had_activity_type") or "").rsplit("/", 1)[-1]
        if kind not in ENRICHABLE_TYPES:
            continue
        if entry.get("activity_id") in key_debate_children:
            continue
        label = _en(entry, "activity_label")
        day = (entry.get("activity_date") or "")[:10]
        if not label or not day:
            continue
        pretty = "vote" if kind == "PLENARY_VOTE" else "debate"
        yield RawItem(
            content_hash=content_hash(cfg.id, sitting_id, label, day),
            external_id=f"{sitting_id}/{_slug(label)}",
            source_id=cfg.id,
            title=f"EP plenary {pretty}: {label}",
            url=source_url,
            date_text=f"{sitting_id} {day}",
            start=day,
            end=day,
            description=None,
            location="Strasbourg",
            verify_strings=[f'"en":"{label}"'],
        )


def _walk(cfg: SourceConfig, fetcher, today: date | None, parser) -> Iterable[RawItem]:
    """Sittings for this year and next, then the agenda of each sitting inside
    the horizon. Next year is fetched so that the year the EP loads it, the
    sittings arrive on a sweep instead of needing a human to notice."""
    from ..fetch import NoContent

    today = today or date.today()
    sittings: list[str] = []
    for year in (today.year, today.year + 1):
        try:
            result = fetcher.get(cfg.id, meetings_url(year), force=True,
                                 ignore_robots=cfg.ignore_robots)
        except NoContent:
            continue  # the EP has not loaded that year yet — the normal case
        sittings.extend(sittings_in_window(result.content, today))

    for sitting_id in sittings:
        url = agenda_url(sitting_id)
        try:
            content = fetcher.fetch_raw(url).content
        except NoContent:
            continue  # no draft agenda published for this sitting yet
        yield from parser(cfg, content, sitting_id, url)


def fetch_ep_opendata(cfg: SourceConfig, fetcher, today: date | None = None) -> Iterable[RawItem]:
    """KEY DEBATE items, as standalone timed events (#64)."""
    return _walk(cfg, fetcher, today, parse_agenda)


def fetch_ep_opendata_items(cfg: SourceConfig, fetcher, today: date | None = None) -> Iterable[RawItem]:
    """Ordinary debates and votes, for enrichment onto the part-session (#69).

    A separate source rather than a flag on the first one, because the two
    dispositions are configured per source: this one carries `enrich_actor`
    so accepted items become note lines on the skeleton, while KEY DEBATEs
    must stay standalone. The extra fetches are the same handful of URLs.
    """
    return _walk(cfg, fetcher, today, parse_agenda_items)
