"""Same-day change alerts (#23).

The digest reports weekly; a date move or a status change deserves a push the
day the sweep writes it. Detection is pure store history: every entry with
field start/end/status/__created__ newer than the cursor, written by a
non-human actor (your own dashboard/CLI edits don't need an echo).

Creations alert too: an auto-accepted or conversationally-added event goes
straight onto the family calendar, so the weekly digest is too late to be the
only thing watching it.

The cursor lives at `store/.alerts-cursor` (dotfile: every store scanner
skips it). On the very first run the cursor initialises to now and nothing
is reported — otherwise the whole history backlog would flood the first
notification. Delivery is OUTSIDE this module: the sweep systemd unit pipes
`pcal alerts` into Nextcloud's `occ notification:generate` on the host
(deploy/systemd/pcal-alerts.sh), so the engine needs no Nextcloud admin
credential — and the cursor only advances once that delivery reports success.
"""

from __future__ import annotations

from .config import Config
from .models import utcnow
from .store import Store

ALERT_FIELDS = {"start", "end", "status", "__created__"}
CURSOR_NAME = ".alerts-cursor"
PENDING_NAME = ".alerts-pending"


def _cursor_path(config: Config):
    return config.store_dir / CURSOR_NAME


def _pending_path(config: Config):
    return config.store_dir / PENDING_NAME


def collect_alerts(store: Store, since: str) -> list[str]:
    lines = []
    # Changes to the machinery itself: a rewritten topic profile
    # changes what the gate lets through and has no event to hang history on.
    for entry in store.iter_system_events(since=since):
        detail = f" — {entry['detail']}" if entry.get("detail") else ""
        lines.append(f"[{entry.get('kind')}] {entry.get('summary')} "
                     f"[{entry.get('actor')}]{detail}")
    for event in store.iter_events(include_removed=True):
        for h in event.history:
            if h.ts <= since or h.field not in ALERT_FIELDS:
                continue
            if h.actor.startswith("human"):
                continue
            reason = f" — {h.reason}" if h.reason else ""
            if h.field == "__created__":
                lines.append(f"{event.title()}: NEW ({h.to}, {event.start}) "
                             f"[{h.actor}]{reason}")
            else:
                lines.append(f"{event.title()}: {h.field} {h.from_} → {h.to} "
                             f"[{h.actor}]{reason}")
    return sorted(lines)


def run_alerts(store: Store, config: Config, advance: bool = True,
               defer: bool = False) -> list[str]:
    """Collect the lines due since the cursor.

    `defer` is the delivery path: it parks the new cursor value
    in a sidecar instead of committing it, so `ack_alerts` can advance it
    only once the notification actually went out. Advancing at collection
    time meant a failed `occ` run dropped the day's alerts silently.
    """
    path = _cursor_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    if not path.exists():
        # first run: arm the cursor, never flood with the backlog
        path.write_text(now, encoding="utf-8")
        return []
    since = path.read_text(encoding="utf-8").strip()
    lines = collect_alerts(store, since)
    if defer:
        _pending_path(config).write_text(now, encoding="utf-8")
    elif advance:
        path.write_text(now, encoding="utf-8")
    return lines


def ack_alerts(config: Config) -> bool:
    """Commit the deferred cursor after a successful delivery. Without an
    ack the cursor stays put and the next run re-reports the same lines —
    a duplicate notification is recoverable, a missing one is not."""
    pending = _pending_path(config)
    if not pending.exists():
        return False
    _cursor_path(config).write_text(pending.read_text(encoding="utf-8").strip(),
                                    encoding="utf-8")
    pending.unlink()
    return True
