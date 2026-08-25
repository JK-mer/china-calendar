"""CLI adapter over the core store (invariant I2: the MCP server exposes the
same core functions with the same validation — no second write path)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import typer
import yaml

from .models import Event, Provenance, SourceRef, Status, make_uid, utcnow
from .store import Store, StoreError

app = typer.Typer(help="china-calendar: dates for German foreign & foreign-economic policy")


def _store() -> Store:
    return Store()


def _fmt(event: Event) -> str:
    span = event.start if not event.end or event.end == event.start else f"{event.start} → {event.end}"
    flags = " [removed]" if event.removed else ""
    return f"{span:<27} {event.status.value:<11} T{event.tier}  {event.uid:<34} {event.title()}{flags}"


@app.command("add")
def add(
    title: str = typer.Argument(..., help="Event title (English or German)"),
    start: str = typer.Option(..., "--start", help="ISO date, e.g. 2026-10-29"),
    end: str = typer.Option(None, "--end", help="ISO date, inclusive"),
    slug: str = typer.Option(None, "--slug", help="uid slug; defaults from title"),
    official: bool = typer.Option(False, "--official",
                                  help="The evidence is an official announcement → confirmed"),
    sectors: list[str] = typer.Option([], "--sector"),
    actors: list[str] = typer.Option([], "--actor"),
    source_url: str = typer.Option(None, "--source-url"),
    evidence: str = typer.Option(..., "--evidence", help="Where the date comes from — a quoted sentence or a free-text note"),
    note: str = typer.Option(None, "--note"),
    timezone_: str = typer.Option("Europe/Berlin", "--timezone"),
    german_title: str = typer.Option(None, "--title-de"),
    as_actor: str = typer.Option("human:cli", "--as", help="Who is making this entry"),
):
    """Add a manual (Tier 0) entry. The date's evidence is mandatory — the
    store never holds a date with no stated origin (invariant I1) — and the
    status comes from that evidence, not from a flag.

    Being the human's adapter is not an exemption: an agent with shell access
    reaches for this first, so it may no more assert a status outright than
    the MCP tools may. Confirmed needs --official plus evidence that states
    the date.
    """
    from .verify import evidence_carries_date

    store = _store()
    year = start[:4]
    uid = make_uid(slug or title, year)
    dated = evidence_carries_date(evidence, start, end)
    if official and not dated:
        typer.echo("-- evidence does not state the date; recorded as scheduled, "
                   "not confirmed")
    status = Status.confirmed if (official and dated) else Status.scheduled
    event = Event(
        uid=uid,
        title_en=title,
        title_de=german_title,
        start=start,
        end=end,
        all_day=True,
        timezone=timezone_,
        tier=0,
        status=status,
        provenance=Provenance.manual,
        sectors=list(sectors),
        actors=list(actors),
        sources=[SourceRef(url=source_url, evidence=evidence, retrieved_at=utcnow() if source_url else None)],
        note=note,
    )
    store.add(event, actor=as_actor)
    typer.echo(f"added {_fmt(event)}")


@app.command("list")
def list_events(
    query: str = typer.Argument(None),
    from_: str = typer.Option(None, "--from"),
    to: str = typer.Option(None, "--to"),
    days: int = typer.Option(None, "--days", help="Shortcut: today → today+N"),
    status: str = typer.Option(None, "--status"),
    tier: int = typer.Option(None, "--tier"),
    sector: list[str] = typer.Option([], "--sector"),
    actor: list[str] = typer.Option([], "--actor"),
    all_: bool = typer.Option(False, "--all", help="Include removed"),
):
    """List events, soonest first."""
    store = _store()
    date_from = date.fromisoformat(from_) if from_ else None
    date_to = date.fromisoformat(to) if to else None
    if days is not None:
        date_from = date_from or date.today()
        date_to = date_from + timedelta(days=days)
    events = store.search(
        query=query, from_=date_from, to=date_to, tier=tier, status=status,
        sectors=list(sector) or None, actors=list(actor) or None, include_removed=all_,
    )
    if not events:
        typer.echo("no events match")
        raise typer.Exit()
    for event in events:
        typer.echo(_fmt(event))
    typer.echo(f"-- {len(events)} event(s)")


@app.command("show")
def show(uid: str):
    """Full record, including sources and history."""
    event = _store().get(uid)
    typer.echo(event.model_dump_json(indent=2, by_alias=True))


@app.command("amend")
def amend(
    uid: str,
    patch: str = typer.Option(..., "--patch", help='JSON object, e.g. \'{"start": "2026-10-30"}\''),
    reason: str = typer.Option(..., "--reason"),
    as_actor: str = typer.Option("human:cli", "--as"),
):
    """Amend fields; every change lands in history with the reason."""
    event = _store().amend(uid, json.loads(patch), actor=as_actor, reason=reason)
    typer.echo(_fmt(event))


@app.command("remove")
def remove(
    uid: str,
    reason: str = typer.Option(..., "--reason"),
    as_actor: str = typer.Option("human:cli", "--as"),
):
    """Soft-delete. The record stays in the store; nothing is ever hard-deleted."""
    event = _store().remove(uid, actor=as_actor, reason=reason)
    typer.echo(f"removed {event.uid} ({reason})")


@app.command("seed")
def seed(
    path: Path = typer.Argument(..., exists=True, readable=True),
    force: bool = typer.Option(False, "--force", help="Overwrite is never done; force only re-reports existing"),
):
    """Load seed records (YAML list). Existing uids are skipped, never overwritten."""
    store = _store()
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    added = skipped = 0
    for entry in entries:
        uid = entry["uid"]
        # A seed asserts a pattern, never a verified fact. `confirmed` is the
        # one status nothing ever re-checks, so it has to be earned by a
        # fetched, string-matched source — the same reason a conversation
        # cannot mint it. A YAML file is not better evidence than a chat.
        seeded_status = str(entry.get("status", "projected"))
        if seeded_status == Status.confirmed.value:
            raise typer.BadParameter(
                f"{uid}: a seed cannot be 'confirmed' — that status requires a "
                "fetched source whose evidence states the date. Seed it as "
                "projected/scheduled and let verification promote it.")
        if store.exists(uid):
            skipped += 1
            if force:
                typer.echo(f"exists   {uid}")
            continue
        event = Event(
            uid=uid,
            title_en=entry.get("title_en"),
            title_de=entry.get("title_de"),
            title_zh=entry.get("title_zh"),
            start=str(entry["start"]),
            end=str(entry["end"]) if entry.get("end") else None,
            all_day=entry.get("all_day", True),
            timezone=entry.get("timezone", "Europe/Berlin"),
            tier=entry.get("tier", 3),
            status=Status(entry.get("status", "projected")),
            provenance=Provenance.manual,
            sectors=entry.get("sectors", []),
            actors=entry.get("actors", []),
            sources=[SourceRef(url=entry.get("source_url"), evidence=entry["evidence"].strip())],
            note=entry.get("note"),
        )
        store.add(event, actor="human:seed", reason=f"seeded from {path.name}")
        added += 1
        typer.echo(f"seeded   {_fmt(event)}")
    typer.echo(f"-- {added} added, {skipped} already present")


@app.command("index")
def index():
    """Rebuild index.json."""
    result = _store().rebuild_index()
    typer.echo(f"index rebuilt: {result['count']} events")


@app.command("init")
def init():
    """Create the store directory layout on the share, incl. the topic profile."""
    store = _store()
    cfg = store.config
    for directory in (cfg.events_dir, cfg.raw_dir, cfg.ledger_dir, cfg.sources_state_dir,
                      cfg.digest_dir, cfg.store_dir / "inbox"):
        directory.mkdir(parents=True, exist_ok=True)
    if not cfg.profile_path.exists():
        default = Path(__file__).with_name("profile_default.yaml")
        cfg.profile_path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"profile created at {cfg.profile_path}")
    typer.echo(f"store ready at {cfg.store_dir}")


@app.command("sweep")
def sweep_cmd(
    source: str = typer.Option(None, "--source", help="Sweep a single source id"),
    since: str = typer.Option(None, "--since", help="Ignore items ending before this ISO date (default: today)"),
    all_dates: bool = typer.Option(False, "--all-dates", help="Keep past items too (ledger bootstrap)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the classifier; everything non-whitelisted goes to triage"),
    force: bool = typer.Option(False, "--force", help="Ignore ETag/Last-Modified cache"),
):
    """Fetch → parse → gate for enabled sources."""
    from .fetch import Fetcher
    from .sources.base import load_sources, source_by_id
    from .sweep import sweep_all, sweep_source

    store = _store()
    cutoff = None if all_dates else (date.fromisoformat(since) if since else date.today())
    if source:
        fetcher = Fetcher(store.config, store)
        try:
            reports = [sweep_source(store, store.config, fetcher, source_by_id(source),
                                    since=cutoff, use_llm=not no_llm, force=force)]
        finally:
            fetcher.close()
        store.rebuild_index()
    else:
        reports = sweep_all(store, store.config, since=cutoff, use_llm=not no_llm, force=force)
    for report in reports:
        typer.echo(json.dumps(report, ensure_ascii=False))


@app.command("pending")
def pending():
    """Raw items awaiting a triage decision."""
    from .gate import pending_triage

    items = pending_triage(_store())
    if not items:
        typer.echo("triage queue is empty")
        raise typer.Exit()
    for item in items:
        classifier_hint = ""
        if item.classifier:
            verdict = "relevant" if item.classifier.get("relevant") else "not relevant"
            classifier_hint = f"  [{verdict} {item.classifier.get('confidence'):.2f}: {item.classifier.get('reason', '')[:60]}]"
        typer.echo(f"{item.content_hash}  {item.start or '?':<12} {item.title[:70]}{classifier_hint}")
    typer.echo(f"-- {len(items)} pending")


@app.command("triage")
def triage_cmd(
    hashes: list[str] = typer.Argument(..., help="content hashes from `pcal pending`"),
    decision: str = typer.Option(..., "--decision", help="accept | reject | defer"),
    reason: str = typer.Option(None, "--reason"),
    as_actor: str = typer.Option("human:cli", "--as"),
):
    """Decide pending items; decisions land in the ledger and feed the few-shot loop."""
    from .gate import triage_decide

    store = _store()
    for content_hash in hashes:
        event = triage_decide(store, store.config, content_hash, decision, reason, actor=as_actor)
        suffix = f" -> {event.uid}" if event else ""
        typer.echo(f"{decision}: {content_hash}{suffix}")


@app.command("calsync")
def calsync_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be pushed/deleted"),
):
    """Push eligible events to the Politics calendar over CalDAV (store →
    calendar, one direction). No-op when PC_NC_APP_PASSWORD is unset."""
    from .calsync import sync

    store = _store()
    report = sync(store, store.config, dry_run=dry_run)
    typer.echo(json.dumps(report, ensure_ascii=False))


@app.command("recheck")
def recheck_cmd():
    """Re-check unverified events: promote on evidence match, demote to
    rumored after repeated failures. Runs automatically as part of sweep."""
    from .fetch import Fetcher
    from .sweep import recheck_unverified

    store = _store()
    fetcher = Fetcher(store.config, store)
    try:
        report = recheck_unverified(store, fetcher)
    finally:
        fetcher.close()
    typer.echo(json.dumps(report))


@app.command("digest")
def digest_cmd():
    """Write this week's digest markdown into the store's digest/ folder."""
    from .digest import write_digest

    store = _store()
    typer.echo(write_digest(store, store.config))


@app.command("verify")
def verify_cmd(
    uid: str = typer.Argument(None, help="Verify one event; omit for all with unverified sources"),
    recheck: bool = typer.Option(False, "--recheck", help="Re-verify already-verified sources too"),
):
    """Re-fetch source URLs and string-match the evidence (code, not model)."""
    from .fetch import Fetcher
    from .verify import verify_all, verify_event

    store = _store()
    fetcher = Fetcher(store.config, store)
    try:
        if uid:
            failures = verify_event(store, fetcher, store.get(uid))
        else:
            failures = verify_all(store, fetcher, only_unverified=not recheck)
    finally:
        fetcher.close()
    if not failures:
        typer.echo("all verifiable sources verified")
        raise typer.Exit()
    for failure in failures:
        typer.echo(json.dumps(failure, ensure_ascii=False))
    typer.echo(f"-- {len(failures)} failure(s); records left untouched — review manually")
    raise typer.Exit(code=1)


@app.command("alerts")
def alerts_cmd(
    peek: bool = typer.Option(False, "--peek", help="Show without advancing the cursor"),
    defer: bool = typer.Option(False, "--defer",
                               help="Advance only on a later `alerts-ack` (delivery path)"),
):
    """Same-day alerts (#23): date moves, status changes and new events since
    the last run. Prints one line per change, nothing when quiet; the sweep
    unit pipes this into a Nextcloud notification."""
    from .alerts import run_alerts

    store = _store()
    for line in run_alerts(store, store.config, advance=not peek, defer=defer):
        typer.echo(line)


@app.command("profile-rollback")
def profile_rollback(
    backup: str = typer.Argument(None, help="Backup filename; omit to list them"),
):
    """Restore a topic-profile backup. profile_amend is callable
    by any authenticated MCP client, so undoing one has to be quick."""
    store = _store()
    profile_path = store.config.profile_path
    backups = sorted(profile_path.parent.glob("profile-*.yaml.bak"), reverse=True)
    if not backup:
        if not backups:
            typer.echo("no profile backups")
            raise typer.Exit()
        for path in backups:
            reason_file = path.with_suffix(".reason")
            reason = reason_file.read_text(encoding="utf-8").strip() if reason_file.exists() else ""
            typer.echo(f"{path.name}  {reason}")
        raise typer.Exit()

    target = profile_path.parent / backup
    if target not in backups:
        typer.echo(f"no such backup: {backup}")
        raise typer.Exit(code=1)
    # The rollback is itself a profile change, so it gets its own backup —
    # otherwise undoing the wrong one would be unrecoverable.
    from .models import utcnow

    stamp = utcnow().replace(":", "").replace("+0000", "Z")
    profile_path.replace(profile_path.with_name(f"profile-{stamp}.yaml.bak"))
    profile_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    store.record_system_event("profile_rollback", f"profile restored from {backup}",
                              actor="human:cli")
    typer.echo(f"profile restored from {backup}")


@app.command("alerts-ack")
def alerts_ack_cmd():
    """Commit the cursor parked by `alerts --defer`. Run only after the
    notification actually went out, so a failed delivery repeats rather than
    disappears."""
    from .alerts import ack_alerts

    store = _store()
    typer.echo("acknowledged" if ack_alerts(store.config) else "nothing pending")


@app.command("source-status")
def source_status():
    """Parser health per source."""
    from .sweep import ZERO_RUNS_FLAG

    store = _store()
    states = list(store.iter_source_states())
    if not states:
        typer.echo("no sources have run yet")
        raise typer.Exit()
    for s in states:
        flags = []
        if s.last_error:
            flags.append(f"ERROR {s.last_error}")
        if s.consecutive_zero_runs >= ZERO_RUNS_FLAG:
            flags.append(f"STALE {s.consecutive_zero_runs} zero runs")
        if s.probe_ok is True:
            flags.append("DISABLED but reachable — consider enabling")
        elif s.probe_ok is False:
            flags.append("disabled, probe unreachable")
        typer.echo(f"{s.source_id:<32} last_run={s.last_run or '-'}  items={s.last_item_count}  {' '.join(flags)}")


if __name__ == "__main__":
    app()
