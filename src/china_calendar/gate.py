"""Selection gate (design: Tier 1, stage 2).

Routes every raw item to auto-accept / triage / auto-reject:

- The decision ledger is consulted first: an item decided once never
  resurfaces unless its content (hash) changes.
- auto-accept only for whitelisted sources (cfg.auto_accept) — trusted
  end-to-end, e.g. Bundesrat Plenartermine ICS.
- The classifier may only auto-REJECT (with high confidence) or send to
  triage. It never auto-accepts: per the design, everything non-whitelisted
  goes to a human until the ledger has a few hundred decisions.
- Few-shot calibration examples are sampled from the ledger, balanced and
  capped — the cheap learning loop.
"""

from __future__ import annotations

import logging
import random
from datetime import date
from functools import lru_cache

from .config import Config
from .llm import LLMError, classify
from .models import (Decision, Event, Provenance, RawItem, SourceRef, Status,
                     slugify, utcnow)
from .sources.base import SourceConfig
from .store import Store

# Auto-reject bar. 0.95, not 0.8: in live validation (2026-08-02) the
# classifier wrongly rejected an upcoming NPCSC item at 0.9 while obvious
# rejects scored 0.95-1.0 — borderline scores belong in triage.
REJECT_CONFIDENCE = 0.95
FEWSHOT_PER_CLASS = 6


def event_from_raw(item: RawItem, cfg: SourceConfig, store: Store | None = None) -> Event:
    # Identity from the feed's own id where there is one, with the title kept
    # out of it: a reworded SUMMARY must amend the record, not fork it.
    # Older records embed the title — keep their uid, or the next sweep forks
    # every one of them. One-time rename rides along with #9.
    if item.external_id:
        import hashlib
        tail = hashlib.sha256(item.external_id.encode()).hexdigest()[:6]
        uid = f"pc-{slugify(cfg.id)}-{tail}"
        legacy_uid = f"pc-{slugify(cfg.id)}-{slugify(item.title)[:40].rstrip('-')}-{tail}"
        if store is not None and store.exists(legacy_uid):
            uid = legacy_uid
    else:
        tail = (item.start or "")[:10].replace("-", "")
        uid = f"pc-{slugify(cfg.id)}-{slugify(item.title)[:40].rstrip('-')}-{tail}"
    all_day = item.start is not None and len(item.start) == 10
    return Event(
        uid=uid,
        title_de=item.title,
        start=item.start,
        end=item.end,
        all_day=all_day,
        timezone=cfg.timezone,
        tier=cfg.tier,
        status=Status.scheduled,  # feed entries are Scheduled, not Confirmed
        provenance=(Provenance.manual if cfg.tier == 0
                    else Provenance.feed if cfg.tier == 1
                    else Provenance.scrape),
        sectors=list(cfg.sectors),
        actors=list(cfg.actors),
        location=item.location,
        sources=[SourceRef(
            url=item.url,
            evidence=f"{item.title.split(': ', 1)[-1]} — {item.date_text or item.start}",
            verify_strings=list(item.verify_strings),
            retrieved_at=item.fetched_at,
        )],
        note=item.description,
        # Structure, not content, until an agenda item says otherwise (#74).
        sync_authorized=False if cfg.calendar_default_off else None,
    )


@lru_cache(maxsize=1)
def _skeleton_uid_prefixes() -> tuple[str, ...]:
    """uid prefixes of sources that produce container events."""
    from .sources.base import load_sources
    return tuple(f"pc-{slugify(c.id)}-" for c in load_sources() if c.skeleton)


def _enrich_target(store: Store, item: RawItem, cfg: SourceConfig) -> Event | None:
    """The skeleton event this agenda item belongs to: same actor, same day.

    Tier 0 is never a target: a manual record on the same actor
    and day would otherwise make the enrichment raise TierZeroProtected out
    of the gate. The item falls through to a standalone event instead, which
    is the documented fallback when no skeleton matches.
    """
    if not cfg.enrich_actor or not item.start:
        return None
    try:
        day = date.fromisoformat(item.start[:10])
    except ValueError:
        return None
    prefixes = _skeleton_uid_prefixes()
    matches = []
    for event in store.iter_events():
        if event.tier == 0 or event.removed:
            continue
        if cfg.enrich_actor not in event.actors:
            continue
        # Containers only. Without this the SOTEU record and the part-session
        # containing it were both legal owners of 16 September, and uid
        # alphabetical order decided — arbitrarily, and silently.
        if prefixes and not event.uid.startswith(prefixes):
            continue
        # Anywhere inside the skeleton's span, not just its first day (#69).
        # Bundesrat sittings are single-day so this is unchanged for them, but
        # an EP part-session runs Monday to Thursday and a Wednesday debate
        # would never have matched a record starting on the Monday.
        if event.start_date() <= day <= event.end_date():
            matches.append(event)

    if len(matches) > 1:
        # Two containers for the same actor and day should not happen; if it
        # does, say so rather than resolving it invisibly.
        logging.getLogger(__name__).warning(
            "enrichment ambiguity for %s on %s: %s — taking the first",
            cfg.id, day, ", ".join(e.uid for e in matches))
    return matches[0] if matches else None


def _human_set_calendar(event: Event) -> bool:
    """Did a person set this record's calendar flag by hand?

    Enrichment may lift the automatic calendar-off of #74, but must never
    overrule someone who switched an event off deliberately — otherwise the
    dashboard toggle silently stops meaning anything the next time an agenda
    item lands on that day.
    """
    return any(h.field == "sync_authorized" and str(h.actor).startswith("human:")
               for h in event.history)


def accept_item(store: Store, item: RawItem, cfg: SourceConfig, actor: str,
                reason: str | None = None) -> Event:
    """Shared accept path for whitelist and human triage decisions."""
    target = _enrich_target(store, item, cfg)
    if target is not None:
        note_line = item.title
        if note_line not in (target.note or ""):
            new_note = f"{target.note}\n{note_line}" if target.note else note_line
            store.amend(target.uid, {"note": new_note}, actor=actor,
                        reason=reason or f"agenda enrichment from {cfg.id}")
        # An accepted agenda item IS the evidence that this sitting matters, so
        # it earns a calendar place a bare skeleton does not get (#74). Only
        # lifts an automatic calendar-off — a human's explicit off stays off,
        # which is why this checks the record's own history rather than the
        # value alone.
        if target.sync_authorized is False and not _human_set_calendar(target):
            store.amend(target.uid, {"sync_authorized": True}, actor=actor,
                        reason=f"agenda item from {cfg.id} earns a calendar place")
        event = store.attach_source(
            target.uid,
            SourceRef(url=item.url,
                      evidence=f"{item.title} — {item.date_text or item.start}",
                      verify_strings=list(item.verify_strings),
                      retrieved_at=item.fetched_at),
            actor=actor, reason=f"agenda item from {cfg.id}",
        )
        item.route = "accepted"
        item.event_uid = event.uid
        store.save_raw(item)
        store.record_decision(Decision(
            content_hash=item.content_hash, source_id=cfg.id, title=item.title,
            decision="accept", reason=reason, actor=actor,
        ))
        return event

    event = event_from_raw(item, cfg, store)
    if store.exists(event.uid):
        existing = store.get(event.uid)
        patch = {}
        if existing.start != event.start:
            patch["start"] = event.start
        if existing.end != event.end:
            patch["end"] = event.end
        if patch:
            # A date moving is often the actual signal — history captures it.
            event = store.amend(event.uid, patch, actor=actor,
                                reason=reason or f"feed update from {cfg.id}")
        else:
            event = existing
    else:
        store.add(event, actor=actor, reason=reason or f"accepted from {cfg.id}")
    item.route = "accepted"
    item.event_uid = event.uid
    store.save_raw(item)
    store.record_decision(Decision(
        content_hash=item.content_hash, source_id=cfg.id, title=item.title,
        decision="accept", reason=reason, actor=actor,
    ))
    return event


def _fewshot_examples(store: Store) -> list[dict]:
    accepts, rejects = [], []
    for decision in store.iter_decisions():
        if decision.actor.startswith("auto:"):
            continue  # learn from humans, not from ourselves
        # Allowlist, not "accept vs everything else" (#68). A `corroborate`
        # decision means "we already hold this date", which says nothing about
        # relevance — bucketing it as a reject would teach the gate that APEC
        # leaders' meetings are irrelevant, the exact failure this guards.
        if decision.decision not in ("accept", "reject"):
            continue
        bucket = accepts if decision.decision == "accept" else rejects
        bucket.append({"item": {"title": decision.title, "source": decision.source_id},
                       "decision": decision.decision, "reason": decision.reason})
    rng = random.Random(0)
    sample = (rng.sample(accepts, min(FEWSHOT_PER_CLASS, len(accepts)))
              + rng.sample(rejects, min(FEWSHOT_PER_CLASS, len(rejects))))
    return sample


def run_gate(store: Store, config: Config, cfg: SourceConfig,
             items: list[RawItem], use_llm: bool = True) -> dict:
    counts = {"accepted": 0, "triage": 0, "rejected": 0, "already_decided": 0}
    profile = config.profile_path.read_text(encoding="utf-8") if config.profile_path.exists() else None
    examples = None

    for item in items:
        if store.decision_for(item.content_hash):
            counts["already_decided"] += 1
            continue

        if cfg.auto_accept:
            accept_item(store, item, cfg, actor="auto:whitelist")
            counts["accepted"] += 1
            continue

        if cfg.prefilter_keywords:
            haystack = f"{item.title} {item.description or ''}".lower()
            if not any(k.lower() in haystack for k in cfg.prefilter_keywords):
                item.route = "auto_reject"
                store.save_raw(item)
                store.record_decision(Decision(
                    content_hash=item.content_hash, source_id=cfg.id,
                    title=item.title, decision="reject",
                    reason="prefilter: no configured keyword in title/description",
                    actor="auto:prefilter",
                ))
                counts["rejected"] += 1
                continue

        if use_llm and profile:
            if examples is None:
                examples = _fewshot_examples(store)
            try:
                verdict = classify(config.llm, profile, {
                    "title": item.title, "start": item.start, "end": item.end,
                    "description": item.description, "source": cfg.id,
                }, examples)
                item.classifier = verdict
                if not verdict["relevant"] and verdict["confidence"] >= REJECT_CONFIDENCE:
                    item.route = "auto_reject"
                    store.save_raw(item)
                    store.record_decision(Decision(
                        content_hash=item.content_hash, source_id=cfg.id,
                        title=item.title, decision="reject",
                        reason=verdict["reason"], actor="auto:classifier",
                    ))
                    counts["rejected"] += 1
                    continue
            except LLMError:
                pass  # no key / bad output → fall through to triage

        # Suggest-and-confirm (#68): propose a match, never apply one. Only on
        # the triage path — an auto-accepted or auto-rejected item is not
        # waiting for a human, so there is nobody to confirm to.
        from .duplicates import find_candidate
        item.duplicate_of = find_candidate(store, item)
        item.route = "triage"
        store.save_raw(item)
        counts["triage"] += 1

    return counts


def pending_triage(store: Store) -> list[RawItem]:
    """Raw items routed to triage with no ledger decision yet, soonest first."""
    items = [item for item in store.iter_raw(route="triage")
             if store.decision_for(item.content_hash) is None]
    items.sort(key=lambda i: (i.start or "9999-12-31", i.title))
    return items


def _corroborate(store: Store, item: RawItem, reason: str | None,
                 actor: str) -> Event | None:
    """Attach a duplicate item's source to the event it duplicates (#68).

    The point of the whole mechanism: a second independent source reporting a
    date we already hold is evidence, and should strengthen that record rather
    than mint a second one or be dismissed as irrelevant.

    Uses `store.attach_source`, which is deliberately not Tier-0 gated —
    corroboration cannot change what a record says. The ledger entry is
    `corroborate`, which `_fewshot_examples` excludes from classifier training.
    """
    target = (item.duplicate_of or {}).get("uid")
    if not target:
        raise ValueError("no duplicate candidate on this item to corroborate")
    # The suggestion is a snapshot; the event may have been removed between the
    # sweep that computed it and the click. Attaching to a removed record is
    # invisible on every surface and only discoverable in history.
    if store.get(target).removed:
        raise ValueError(f"{target} has been removed; nothing to corroborate")

    already = any(s.url and item.url and s.url == item.url
                  for s in store.get(target).sources)
    if not already:
        store.attach_source(
            target,
            SourceRef(url=item.url,
                      evidence=(item.date_text or item.title),
                      verify_strings=list(item.verify_strings or [])),
            actor=actor,
            reason=reason or f"corroborated by {item.source_id}",
        )
    item.route = "corroborated"
    item.event_uid = target
    store.save_raw(item)
    store.record_decision(Decision(
        content_hash=item.content_hash, source_id=item.source_id,
        title=item.title, decision="corroborate",
        reason=reason or f"already held as {target}", actor=actor,
    ))
    return store.get(target)


def triage_decide(store: Store, config: Config, content_hash: str, decision: str,
                  reason: str | None, actor: str = "human") -> Event | None:
    """Apply a human triage decision. Returns the created event on accept."""
    from .sources.base import source_by_id

    if decision not in ("accept", "reject", "defer", "corroborate"):
        raise ValueError(
            f"decision must be accept|reject|defer|corroborate, got {decision!r}")
    item = store.get_raw(content_hash)
    if decision == "defer":
        return None  # stays pending; deliberate no-op, revisited next session
    if decision == "corroborate":
        return _corroborate(store, item, reason, actor)
    if decision == "accept":
        if item.source_id.startswith("inbox-"):
            # Inbox sources are ephemeral (one per dropped file), not in
            # sources.yaml — reconstruct their config: Tier 0, manual.
            cfg = SourceConfig(id=item.source_id, tier=0, kind="ics", url="",
                               notes="manual file drop")
        else:
            cfg = source_by_id(item.source_id)
        return accept_item(store, item, cfg, actor=actor, reason=reason)
    item.route = "rejected"
    store.save_raw(item)
    store.record_decision(Decision(
        content_hash=item.content_hash, source_id=item.source_id,
        title=item.title, decision="reject", reason=reason, actor=actor,
    ))
    return None
