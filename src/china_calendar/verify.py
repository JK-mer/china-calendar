"""Verification pass — a string check in code, not a model judgement (I1).

Re-fetches each source URL and confirms every verify_string is literally
present in the fetched content. On success stamps verified_at; on failure
reports — the caller decides (candidates get dropped, stored events get
surfaced, never silently deleted).
"""

from __future__ import annotations

import html as html_lib
import re

from .fetch import Fetcher, FetchError
from .models import Event, HistoryEntry, SourceRef, Status, utcnow
from .store import Store

# Promotion never downgrades: a human verification on an already-confirmed
# event must not pull it back to scheduled.
_STATUS_RANK = {Status.unverified: 0, Status.rumored: 1, Status.projected: 2,
                Status.scheduled: 3, Status.confirmed: 4}


def _normalise(text: str) -> str:
    # Tags first: parsers strip markup before lifting an evidence
    # sentence out of a page, so "<em>June 23</em>" inside the live sentence
    # made the stored evidence permanently unmatchable — silently, and it
    # defeated human corrections too. Both ends normalise the same way now.
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace(" ", " ").replace("\\/", "/")
    text = re.sub(r"\s+", " ", text)
    # Tag substitution above inserts a space, so markup abutting punctuation
    # yields "Nadi, Fiji ." while evidence copied from the RENDERED page has
    # it flush — and the two never match (#55, found live on Pre-COP31).
    # Applied to both sides, so it stays symmetric: page and stored string
    # normalise identically.
    # strip(): a tag at the START of an evidence string leaves a leading
    # space that no clean page can match. Stripping the haystack too is
    # harmless for a substring test.
    return re.sub(r"\s+([.,;:!?])", r"\1", text).strip()


def strings_present(content: str, verify_strings: list[str]) -> bool:
    haystack = _normalise(content)
    return all(_normalise(s) in haystack for s in verify_strings)


_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]


def _date_tokens(iso: str) -> list[str]:
    """Every ordinary way of writing one date in the languages this calendar
    actually holds. A whitelist, so the cost of a gap is a false negative
    (evidence flagged as dateless) rather than a false positive."""
    try:
        day, month, year = int(iso[8:10]), int(iso[5:7]), int(iso[:4])
    except ValueError:
        return []
    de, en = _MONTHS_DE[month - 1], _MONTHS_EN[month - 1]
    abbr = en[:3]
    return [
        iso[:10],
        f"{day}. {de}", f"{day}.{month}.", f"{day:02d}.{month:02d}.",
        f"{day}.{month}.{year}", f"{day:02d}.{month:02d}.{year}",
        f"{en} {day}", f"{abbr} {day}", f"{day} {en}", f"{day} {abbr}",
        f"{day}/{month}", f"{month}/{day}",
        f"{month}月{day}日", f"{year}年{month}月{day}日",
    ]


def evidence_carries_date(evidence: str, start: str, end: str | None = None) -> bool:
    """Does the evidence string actually mention the event's date?

    The literal string match (I1) proves that a caller-chosen string sits at
    a caller-chosen URL — nothing more: without this check, `evidence="2026"`
    against any page containing "2026" is enough to confirm any 2026 date.
    This ties the two ends together in code, without asking a model to judge
    anything — the evidence has to carry the day, not just the year.
    """
    haystack = _normalise(evidence).lower()
    candidates = _date_tokens(start) + (_date_tokens(end) if end else [])
    return any(_normalise(tok).lower() in haystack for tok in candidates if tok)


def verify_event(store: Store, fetcher: Fetcher, event: Event,
                 actor: str = "auto:verify") -> list[dict]:
    """Verify every verifiable source of one event. Returns failure reports
    (empty list = everything verifiable verified)."""
    failures = []
    changed = False
    for source in event.sources:
        if not source.url or not source.verify_strings:
            continue  # manual/tier-0 evidence has nothing to string-match
        try:
            result = fetcher.fetch_raw(source.url)
        except FetchError as exc:
            failures.append({"uid": event.uid, "url": source.url,
                             "error": str(exc), "kind": "fetch"})
            continue
        if strings_present(result.text, source.verify_strings):
            source.verified_at = utcnow()
            changed = True
        else:
            failures.append({"uid": event.uid, "url": source.url,
                             "missing": source.verify_strings, "kind": "mismatch"})
    if changed:
        store.attach_verification(event)
    return failures


def _promote(store: Store, event: Event, official: bool, actor: str,
             reason: str) -> Event:
    target = Status.confirmed if official else Status.scheduled
    if _STATUS_RANK[target] > _STATUS_RANK[event.status]:
        event = store.amend(event.uid, {"status": target.value},
                            actor=actor, reason=reason)
    return event


def _date_check(event: Event, evidence: str, official: bool) -> tuple[bool, str | None]:
    """Whether `official` survives, and what to tell the caller.

    Nothing automated revisits `confirmed`, so it has to be earned: evidence
    that does not state the date cannot buy it. Below that a mismatch is
    reported but not blocking — date formats vary more than any whitelist.
    """
    if evidence_carries_date(evidence, event.start, event.end):
        return official, None
    if official:
        return False, ("evidence does not state this event's date, so it cannot "
                       "confirm it; recorded as scheduled instead")
    return False, "note: the evidence does not state this event's date"


def human_verify(store: Store, uid: str, evidence: str, actor: str,
                 official: bool = False, reason: str | None = None,
                 url: str | None = None) -> tuple[Event, str | None]:
    """Verification by human statement (issue #33) — the same provenance rule
    as event_add(human_stated=true): a human's own reading is valid evidence
    under I1. Appends a no-verify-strings source (nothing to string-match)
    and promotes to scheduled, or confirmed when the human says the source is
    an official announcement AND the evidence states the date. An optional
    url is kept as reference only.

    Returns (event, note); note explains a capped or flagged promotion."""
    evidence = (evidence or "").strip()
    if not evidence:
        raise ValueError("human verification needs an evidence statement")
    source = SourceRef(url=url, evidence=evidence, verified_at=utcnow())
    reason = reason or "verified by human statement"
    store.attach_source(uid, source, actor=actor, reason=reason)
    event = store.get(uid)
    was_official = official
    official, note = _date_check(event, evidence, official)
    if was_official and not official:
        # The refusal is the interesting event — it belongs in the Journal,
        # not only in the reply to whoever asked.
        event = store.note_history(uid, "official_declined", "scheduled",
                                   actor=actor, reason=note)
    return _promote(store, event, official, actor, reason), note


def source_verify(store: Store, fetcher: Fetcher, uid: str, url: str,
                  evidence: str, actor: str, official: bool = False,
                  reason: str | None = None) -> tuple[Event, bool, str | None]:
    """Verification against a source (issue #33): fetch NOW, literal string
    match in code (I1). On match the source is attached verified and the
    event promoted; on mismatch or fetch failure it is attached unverified —
    append-only either way (sources are provenance record), and the nightly
    sweep re-checks whatever did not match. This is also the correction path
    for events stuck on a wrong url/evidence string.

    Returns (event, matched, note); note explains a non-match."""
    evidence = (evidence or "").strip()
    if not url or not evidence:
        raise ValueError("source verification needs url and evidence")
    # Emptiness has to be judged AFTER normalisation: "&nbsp;" and "<br/>" are
    # non-empty raw but normalise to "", and "" is a substring of every page —
    # such evidence would stamp verified_at and promote to scheduled.
    if not _normalise(evidence):
        raise ValueError("evidence is empty once normalised; it would match any page")
    reason = reason or "source verification"
    matched, note = False, None
    retrieved_at = verified_at = None
    try:
        result = fetcher.fetch_raw(url)
        retrieved_at = utcnow()
        if strings_present(result.text, [evidence]):
            verified_at = utcnow()
            matched = True
        else:
            note = "evidence string NOT found on the page; source attached unverified"
    except FetchError as exc:
        note = f"source fetch failed ({exc}); source attached unverified"

    event = store.get(uid)
    existing = next((s for s in event.sources
                     if s.url == url and s.evidence == evidence), None)
    if existing:
        # a re-check of a source already on record (#43) — refresh its
        # stamps instead of appending a duplicate; history records the check
        existing.retrieved_at = retrieved_at or existing.retrieved_at
        if verified_at:
            existing.verified_at = verified_at
        # One write, not two (#55): refreshed stamps and the record of the
        # check that produced them commit together or not at all.
        store.attach_verification(event, history=HistoryEntry(
            field="source_recheck", to="matched" if matched else "no match",
            actor=actor, reason=reason))
    else:
        source = SourceRef(url=url, evidence=evidence, verify_strings=[evidence],
                           retrieved_at=retrieved_at, verified_at=verified_at)
        store.attach_source(uid, source, actor=actor, reason=reason)
    event = store.get(uid)
    if matched:
        was_official = official
        official, date_note = _date_check(event, evidence, official)
        if date_note:
            note = f"{note}; {date_note}" if note else date_note
        if was_official and not official:
            event = store.note_history(uid, "official_declined", "scheduled",
                                       actor=actor, reason=date_note)
        event = _promote(store, event, official, actor,
                         f"{reason}: evidence verified on fetch")
    return event, matched, note


def verify_all(store: Store, fetcher: Fetcher, only_unverified: bool = True) -> list[dict]:
    failures = []
    for event in store.iter_events():
        pending = [s for s in event.sources
                   if s.url and s.verify_strings and (s.verified_at is None or not only_unverified)]
        if not pending:
            continue
        failures.extend(verify_event(store, fetcher, event))
    return failures
