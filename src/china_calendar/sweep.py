"""Sweep orchestration: fetch → parse → gate, per enabled source.

Zero-item tracking: a Tier 2 parser silently dying is the main long-run
failure mode, so consecutive zero-item runs raise a maintenance flag in
SourceState (surfaced by source_status / the digest).
"""

from __future__ import annotations

from datetime import date

from .config import Config
from .fetch import Fetcher, FetchError
from .gate import run_gate
from .models import utcnow
from .sources.base import SourceConfig, load_sources
from .sources.bundesrat_to import fetch_bundesrat_to
from .sources.ep_opendata import fetch_ep_opendata, fetch_ep_opendata_items
from .sources.pecc import fetch_pecc
from .sources.bundestag_to_rss import parse_bundestag_to_rss
from .sources.npcobserver import parse_npcobserver
from .sources.tier1_ics import parse_ics
from .sources.tier2.apa import parse_apa
from .sources.tier2.bdi import parse_bdi
from .sources.tier2.bundespraesident import parse_bundespraesident
from .sources.tier2.electionguide import parse_electionguide
from .sources.tier2.ipu_elections import parse_ipu_elections
from .sources.tier2.oav import parse_oav
from .sources.tier2.wahltermine import parse_wahltermine
from .store import Store

PARSERS = {
    "ics": parse_ics,
    "rss:bundestag-to": parse_bundestag_to_rss,
    "rss:npcobserver": parse_npcobserver,
    "html:oav": parse_oav,
    "html:apa": parse_apa,
    "html:bdi": parse_bdi,
    "html:bundespraesident": parse_bundespraesident,
    "html:ipu-elections": parse_ipu_elections,
    "html:electionguide": parse_electionguide,
    "html:wahltermine": parse_wahltermine,
}

# Sources that need more than one fetch (landing page → detail pages); they
# drive the fetcher themselves and yield raw items directly.
SUBFETCH_PARSERS = {
    "html:bundesrat-to": fetch_bundesrat_to,
    "html:pecc": fetch_pecc,
    "api:ep-opendata": fetch_ep_opendata,
    "api:ep-opendata-items": fetch_ep_opendata_items,
}

ZERO_RUNS_FLAG = 3  # consecutive zero-item runs before a source counts as sick


def _mark_coverage(store: Store, cfg: SourceConfig) -> bool:
    """Persist whether this source's file has outlived the year it covers.

    Separate from the sweep body so it runs before every early return — the
    304 path in particular, which is the normal daily outcome for a static
    annual file and therefore the exact path the flag has to survive.
    """
    if not cfg.covers_year:
        return False
    expired = date.today().year > cfg.covers_year
    state = store.source_state(cfg.id)
    if state.coverage_expired != expired:
        state.coverage_expired = expired
        store.save_source_state(state)
    return expired


def sweep_source(store: Store, config: Config, fetcher: Fetcher,
                 cfg: SourceConfig, since: date | None = None,
                 use_llm: bool = True, force: bool = False) -> dict:
    subfetch = SUBFETCH_PARSERS.get(cfg.kind)
    parser = PARSERS.get(cfg.kind)
    if parser is None and subfetch is None:
        return {"source": cfg.id, "skipped": f"no parser for kind {cfg.kind!r}"}

    # Before any early return (#75). This needs only the clock and the config,
    # and computing it after the fetch defeated the whole feature: a static
    # year-pinned file is exactly the kind that 304s every day, so the flag
    # never fired in the one scenario it was built for.
    stale_file = _mark_coverage(store, cfg)

    try:
        if subfetch is not None:
            items = list(subfetch(cfg, fetcher))
        else:
            result = fetcher.get(cfg.id, cfg.url, force=force,
                                 ignore_robots=cfg.ignore_robots)
            if result.not_modified:
                report = {"source": cfg.id, "not_modified": True}
                if stale_file:
                    report["maintenance"] = (
                        f"file covers {cfg.covers_year} only — re-point the URL; "
                        f"it is unchanged because it is finished, not because it is current")
                return report
            items = list(parser(cfg, result.content))
    except FetchError as exc:
        return {"source": cfg.id, "error": str(exc)}
    # Parser health is judged on what the parser EXTRACTED, before the date
    # filter — an agenda source whose current TO lies entirely in the past is
    # healthy, not dead (#18).
    raw_count = len(items)
    if since:
        items = [i for i in items if (i.end or i.start or "")[:10] >= since.isoformat()]

    state = store.source_state(cfg.id)
    state.last_item_count = len(items)
    if raw_count:
        state.last_success = utcnow()
        state.consecutive_zero_runs = 0
    else:
        state.consecutive_zero_runs += 1
    # An enabled source is past probing; clear leftover probe state so the
    # digest stops suggesting to enable it.
    state.last_probe = None
    state.probe_ok = None
    store.save_source_state(state)

    counts = run_gate(store, config, cfg, items, use_llm=use_llm)
    report = {"source": cfg.id, "items": len(items), **counts}
    if state.consecutive_zero_runs >= ZERO_RUNS_FLAG:
        report["maintenance"] = f"{state.consecutive_zero_runs} consecutive zero-item runs"
    if stale_file:
        report["maintenance"] = (
            f"file covers {cfg.covers_year} only — re-point the URL to the "
            f"current year; the parser is fine and will keep reporting success")
    return report


def probe_disabled_source(store: Store, fetcher: Fetcher,
                          cfg: SourceConfig) -> dict:
    """Disabled sources are skipped but not forgotten: a cheap reachability
    probe per sweep, so a WAF-posture change (bundestag.de, #2) is noticed the
    day it happens instead of whenever someone re-tests by hand. No parsing,
    no gate, no cache validators touched."""
    report = {"source": cfg.id, "skipped": "disabled"}
    url = cfg.probe_url or cfg.url
    if not url:
        return report
    state = store.source_state(cfg.id)
    was_ok = state.probe_ok
    state.last_probe = utcnow()
    try:
        fetcher.fetch_raw(url)
    except FetchError as exc:
        state.probe_ok = False
        report["probe"] = f"unreachable: {exc}"
    else:
        state.probe_ok = True
        report["probe"] = "reachable"
        if was_ok is not True:
            report["reachable_again"] = True
    store.save_source_state(state)
    return report


def sweep_all(store: Store, config: Config, since: date | None = None,
              use_llm: bool = True, force: bool = False) -> list[dict]:
    fetcher = Fetcher(config, store)
    reports = []
    try:
        for cfg in load_sources():
            try:
                if not cfg.enabled:
                    reports.append(probe_disabled_source(store, fetcher, cfg))
                    continue
                reports.append(sweep_source(store, config, fetcher, cfg,
                                            since=since, use_llm=use_llm, force=force))
            except Exception as exc:
                # Expire, recheck and the index rebuild run after this loop;
                # losing those to one bad feed costs more than the feed does.
                reports.append({"source": cfg.id, "error": f"{type(exc).__name__}: {exc}"})
        from .inbox import process_inbox
        reports.extend(process_inbox(store, config, use_llm=use_llm, since=since))
        if use_llm:
            from .top_extract import run_top_extraction
            reports.append(run_top_extraction(store, config, fetcher))
        reports.append(expire_stale_triage(store))
        reports.append(recheck_unverified(store, fetcher))
    finally:
        fetcher.close()
    store.rebuild_index()
    return reports


def expire_stale_triage(store: Store, today: date | None = None) -> dict:
    """The queue only ever suggests the future (#24): pending items whose
    date has passed are ledger-rejected as expired — they cannot resurface,
    which is correct for a past date. Undated items are left alone, and the
    `auto:` actor keeps expiries out of the few-shot examples."""
    from .gate import pending_triage
    from .models import Decision

    cutoff = (today or date.today()).isoformat()
    report = {"expired_triage": 0}
    for item in pending_triage(store):
        last_day = (item.end or item.start or "")[:10]
        if last_day and last_day < cutoff:
            item.route = "auto_reject"
            store.save_raw(item)
            store.record_decision(Decision(
                content_hash=item.content_hash, source_id=item.source_id,
                title=item.title, decision="reject",
                reason="expired: date passed before a triage decision",
                actor="auto:expire",
            ))
            report["expired_triage"] += 1
    return report


RECHECK_DEMOTE_AFTER = 2  # consecutive failed re-checks → rumored
UNREACHABLE_BACKOFF_AFTER = 7   # daily attempts before dropping to weekly
UNREACHABLE_RETRY_DAYS = 7

# Two failure modes meaning opposite things: evidence absent from a page we
# read is what "possibly fabricated" is made of; a page we could not read
# says nothing about the date (unfccc.int walls us). Only the first demotes.
RECHECK_FAILED = "recheck_failed"
RECHECK_UNREACHABLE = "recheck_unreachable"


def _trailing_rechecks(event, field: str) -> tuple[int, str | None]:
    """How many `field` notes sit at the end of the history, and the newest
    one's timestamp. Any status change or a note of the other kind resets the
    run — the question is always 'since the last thing that happened'."""
    other = RECHECK_UNREACHABLE if field == RECHECK_FAILED else RECHECK_FAILED
    count, latest = 0, None
    for entry in reversed(event.history):
        if entry.field == field:
            count += 1
            if latest is None:
                latest = entry.ts
        elif entry.field in ("status", "__created__", other):
            break
    return count, latest


def _failed_rechecks(event) -> int:
    return _trailing_rechecks(event, RECHECK_FAILED)[0]


def _backing_off(event, today: date) -> bool:
    """True while a source that has been unreachable for a week is resting.
    Re-fetching a bot wall every night for months is noise, not diligence."""
    count, latest = _trailing_rechecks(event, RECHECK_UNREACHABLE)
    if count < UNREACHABLE_BACKOFF_AFTER or not latest:
        return False
    try:
        last = date.fromisoformat(latest[:10])
    except ValueError:
        return False
    return (today - last).days < UNREACHABLE_RETRY_DAYS


def recheck_unverified(store: Store, fetcher: Fetcher) -> dict:
    """Re-check unverified events (conversational adds whose evidence did not
    match, or amended dates). Promote to scheduled when the evidence now
    matches; demote to rumored after RECHECK_DEMOTE_AFTER failures. Never
    delete — a date the tool invented and quietly removed is worse than one
    left visible with a warning."""
    from .verify import strings_present

    report = {"recheck": True, "checked": 0, "promoted": 0, "demoted": 0,
              "unresolvable": 0, "unreachable": 0, "resting": 0}
    today = date.today()
    for event in list(store.iter_events()):
        if event.status.value != "unverified":
            continue
        if event.tier == 0:
            report["unresolvable"] += 1  # manual record: humans resolve it, not sweeps
            continue
        checkable = [s for s in event.sources if s.url and s.verify_strings]
        if not checkable:
            report["unresolvable"] += 1  # nothing to string-match; digest surfaces it
            continue
        if _backing_off(event, today):
            report["resting"] += 1
            continue
        report["checked"] += 1
        matched = False
        reached = False
        for source in checkable:
            try:
                result = fetcher.fetch_raw(source.url)
            except FetchError:
                continue
            reached = True
            if strings_present(result.text, source.verify_strings):
                source.verified_at = utcnow()
                matched = True
        if matched:
            store.attach_verification(event)
            store.amend(event.uid, {"status": "scheduled"}, actor="sweep",
                        reason="evidence verified on re-check")
            report["promoted"] += 1
        elif not reached:
            # Could not look. Says nothing about the date, so it must not
            # feed the fabrication counter.
            store.note_history(event.uid, RECHECK_UNREACHABLE, utcnow(), actor="sweep",
                               reason="source could not be fetched; date not re-checked")
            report["unreachable"] += 1
        else:
            event = store.note_history(event.uid, RECHECK_FAILED, utcnow(),
                                       actor="sweep", reason="evidence not found on re-fetch")
            if _failed_rechecks(event) >= RECHECK_DEMOTE_AFTER:
                store.amend(event.uid, {"status": "rumored"}, actor="sweep",
                            reason=f"{RECHECK_DEMOTE_AFTER} consecutive failed re-checks; "
                                   "flagged as likely fabrication in digest")
                report["demoted"] += 1
    return report
