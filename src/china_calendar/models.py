"""Core data model.

Invariant I1: the model never originates a date. Every non-manual date is
traceable to a source (url + evidence + retrieved_at) and verified by literal
string matching in code, not by model judgement.

Statuses:
    confirmed   official announcement, exact date
    scheduled   on a published forward calendar, subject to change
    rumored     press or single-source report, no official confirmation
    projected   inferred from historical pattern, no announcement exists
    unverified  asserted in conversation with no fetched source; queued for
                the next sweep to promote or demote

Tiers describe provenance, not importance:
    0 manual · 1 feeds/registers · 2 structured HTML · 3 researched
"""

from __future__ import annotations

import enum
import re
import unicodedata
from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Status(str, enum.Enum):
    confirmed = "confirmed"
    scheduled = "scheduled"
    rumored = "rumored"
    projected = "projected"
    unverified = "unverified"


class Provenance(str, enum.Enum):
    manual = "manual"
    feed = "feed"
    scrape = "scrape"
    research = "research"


# Statuses that sync to the calendar without explicit authorization.
AUTO_SYNC_STATUSES = {Status.confirmed, Status.scheduled}

# Title prefixes for calendar projection.
STATUS_PREFIX = {
    Status.confirmed: "(Confirmed)",
    Status.scheduled: "(Scheduled)",
    Status.rumored: "(Rumored)",
    Status.projected: "(Projected)",
    Status.unverified: "(Unverified)",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


def make_uid(slug_source: str, year: int | str) -> str:
    return f"pc-{slugify(slug_source)}-{year}"


class SourceRef(BaseModel):
    url: str | None = None
    evidence: str  # the sentence the date came from; original language, always
    # Literal strings that must appear in a re-fetch of url for the date to
    # count as verified. Set by parsers; checked by verify.py in code.
    verify_strings: list[str] = Field(default_factory=list)
    retrieved_at: str | None = None
    verified_at: str | None = None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ts: str = Field(default_factory=utcnow)
    field: str
    from_: object = Field(default=None, alias="from")
    to: object = None
    actor: str
    reason: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    uid: str
    title_de: str | None = None
    title_en: str | None = None
    title_zh: str | None = None
    start: str  # ISO 8601 date (all_day) or datetime
    end: str | None = None  # inclusive end date for all_day multi-day windows
    all_day: bool = True
    timezone: str = "Europe/Berlin"
    tier: int = Field(ge=0, le=3)
    status: Status
    provenance: Provenance
    sectors: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    location: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    note: str | None = None
    removed: bool = False
    removed_reason: str | None = None
    # Calendar on/off (#70): None = default policy (everything syncs, labelled
    # by STATUS_PREFIX), True/False = per-event override in either direction.
    # Same tri-state as `remind`, and for the same reason — as a plain bool
    # this could not express "the human turned it off" distinguishably from
    # "never touched". Hiding a projection does not make the calendar more
    # rigorous, it makes it silently incomplete.
    sync_authorized: bool | None = None
    # Calendar reminder (#78, was #8): None = no alarm — the status policy it
    # once deferred to is now empty. Only True produces one, and only the
    # dashboard toggle sets it; no add path (CLI, MCP, feed, adopt) touches it.
    remind: bool | None = None
    calendar_uid: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _require_title(self) -> "Event":
        if not (self.title_de or self.title_en or self.title_zh):
            raise ValueError("event needs at least one title")
        return self

    @field_validator("start", "end")
    @classmethod
    def _valid_iso(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            date.fromisoformat(v)
        except ValueError:
            datetime.fromisoformat(v)  # raises if neither parses
        return v

    @field_validator("uid")
    @classmethod
    def _uid_namespace(cls, v: str) -> str:
        if not re.fullmatch(r"pc-[a-z0-9-]+", v):
            raise ValueError(f"uid {v!r} not in pc-<slug>[-<year>] namespace")
        return v

    def title(self) -> str:
        return self.title_en or self.title_de or self.title_zh or self.uid

    def start_date(self) -> date:
        try:
            return date.fromisoformat(self.start)
        except ValueError:
            return datetime.fromisoformat(self.start).date()

    def end_date(self) -> date:
        if self.end is None:
            return self.start_date()
        try:
            return date.fromisoformat(self.end)
        except ValueError:
            return datetime.fromisoformat(self.end).date()


class RawItem(BaseModel):
    """A parsed item from a Tier 1/2 source, before/after the selection gate."""

    content_hash: str  # sha256 of source_id + normalized payload
    source_id: str
    external_id: str | None = None  # stable id from the feed (e.g. ICS UID)
    fetched_at: str = Field(default_factory=utcnow)
    title: str
    url: str | None = None
    date_text: str | None = None  # raw date string(s) as found
    start: str | None = None  # ISO, if deterministically parseable
    end: str | None = None
    description: str | None = None
    location: str | None = None
    verify_strings: list[str] = Field(default_factory=list)
    route: str | None = None  # auto_accept | triage | auto_reject, set by gate
    classifier: dict | None = None  # {relevant, confidence, reason, model}
    event_uid: str | None = None  # set once accepted into the store
    # Cross-source duplicate SUGGESTION (#68): {uid, title, score, why, ...}.
    # A proposal for a human, never applied automatically — a wrong merge
    # fuses two real events and is close to undetectable afterwards.
    duplicate_of: dict | None = None


class Decision(BaseModel):
    """Selection-gate ledger entry. One file per content hash; an item rejected
    once never resurfaces unless its content changes."""

    content_hash: str
    source_id: str
    title: str
    decision: str  # accept | reject | defer
    reason: str | None = None
    actor: str  # "human", "auto:whitelist", "auto:classifier"
    ts: str = Field(default_factory=utcnow)


class SourceState(BaseModel):
    """Parser health, persisted per source. Silent parser death is the main
    long-run failure mode for Tier 2."""

    source_id: str
    last_run: str | None = None
    last_success: str | None = None  # last run that yielded >= 1 item
    last_item_count: int = 0
    consecutive_zero_runs: int = 0
    last_error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    # Reachability probe for DISABLED sources (#2): None while the source is
    # enabled (normal sweeps clear it), True/False after a probe.
    last_probe: str | None = None
    probe_ok: bool | None = None
    # Annual-file staleness (#75). A source pinned to one calendar year keeps
    # serving a well-formed file forever once that year passes — healthy fetch,
    # healthy parse, nothing forward-dated, no complaint. Set from
    # SourceConfig.covers_year at sweep time.
    coverage_expired: bool = False
