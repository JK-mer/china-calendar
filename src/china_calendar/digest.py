"""Weekly digest — change *reporting*, distinct from change detection (open
question #6). Written as markdown into the store's digest/ folder on the
share, so it is readable from Nextcloud and by other agents.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import Config
from .gate import pending_triage
from .models import STATUS_PREFIX, Event
from .store import Store
from .sweep import ZERO_RUNS_FLAG


def _week_id(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _fmt_event(event: Event) -> str:
    span = event.start if not event.end or event.end == event.start else f"{event.start} → {event.end}"
    prefix = STATUS_PREFIX[event.status]
    source = f" — {event.sources[0].url}" if event.sources and event.sources[0].url else ""
    return f"- {prefix} **{event.title()}** ({span}){source}"


def build_digest(store: Store, config: Config, today: date | None = None) -> str:
    today = today or date.today()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    lines = [f"# china-calendar digest — {_week_id(today)}", "",
             f"Generated {today.isoformat()}.", ""]

    upcoming = store.search(from_=today, to=today + timedelta(days=21))
    lines += ["## Next three weeks", ""]
    lines += [_fmt_event(e) for e in upcoming] or ["- nothing on the calendar"]

    moved = []
    for event in store.iter_events(include_removed=True):
        for entry in event.history:
            if entry.ts >= week_ago and entry.field in ("start", "end", "status", "removed"):
                moved.append((event, entry))
    if moved:
        lines += ["", "## Moved / changed this week", ""]
        for event, entry in moved:
            lines.append(f"- **{event.title()}**: {entry.field} "
                         f"{entry.from_!r} → {entry.to!r} ({entry.actor}"
                         f"{': ' + entry.reason if entry.reason else ''})")

    pending = pending_triage(store)
    lines += ["", f"## Triage queue ({len(pending)} pending)", ""]
    lines += [f"- `{i.content_hash}` {i.start or '?'} {i.title}" for i in pending] \
        or ["- empty"]

    watch = [e for e in store.iter_events()
             if e.status.value in ("unverified", "rumored")]
    if watch:
        lines += ["", "## Watchlist (unverified / rumored)", ""]
        for event in watch:
            rechecks = sum(1 for h in event.history if h.field == "recheck_failed")
            unreachable = sum(1 for h in event.history if h.field == "recheck_unreachable")
            if rechecks >= 2:
                flag = f" — {rechecks} failed re-check(s), possible fabrication"
            elif unreachable:
                # Not the same thing at all: nobody has contradicted this date,
                # we just cannot reach the page that carries it.
                flag = (f" — source unreachable on {unreachable} run(s); "
                        "consider an alternative source")
            else:
                flag = ""
            lines.append(f"{_fmt_event(event)}{flag}")

    # Projections never move on their own (I1) — a window that closed without
    # resolution needs a human/research act: amend with a source, or remove.
    unresolved = [e for e in store.iter_events()
                  if e.status.value == "projected" and e.end_date() < today]
    if unresolved:
        lines += ["", "## Projection windows closed — unresolved", ""]
        for event in unresolved:
            lines.append(f"{_fmt_event(event)} — window passed; research and amend "
                         "with a source, or remove with reason")

    sick = [s for s in store.iter_source_states()
            if s.last_error or s.consecutive_zero_runs >= ZERO_RUNS_FLAG]
    # A file that has outlived its year (#75) fetches and parses perfectly and
    # would never appear above. Reading it here is what makes the flag
    # proactive: gaps() only answers when asked, and the whole point was a
    # failure nobody is asked about.
    stale_files = [s for s in store.iter_source_states() if s.coverage_expired]
    # probe_ok is only ever set for disabled sources; True means the block
    # that got the source disabled may have lifted — the actionable case.
    reachable = [s for s in store.iter_source_states() if s.probe_ok]
    if sick or reachable or stale_files:
        lines += ["", "## Source health", ""]
        for state in sick:
            problem = state.last_error or f"{state.consecutive_zero_runs} zero-item runs"
            lines.append(f"- `{state.source_id}`: {problem} (last success {state.last_success or 'never'})")
        for state in stale_files:
            lines.append(f"- `{state.source_id}`: STALE FILE — it covers a year that "
                         "has passed. The parser is fine and will keep reporting "
                         "success; re-point the URL to the current year's file.")
        for state in reachable:
            lines.append(f"- `{state.source_id}`: DISABLED but reachable again "
                         f"(probed {state.last_probe}) — consider enabling")

    decisions_this_week = sum(1 for d in store.iter_decisions() if d.ts >= week_ago)
    lines += ["", "## Instrumentation", "",
              f"- events in store: {sum(1 for _ in store.iter_events())}",
              f"- gate decisions in the last 7 days: {decisions_this_week}",
              f"- pending triage items: {len(pending)}", ""]
    return "\n".join(lines)


def write_digest(store: Store, config: Config) -> str:
    config.digest_dir.mkdir(parents=True, exist_ok=True)
    content = build_digest(store, config)
    path = config.digest_dir / f"{_week_id(date.today())}.md"
    path.write_text(content, encoding="utf-8")
    return str(path)
