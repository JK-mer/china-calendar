"""Source registry: sources.yaml → SourceConfig, and the parser protocol."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol
from urllib.parse import urlsplit

import yaml

from ..models import RawItem

# Repo root in dev; overridden in the container where the package is installed
# into site-packages and the registry is baked at /app/sources.yaml.
SOURCES_FILE = Path(os.environ.get(
    "PC_SOURCES_FILE", Path(__file__).resolve().parents[3] / "sources.yaml"
))


@dataclass
class SourceConfig:
    id: str
    tier: int
    kind: str  # ics | rss | html:<parser-name>
    url: str
    enabled: bool = True
    auto_accept: bool = False  # only for trusted (source, item-type) pairs
    title_prefix: str | None = None
    sectors: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    timezone: str = "Europe/Berlin"
    # Agenda-style sources: an accepted item does not create a new event but
    # enriches the existing skeleton event of this actor on the item's date
    # (corroborating source + note line). Falls back to a standalone event.
    enrich_actor: str | None = None
    # Skeleton sources whose bare sittings are structure, not content (#74):
    # records land calendar-off and are switched on by the enrichment branch
    # when an accepted agenda item earns them a place. Bundesrat sittings are
    # the case — 85 of 88 TOPs auto-reject here, so most never carry a China
    # item, and a shared calendar full of contextless Plenarsitzungen buries
    # the signal. Bundestag sitting weeks deliberately do NOT use this: they
    # structure everything else and the profile calls them always relevant.
    calendar_default_off: bool = False
    # The single calendar year this source's file covers (#75). Annual files
    # do not fail when they go stale — they keep serving last year's calendar,
    # fetch fine, parse fine, and report success daily while nothing
    # forward-dated ever arrives again. Declaring the year is what makes that
    # detectable; a generic "no future items" counter cannot, because an agenda
    # source between sessions is legitimately all-past for weeks (#18).
    covers_year: int | None = None
    # This source produces CONTAINERS, not contents (#69 review): sitting
    # calendars whose records agenda items belong to. Only skeletons are
    # enrichment targets — an agenda item can never be the home for another
    # agenda item, which is a fact about the data rather than a tie-break.
    # Without it, "which event owns 16 September?" had two legal answers (the
    # EP part-session and SOTEU) and was resolved by uid alphabetical order.
    skeleton: bool = False
    # Optional cheap keyword gate BEFORE the classifier: items whose
    # title+description match none of these (case-insensitive substrings) are
    # auto-rejected without an LLM call. For very-high-volume sources where
    # even flash calls add up; leave unset to classify everything.
    prefilter_keywords: list[str] = field(default_factory=list)
    # Reachability-probe target for disabled sources whose fetch url is not
    # (yet) known — e.g. a landing page while the direct file URL is TBD.
    probe_url: str | None = None
    # Published subscription endpoints (public ICS URLs) are meant for
    # programmatic access even when the host's robots.txt targets crawlers —
    # e.g. calendar.google.com disallows /calendar/ical/ yet every calendar
    # client fetches it. Only for such endpoints; never for scraped HTML.
    ignore_robots: bool = False
    notes: str | None = None


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    raw = yaml.safe_load((path or SOURCES_FILE).read_text(encoding="utf-8"))
    return [SourceConfig(**entry) for entry in raw]


def robots_exempt(url: str | None, path: Path | None = None) -> bool:
    """Whether a stored URL belongs to a host we already fetch with
    `ignore_robots`. The verify and probe paths get a bare URL with no
    source_id, so without this they refuse URLs the sweep fetches nightly.
    Matched on host: a source's items share its host, not its path.
    """
    if not url:
        return False
    host = urlsplit(url).netloc.lower()
    if not host:
        return False
    return any(
        cfg.ignore_robots and urlsplit(cfg.url).netloc.lower() == host
        for cfg in load_sources(path)
    )


def source_by_id(source_id: str, path: Path | None = None) -> SourceConfig:
    for cfg in load_sources(path):
        if cfg.id == source_id:
            return cfg
    raise KeyError(f"unknown source {source_id!r}")


def content_hash(source_id: str, *parts: str | None) -> str:
    """Stable identity of a raw item. An item whose content changes gets a new
    hash and therefore a fresh gate decision (design: decision ledger)."""
    h = hashlib.sha256()
    h.update(source_id.encode())
    for part in parts:
        h.update(b"\x1f")
        h.update((part or "").encode())
    return h.hexdigest()[:24]


class Parser(Protocol):
    def __call__(self, cfg: SourceConfig, content: bytes) -> Iterable[RawItem]: ...
