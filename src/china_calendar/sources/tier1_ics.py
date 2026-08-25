"""Tier 1 ICS parsing — deterministic, no AI involved (design: Tier 1 stage 1).

Evidence for an ICS item is the SUMMARY text plus the compact DTSTART value;
both are literally present in the fetched file, so the verification pass can
string-match them on a re-fetch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from icalendar import Calendar

from ..models import RawItem
from .base import SourceConfig, content_hash


def _iso(value) -> tuple[str, bool]:
    """(iso_string, is_all_day) from an icalendar DTSTART/DTEND value."""
    dt = value.dt
    if isinstance(dt, datetime):
        return dt.isoformat(), False
    if isinstance(dt, date):
        return dt.isoformat(), True
    raise ValueError(f"unsupported ical date value {value!r}")


def parse_ics(cfg: SourceConfig, content: bytes) -> Iterable[RawItem]:
    calendar = Calendar.from_ical(content)
    for component in calendar.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        if not summary:
            continue
        start_iso, all_day = _iso(component["DTSTART"])
        end_iso = None
        if component.get("DTEND") is not None:
            end_iso, _ = _iso(component["DTEND"])
            if all_day:
                # DTEND is exclusive for all-day events; our end is inclusive.
                end_iso = (date.fromisoformat(end_iso[:10]) - timedelta(days=1)).isoformat()
                if end_iso == start_iso:
                    end_iso = None
        location = str(component.get("LOCATION", "")).strip() or None
        description = str(component.get("DESCRIPTION", "")).strip() or None
        compact_start = start_iso[:10].replace("-", "")
        yield RawItem(
            content_hash=content_hash(cfg.id, summary, start_iso, end_iso),
            source_id=cfg.id,
            external_id=str(component.get("UID", "")).strip() or None,
            title=f"{cfg.title_prefix}: {summary}" if cfg.title_prefix else summary,
            url=cfg.url,
            date_text=f"DTSTART {compact_start}",
            start=start_iso,
            end=end_iso,
            description=description,
            location=location,
            verify_strings=[summary, compact_start],
        )
