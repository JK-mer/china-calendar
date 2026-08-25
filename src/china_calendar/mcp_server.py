"""MCP server — the conversational access path.

Thin adapter over the same core as the CLI (invariant I2). The load-bearing
rule: **status is assigned by the tool from provenance, never by the caller.**
The human's yes authorizes tracking; only a fetched-and-string-matched source
authorizes the date itself.

Auth (#12): bearer token when PC_MCP_TOKEN is set (MCP Stack pattern, kept
for local clients), plus OAuth 2.1 for claude.ai when the PC_OIDC_* env is
set — an OAuthProxy fronting a static confidential client on Nextcloud's
`oidc` app (which has no DCR; the proxy answers claude.ai's dynamic
registration itself). Tokens are RS256 JWTs verified against the IdP's
JWKS with issuer + audience checks. Traps live in issue #12 and
~/nextcloud-mcp/DEPLOYMENT-NOTES.md.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from fastmcp import FastMCP

from .config import load_config
from .expectations import evaluate, summarise
from .fetch import Fetcher, FetchError
from .gate import pending_triage, triage_decide
from .models import (Event, HistoryEntry, Provenance, SourceRef, Status,
                     make_uid, slugify, utcnow)
from .store import Store, StoreError, TierZeroProtected
from .sweep import ZERO_RUNS_FLAG
from .verify import evidence_carries_date, strings_present

CONFIG = load_config()
STORE = Store(CONFIG)

from .clusters import cluster_of as _cluster


def _brief(event: Event) -> dict:
    """Every read shows status + source inline — it must be impossible to
    discuss an event without seeing how solid it is."""
    return {
        "uid": event.uid,
        "title": event.title(),
        "start": event.start,
        "end": event.end,
        "all_day": event.all_day,
        "status": event.status.value,
        "tier": event.tier,
        "sectors": event.sectors,
        "actors": event.actors,
        "source_url": event.sources[0].url if event.sources else None,
        "verified": any(s.verified_at for s in event.sources),
        "note": event.note,
    }


def _build_oauth_proxy():
    """OAuth against Nextcloud's oidc app; None when unconfigured or the
    IdP discovery fetch fails (the server then still runs bearer-only —
    a Nextcloud outage must not take the local MCP path down)."""
    client_id = os.environ.get("PC_OIDC_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PC_OIDC_CLIENT_SECRET", "").strip()
    public_url = os.environ.get("PC_PUBLIC_URL", "").strip()
    if not (client_id and client_secret and public_url):
        return None
    # Discovery MUST use the public Nextcloud URL: fetched over the LAN URL
    # the oidc app hands out LAN-only endpoints (deployment notes trap).
    discovery = os.environ.get(
        "PC_OIDC_DISCOVERY_URL",
        "https://nextcloud.example.internal/.well-known/openid-configuration")
    import httpx

    try:
        idp = httpx.get(discovery, timeout=15).raise_for_status().json()
    except Exception as exc:
        import sys
        print(f"OAuth disabled: discovery fetch failed: {exc}", file=sys.stderr)
        return None
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier = JWTVerifier(
        jwks_uri=idp["jwks_uri"],
        issuer=idp["issuer"],
        audience=f"{public_url}/mcp",  # set via oidc:create --resource_url
    )
    return OAuthProxy(
        upstream_authorization_endpoint=idp["authorization_endpoint"],
        upstream_token_endpoint=idp["token_endpoint"],
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=verifier,
        base_url=public_url,
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
        valid_scopes=["openid", "profile", "email", "offline_access"],
    )


def build_auth():
    token = os.environ.get("PC_MCP_TOKEN", "").strip()
    bearer = None
    if token:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        bearer = StaticTokenVerifier({token: {"client_id": "china-calendar-client"}})
    proxy = _build_oauth_proxy()
    if proxy and bearer:
        from fastmcp.server.auth import MultiAuth

        return MultiAuth(server=proxy, verifiers=[bearer])
    return proxy or bearer


mcp = FastMCP(
    "china-calendar",
    auth=build_auth(),
    instructions=(
        "Date tracker for German foreign & foreign-economic policy "
        "(China/Asia-Pacific focus). Write tools take effect IMMEDIATELY — "
        "there is no pending queue for single events. Status "
        "(confirmed/scheduled/rumored/projected/unverified) is assigned by "
        "the server from provenance; callers cannot set it. An event added "
        "without a fetched source lands as (Unverified) and is re-checked by "
        "the next sweep. Always report the returned status back to the user."
    ),
)


# ------------------------------------------------------------------ reads

@mcp.tool
def calendar_search(query: str | None = None, from_date: str | None = None,
                    to_date: str | None = None, tier: int | None = None,
                    status: str | None = None, sectors: list[str] | None = None,
                    actors: list[str] | None = None) -> list[dict]:
    """Search stored events. Dates ISO (YYYY-MM-DD). Returns each event with
    its status, tier and primary source inline."""
    events = STORE.search(
        query=query,
        from_=date.fromisoformat(from_date) if from_date else None,
        to=date.fromisoformat(to_date) if to_date else None,
        tier=tier, status=status, sectors=sectors, actors=actors,
    )
    return [_brief(e) for e in events]


@mcp.tool
def event_get(uid: str) -> dict:
    """Full record including sources (with evidence and verification stamps)
    and the complete change history."""
    return json.loads(STORE.get(uid).model_dump_json(by_alias=True))


@mcp.tool
def upcoming(days: int = 90, sectors: list[str] | None = None) -> dict:
    """The conversational default: events in the next N days, grouped by
    cluster (german_institutional / eu / business_formats / china / other),
    soonest first, status and source inline."""
    today = date.today()
    events = STORE.search(from_=today, to=today + timedelta(days=days), sectors=sectors)
    grouped: dict[str, list[dict]] = {}
    for event in events:
        grouped.setdefault(_cluster(event), []).append(_brief(event))
    return {"horizon_days": days, "count": len(events), "clusters": grouped}


@mcp.tool
def gaps(from_date: str, to_date: str) -> dict:
    """What am I probably missing? Months with thin coverage in the window,
    plus sources whose parsers look dead or stale."""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    events = STORE.search(from_=start, to=end)
    months: dict[str, int] = {}
    cursor = start.replace(day=1)
    while cursor <= end:
        months[cursor.isoformat()[:7]] = 0
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    for event in events:
        months[event.start_date().isoformat()[:7]] = months.get(event.start_date().isoformat()[:7], 0) + 1
    sick = []
    for state in STORE.iter_source_states():
        if (state.last_error or state.consecutive_zero_runs >= ZERO_RUNS_FLAG
                or state.coverage_expired):
            entry = {"source": state.source_id, "last_error": state.last_error,
                     "zero_runs": state.consecutive_zero_runs,
                     "last_success": state.last_success}
            if state.coverage_expired:
                # Distinct wording on purpose: "parser looks dead" sends you
                # debugging code, and the parser is fine (#75).
                entry["stale_file"] = ("this source's file covers a year that has "
                                       "passed — re-point the URL; the parser is "
                                       "healthy and will keep reporting success")
            sick.append(entry)
    return {"thin_months": {m: c for m, c in months.items() if c <= 1},
            "sick_sources": sick}


@mcp.tool
def missing_expected(from_date: str, to_date: str, detail: bool = False) -> dict:
    """Which recurring formats SHOULD have a date in this window and don't.

    Complements gaps(), which only counts events per month and so cannot see
    a missing Party Congress in a month full of elections. The registry is the
    Glossary's `expect:` blocks — formats we watch, with their cadence.

    Three lists come back, and they mean different things:

    - `missing`: a cadence says an occurrence belongs in this period and no
      stored event matches. Worth researching.
    - `standing_watch`: formats with no cadence to project (EU-China Summit,
      Council formations). Absence is NOT evidence of a miss — `last_seen`
      says how long it has been. Never report these as gaps.
    - `stale_projections`: a projected record whose window has already closed
      with nothing promoting it. Either it happened and was missed, or it
      never happened; both need a human.

    This tool reports expectations, never dates: nothing here is a source.
    Research what it flags, then write through event_add with real evidence."""
    result = evaluate(STORE, date.fromisoformat(from_date), date.fromisoformat(to_date))
    out = summarise(result)
    if detail:
        out["formats"] = [e.as_dict() for e in result]
    return out


@mcp.tool
def source_status() -> list[dict]:
    """Parser health: last run, last success, item counts, errors, per source."""
    return [json.loads(s.model_dump_json()) for s in STORE.iter_source_states()]


# ----------------------------------------------------------------- writes

@mcp.tool
def event_add(title: str, start: str, end: str | None = None,
              sectors: list[str] | None = None, actors: list[str] | None = None,
              source_url: str | None = None, evidence: str | None = None,
              human_stated: bool = False, official: bool = False,
              note: str | None = None, title_de: str | None = None,
              timezone: str = "Europe/Berlin", slug: str | None = None) -> dict:
    """Add an event. WRITES IMMEDIATELY. Status is assigned by the server:

    - source_url + evidence given → the server re-fetches the URL NOW and
      string-matches the evidence. Match → scheduled; confirmed only if
      official=true AND the evidence itself states the date. No match →
      unverified.
    - human_stated=true + evidence, no URL → what the user told you: Tier 3,
      scheduled. Never confirmed and never Tier 0 from a conversation — an
      official announcement is confirmed in the dashboard or the CLI.
    - otherwise → unverified: usable immediately, flagged, re-checked by the
      next sweep, surfaced as a likely fabrication if nothing corroborates.

    Report the RETURNED status to the user — it may be lower than implied."""
    uid = make_uid(slug or title, start[:4])
    verified_note = None

    if source_url and evidence:
        try:
            fetcher = Fetcher(CONFIG, STORE)
            try:
                result = fetcher.fetch_raw(source_url)
            finally:
                fetcher.close()
            if strings_present(result.text, [evidence]):
                # A match proves the string is on the page, not that the page
                # says anything about this date — so confirmed additionally
                # requires the evidence to state it.
                dated = evidence_carries_date(evidence, start, end)
                if official and not dated:
                    verified_note = ("evidence matched but does not state the date; "
                                     "recorded as scheduled, not confirmed")
                elif not dated:
                    verified_note = "note: the evidence does not state the date"
                status = Status.confirmed if (official and dated) else Status.scheduled
                tier, provenance = 3, Provenance.research
                source = SourceRef(url=source_url, evidence=evidence,
                                   verify_strings=[evidence],
                                   retrieved_at=utcnow(), verified_at=utcnow())
            else:
                status, tier, provenance = Status.unverified, 3, Provenance.research
                source = SourceRef(url=source_url, evidence=evidence,
                                   verify_strings=[evidence], retrieved_at=utcnow())
                verified_note = "evidence string NOT found on the page; stored as unverified"
        except FetchError as exc:
            status, tier, provenance = Status.unverified, 3, Provenance.research
            source = SourceRef(url=source_url, evidence=evidence,
                               verify_strings=[evidence])
            verified_note = f"source fetch failed ({exc}); stored as unverified"
    elif human_stated and evidence:
        # Tier 3, never Tier 0, and never confirmed: Tier 0 is immortal (no
        # re-check, no automated amend or remove) and this path is driven by
        # a model. Tier 0 comes from the CLI, the dashboard or the inbox.
        status = Status.scheduled
        tier, provenance = 3, Provenance.manual
        source = SourceRef(evidence=evidence)
        if official:
            verified_note = ("recorded as scheduled: an official announcement is "
                             "confirmed in the dashboard or the CLI, not from a chat")
    else:
        status, tier, provenance = Status.unverified, 3, Provenance.research
        source = SourceRef(evidence=evidence or "asserted in conversation, no source")

    event = Event(
        uid=uid, title_en=title, title_de=title_de, start=start, end=end,
        all_day=len(start) == 10, timezone=timezone, tier=tier, status=status,
        provenance=provenance, sectors=sectors or [], actors=actors or [],
        sources=[source], note=note,
    )
    STORE.add(event, actor="mcp", reason="added in conversation")
    from .calsync import push_event
    pushed = push_event(STORE, CONFIG, event)
    result = {"written": True, "assigned_status": status.value,
              "calendar": pushed, **_brief(event)}
    if verified_note:
        result["verification"] = verified_note
    return result


def _refuse_tier_zero(uid: str, verb: str) -> None:
    """Manual records are not editable from a conversation.

    Every call here is issued by a model that reads untrusted web pages, so a
    page can talk it into rewriting or deleting a date a human entered by
    hand. Enforced at this adapter rather than in the store's actor gate: the
    store gate also governs calendar bookkeeping and corroboration, which must
    keep working on Tier 0.
    """
    if STORE.get(uid).tier == 0:
        raise TierZeroProtected(
            f"{uid} is Tier 0 (manual): {verb} it in the dashboard or the CLI, "
            "not from a conversation")


@mcp.tool
def event_amend(uid: str, patch: dict, reason: str) -> dict:
    """Amend fields (title_*, start, end, sectors, actors, note, timezone).
    APPLIES IMMEDIATELY; every change lands in history with the reason.
    Moving the date of a confirmed/scheduled event without a new verified
    source drops it to unverified for the next sweep to re-check. Status
    cannot be set directly."""
    refused = {"status", "tier", "provenance", "sync_authorized"} & set(patch)
    if refused:
        # sync_authorized is the calendar on/off control, and since #70 it is
        # an opt-OUT: every status syncs unless a person switched that record
        # off. Either direction is a human's call about what the shared
        # calendar shows, so chat does not get to move it.
        raise ValueError(f"{'/'.join(sorted(refused))} are assigned by the server "
                         "or reserved for the dashboard, not the caller")
    _refuse_tier_zero(uid, "amend")
    event, _ = STORE.amend_and_requeue(uid, patch, actor="mcp", reason=reason)
    from .calsync import push_event
    return {"written": True, "calendar": push_event(STORE, CONFIG, event), **_brief(event)}


@mcp.tool
def event_verify(uid: str, source_url: str | None = None,
                 evidence: str | None = None, human_stated: bool = False,
                 official: bool = False, reason: str | None = None) -> dict:
    """Verify an EXISTING event (or correct a wrong source) — the fix for
    unverified events stuck on a bad url/evidence string. APPLIES IMMEDIATELY.

    - source_url + evidence → the server fetches NOW and string-matches the
      evidence in code. Match → source attached verified, event promoted to
      scheduled; confirmed only if official=true AND the evidence states the
      event's date. No match → source attached unverified, status unchanged;
      the nightly sweep re-checks it.
    - human_stated=true + evidence → what the user told you: promoted to
      scheduled. Confirming is a dashboard/CLI act, not a conversational one.

    Sources are append-only; the old source stays as provenance record.
    Promotion never downgrades. Report the RETURNED status to the user."""
    from .verify import human_verify, source_verify

    if source_url and evidence:
        fetcher = Fetcher(CONFIG, STORE)
        try:
            event, matched, note = source_verify(
                STORE, fetcher, uid, source_url, evidence,
                actor="mcp", official=official, reason=reason)
        finally:
            fetcher.close()
        result = {"written": True, "matched": matched,
                  "assigned_status": event.status.value, **_brief(event)}
        if note:
            result["verification"] = note
    elif human_stated and evidence:
        # official is dropped on this path: every MCP call is made
        # by a model, so "the human says it is official" is unverifiable here.
        event, note = human_verify(STORE, uid, evidence, actor="mcp",
                                   official=False, reason=reason, url=source_url)
        result = {"written": True, "matched": True,
                  "assigned_status": event.status.value, **_brief(event)}
        notes = [n for n in (note, ("confirmed is not available from a conversation; "
                                    "confirm in the dashboard" if official else None)) if n]
        if notes:
            result["verification"] = "; ".join(notes)
    else:
        raise ValueError("need source_url+evidence, or human_stated=true+evidence")
    from .calsync import push_event
    result["calendar"] = push_event(STORE, CONFIG, event)
    return result


@mcp.tool
def event_remove(uid: str, reason: str) -> dict:
    """Soft-delete IMMEDIATELY: the record is kept with removed=true and the
    reason; nothing is ever hard-deleted."""
    _refuse_tier_zero(uid, "remove")
    event = STORE.remove(uid, actor="mcp", reason=reason)
    from .calsync import push_event
    return {"removed": True, "uid": event.uid, "reason": reason,
            "calendar": push_event(STORE, CONFIG, event)}


# --------------------------------------------------------------- research

@mcp.tool
def research_request(topic: str, window: str, note: str | None = None) -> dict:
    """Queue a Tier 3 research job for the next sweep (does NOT run inline —
    research stays under the verifier's rules, not the chat's)."""
    research_dir = CONFIG.store_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(topic)[:60]
    path = research_dir / f"{slug}.json"
    payload = {"topic": topic, "window": window, "note": note,
               "requested_at": utcnow(), "status": "queued"}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"queued": True, "id": slug}


# ------------------------------------------------------------ batch review

@mcp.tool
def pending_list() -> list[dict]:
    """Triage queue: raw items from the sweep awaiting a human decision,
    with classifier verdicts where available."""
    return [
        {"id": i.content_hash, "title": i.title, "start": i.start, "end": i.end,
         "source": i.source_id, "url": i.url, "description": (i.description or "")[:200],
         "classifier": i.classifier}
        for i in pending_triage(STORE)
    ]


@mcp.tool
def triage(item_ids: list[str], decision: str, reason: str | None = None) -> list[dict]:
    """Decide pending items: accept | reject | defer. Accept creates the event
    (tier/status from its source's rules); decisions land in the ledger and an
    item rejected once never resurfaces unless its content changes."""
    from .calsync import push_event

    # Whitelist, matching the JSON API. `corroborate` (#68) exists to attach a
    # source to a DIFFERENT record than the item came from, on a suggestion a
    # human has looked at — and `pending_list` never exposes `duplicate_of`, so
    # a model choosing it would be deciding blind, over a list, against targets
    # that may be Tier 0. Conversation is treated as hostile input everywhere
    # else in this server; it gets no batched cross-record write here either.
    if decision not in ("accept", "reject", "defer"):
        raise ValueError(
            f"decision must be accept|reject|defer, got {decision!r}"
            " — corroborate is dashboard-only, it needs the suggestion in front of a human")

    results = []
    for item_id in item_ids:
        event = triage_decide(STORE, CONFIG, item_id, decision, reason, actor="human:mcp")
        entry = {"id": item_id, "decision": decision,
                 "event_uid": event.uid if event else None}
        if event:
            entry["calendar"] = push_event(STORE, CONFIG, event)
        results.append(entry)
    return results


# ---------------------------------------------------------------- profile

@mcp.tool
def profile_get() -> str:
    """The topic profile the selection gate scores against (YAML)."""
    return CONFIG.profile_path.read_text(encoding="utf-8")


@mcp.tool
def profile_amend(new_profile_yaml: str, reason: str,
                  confirm_large_reduction: bool = False) -> dict:
    """Replace the topic profile (full YAML). Must parse as YAML and keep a
    non-empty clusters key; halving the cluster count needs
    confirm_large_reduction=true.

    One call rewrites what the whole gate considers relevant. It cannot be
    restricted to local clients, so it is visible and reversible instead:
    timestamped backup, system-journal entry, same-day alert, and
    `pcal profile-rollback`.
    """
    import yaml

    parsed = yaml.safe_load(new_profile_yaml)
    if not isinstance(parsed, dict) or "clusters" not in parsed:
        raise ValueError("profile must be a YAML mapping with a 'clusters' key")
    clusters = parsed["clusters"]
    if not clusters:
        raise ValueError("profile must keep at least one cluster — an empty "
                         "profile auto-rejects everything the gate sees")

    old_count = 0
    if CONFIG.profile_path.exists():
        old = yaml.safe_load(CONFIG.profile_path.read_text(encoding="utf-8"))
        old_count = len(old.get("clusters") or []) if isinstance(old, dict) else 0
    # Gutting the profile is what a prompt injection would do: everything
    # afterwards scores as irrelevant and the queue goes quiet.
    if old_count and len(clusters) * 2 < old_count and not confirm_large_reduction:
        raise ValueError(
            f"this replaces {old_count} clusters with {len(clusters)}. If that is "
            "really intended, repeat the call with confirm_large_reduction=true — "
            "and tell the user what is being dropped first")

    stamp = utcnow().replace(":", "").replace("+0000", "Z")
    backup = CONFIG.profile_path.with_name(f"profile-{stamp}.yaml.bak")
    if CONFIG.profile_path.exists():
        backup.write_text(CONFIG.profile_path.read_text(encoding="utf-8"), encoding="utf-8")
        backup.with_suffix(".reason").write_text(reason + "\n", encoding="utf-8")
    CONFIG.profile_path.write_text(new_profile_yaml, encoding="utf-8")
    STORE.record_system_event(
        "profile_amend",
        f"topic profile rewritten ({old_count} → {len(clusters)} clusters)",
        actor="mcp", detail=f"{reason} — restore with: pcal profile-rollback {backup.name}")
    return {"written": True, "backup": backup.name, "reason": reason,
            "clusters": f"{old_count} → {len(clusters)}",
            "note": "recorded in the system journal and in today's alerts; "
                    f"restore with `pcal profile-rollback {backup.name}`"}


# ---------------------------------------------------------------- prompts
#
# Stored runbooks (#41): clients that support MCP prompts (claude.ai's
# + menu, Claude Desktop) can invoke these by name, so the operational
# wording — including the I1 guardrails — is versioned here with the tools
# it protects, not retyped per chat.

@mcp.prompt(name="verify_unverified")
def verify_unverified_prompt() -> str:
    """Research every unverified event and verify it against fetchable
    sources — the recurring maintenance run."""
    return (
        "Pull all unverified events with calendar_search(status=\"unverified\"), "
        "then work through them one by one:\n\n"
        "1. Research current primary sources for each event with your own web "
        "access. Read the event's note first — it often records why "
        "verification failed before and where the correct source is.\n"
        "2. When a page states the date, call event_verify with the URL and "
        "the exact verbatim sentence (original language, copied character-"
        "for-character — the server does a literal string match on its own "
        "fetch). Set official=true only when the page belongs to the "
        "organizer or a government announcing its own event — and pick a "
        "sentence that STATES THE DATE, because the server will not confirm "
        "on evidence that does not contain it.\n"
        "3. NEVER use human_stated=true. That path records what the user told "
        "you, not what you found; you always hand over URL + evidence and let "
        "the server's fetch decide.\n"
        "4. If the server reports the evidence was NOT found on a page you "
        "can read perfectly well, the site is bot-walled for the engine "
        "(known cases: unfccc.int, state.gov, consilium.europa.eu, "
        "chathamhouse.org). Do not retry the same page — find an alternative "
        "fetchable source (Wikipedia, PECC, host-government pages). "
        "Machine-readable fragments like JSON-LD startDate strings make "
        "excellent evidence.\n"
        "5. Events with no source anywhere are fine to leave unverified — "
        "that is the system being honest. List them at the end, together "
        "with anything only you could see on a bot-walled page, so the user "
        "can confirm those by hand in the dashboard.\n\n"
        "Finish with a table: uid, what you did, the RETURNED status "
        "(never assume — report what the server assigned), and what remains "
        "open."
    )


@mcp.prompt(name="quarterly_outlook")
def quarterly_outlook_prompt(horizon_days: int = 90) -> str:
    """Outlook analysis over the coming window: what's coming, what's
    missing, what to watch."""
    return (
        f"Produce a forward outlook for the next {horizon_days} days of "
        "German foreign and foreign-economic policy dates (China/Asia-Pacific "
        "focus).\n\n"
        f"1. Pull upcoming(days={horizon_days}), gaps() and "
        "missing_expected() for the same window. Read the clusters as a "
        "brief, not a list.\n"
        "2. Analyse: where do the density peaks fall, which events interact "
        "(summit sequences, agenda feed-throughs), what does the German "
        "government have to position for, and which dates would a China "
        "analyst be embarrassed to miss?\n"
        "3. Research what is MISSING, driven by missing_expected(): its "
        "`missing` list is the research queue (a cadence says the date "
        "belongs in the window and nothing matches). Treat `standing_watch` "
        "as context, not gaps — those formats have no cadence, so absence "
        "proves nothing; check them only if `last_seen` looks stale. Report "
        "`stale_projections` to the user rather than researching them: a "
        "projection whose window closed unpromoted needs a human decision.\n"
        "4. For every missing date you can source, add it via event_add "
        "with source_url + the verbatim evidence sentence; report the "
        "returned status. Never assert a date without a source behind it; "
        "never use human_stated.\n"
        "5. Deliver: (a) the narrative outlook, (b) a table of key dates "
        "with status, (c) what you added or corrected, (d) open questions "
        "for the user."
    )


@mcp.prompt(name="check_missing")
def check_missing_prompt(from_date: str, to_date: str) -> str:
    """Maintenance pass: what should be in the calendar for this window and
    isn't — researched and added, with provenance."""
    return (
        f"Fill the gaps in the calendar for {from_date} to {to_date}.\n\n"
        f"1. Call missing_expected('{from_date}', '{to_date}'). It compares "
        "the recurring formats we watch against what is stored. Its three "
        "lists mean different things and must be treated differently — this "
        "is the whole point of the tool:\n\n"
        "   - `missing` — THE RESEARCH QUEUE. A cadence says an occurrence "
        "belongs in this period and nothing matches. Each entry carries "
        "`look` (where to check, including known bot-walls) and `confirms` "
        "(the wired source that would normally deliver it — if that source "
        "exists, check source_status() before researching by hand: a dead "
        "parser is the likelier explanation).\n"
        "   - `standing_watch` — CONTEXT, NEVER A GAP. These formats have no "
        "cadence, so absence proves nothing. Do NOT research them on spec: "
        "inventing a date for a summit nobody has announced is exactly what "
        "this system exists to prevent. Mention one only if `last_seen` looks "
        "genuinely stale, and then as a question for the user.\n"
        "   - `stale_projections` — REPORT, DO NOT RESEARCH. A projection "
        "whose window closed with nothing promoting it either happened and "
        "was missed, or never happened. Both need the user; neither is fixed "
        "by adding another date.\n\n"
        "2. Research the `missing` list only. For each, find the organiser's "
        "or a government's own announcement where one exists.\n"
        "3. Add what you can source via event_add with source_url plus the "
        "verbatim sentence the date comes from, in its original language. "
        "`official=true` only for the organiser's or a government's own "
        "page, and it earns `confirmed` only when that sentence states the "
        "date itself. NEVER use human_stated=true — it is reserved for the "
        "user. If a page is bot-walled, do not retry it: find a fetchable "
        "corroborating source, or leave the event unverified and say so.\n"
        "4. Leave genuinely unsourceable dates alone rather than guessing. "
        "A gap the tool keeps reporting is cheaper than a date nobody can "
        "check.\n\n"
        "Finish with three short lists: what you added (uid + the RETURNED "
        "status, never the one you expected), what you could not source and "
        "why, and anything needing the user — stale projections, stale "
        "standing watches, and any `confirms` source that looks dead."
    )


@mcp.prompt(name="research_and_add")
def research_and_add_prompt(topic: str, window: str = "") -> str:
    """Research a topic or format and add the dates found, with full
    provenance."""
    scope = f" in the window {window}" if window else ""
    return (
        f"Research upcoming dates for: {topic}{scope}.\n\n"
        "Rules of this system (they are enforced server-side, but working "
        "with them saves you round-trips):\n"
        "- Every event_add needs source_url + the exact verbatim sentence "
        "the date comes from (original language). The server re-fetches and "
        "string-matches before assigning status; official=true only for the "
        "organizer's or a government's own announcement, and only earns "
        "confirmed when the evidence sentence states the date itself.\n"
        "- No source at all → the event lands as unverified and gets "
        "re-checked nightly; that is acceptable for genuinely reported-only "
        "dates — say so in the note, including where you saw it.\n"
        "- never use human_stated=true (reserved for humans).\n"
        "- Check calendar_search first so you amend/verify existing records "
        "instead of duplicating them (event_verify corrects a wrong source; "
        "event_amend moves fields).\n"
        "- Pattern-based expectations without any announcement belong as a "
        "note in your report, not as events.\n\n"
        "Report every write with its RETURNED status, plus what you could "
        "not source."
    )


def main() -> None:
    host = os.environ.get("PC_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("PC_MCP_PORT", "8804"))
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
