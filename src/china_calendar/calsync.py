"""Store → calendar sync over CalDAV.

The store is the source of truth; the calendar is a projection. Direction is
always store → calendar — a direct edit in a calendar client will be
overwritten on the next run. Reconciliation in the other direction is a
v2 idea, not a gap — see the wiki's deliberate limitations.

Ownership discipline: we only ever PUT/DELETE resources named `pc-*.ics`
that this tool created, and our ICS UIDs live in the tool's own UID
domain namespace. Anything else in the calendar is read-only, always.

Sync set (#70): every status syncs, carrying its STATUS_PREFIX; uncertain
statuses are forced all-day so a window never reads as a timed appointment.
Withholding a projection does not make the calendar more rigorous, it makes
it silently incomplete — a colleague seeing an empty October concludes
nothing is happening, which is worse than "(Projected)".

- removed, or sync_authorized explicitly False (the dashboard's calendar-off
  toggle): deleted from the calendar
- horizon: 12 months forward (store horizon stays unlimited)
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import httpx
from icalendar import Alarm
from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent

from .config import Config, NextcloudConfig
from .models import STATUS_PREFIX, Event, Status
from .store import Store

UID_DOMAIN = os.environ.get("PC_UID_DOMAIN", "china-calendar.example.internal")
# 18 months (#71): the outlook year plus the following half. 365 cut the 21st
# Party Congress (Oct 2027) out of a calendar built to track it. Measured cost
# of the extension: 21 events. The cap still matters — the store reaches 2031
# on IPU election data, which no working calendar wants.
FORWARD_DAYS = 540

# Reminder policy (#78, was #8): opt-in per event only — no status earns an
# alarm on its own. The VALARM rides in the pushed payload, so a default would
# ping every subscriber with a policy none of them chose.
REMIND_DEFAULT_STATUSES: set[Status] = set()
REMIND_TRIGGER = timedelta(days=-2)


def wants_alarm(event: Event) -> bool:
    if event.remind is not None:
        return event.remind
    return event.status in REMIND_DEFAULT_STATUSES


def ical_uid(event: Event) -> str:
    return f"{event.uid}@{UID_DOMAIN}"


def resource_name(event: Event) -> str:
    return f"{event.uid}.ics"


def eligible(event: Event, today: date) -> bool:
    # Past events stay in the calendar indefinitely — it is also the archive
    # (learned 2026-08-02: adopting the user's 2023/24 history and then
    # horizon-pruning it emptied their calendar of its past). Only the
    # FORWARD horizon is capped.
    if event.removed:
        return False
    if event.start_date() > today + timedelta(days=FORWARD_DAYS):
        return False
    # Explicit calendar-off is the only exclusion; None (never touched) and
    # True both sync. `is False` rather than `not` — None must not read as off.
    return event.sync_authorized is not False


def build_ics(event: Event) -> bytes:
    force_all_day = event.status in (Status.unverified, Status.rumored, Status.projected)
    all_day = event.all_day or force_all_day

    cal = ICalendar()
    cal.add("prodid", "-//china-calendar//pcal//EN")
    cal.add("version", "2.0")

    ical = ICalEvent()
    ical.add("uid", ical_uid(event))
    ical.add("summary", f"{STATUS_PREFIX[event.status]} {event.title()}")
    if all_day:
        start = event.start_date()
        end = event.end_date() + timedelta(days=1)  # DTEND exclusive
        ical.add("dtstart", start)
        ical.add("dtend", end)
    else:
        start_dt = datetime.fromisoformat(event.start).astimezone(timezone.utc)
        ical.add("dtstart", start_dt)
        if event.end:
            ical.add("dtend", datetime.fromisoformat(event.end).astimezone(timezone.utc))

    primary = event.sources[0] if event.sources else None
    last_verified = max((s.verified_at for s in event.sources if s.verified_at), default=None)
    description = [
        f"status: {event.status.value}",
        f"tier: {event.tier}",
        f"source: {primary.url if primary and primary.url else '(manual/asserted)'}",
        f"last_verified: {last_verified or 'never'}",
        f"record: {event.uid}",
    ]
    if event.note:
        description.append(f"note: {event.note}")
    ical.add("description", "\n".join(description))
    if event.location:
        ical.add("location", event.location)
    if event.sectors:
        ical.add("categories", event.sectors)
    ical.add("dtstamp", datetime.now(timezone.utc))
    ical.add("last-modified", datetime.now(timezone.utc))
    if wants_alarm(event):
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"{STATUS_PREFIX[event.status]} {event.title()}")
        alarm.add("trigger", REMIND_TRIGGER)
        ical.add_component(alarm)
    cal.add_component(ical)
    return cal.to_ical()


class CalDavClient:
    """Thin CalDAV client. Only knows PUT/DELETE by resource name and how to
    list resources that carry our pc- prefix — it cannot address anything
    else in the calendar by construction."""

    def __init__(self, cfg: NextcloudConfig):
        self.cfg = cfg
        self._client = httpx.Client(
            auth=(cfg.user, cfg.app_password),
            timeout=30,
            headers={"User-Agent": "china-calendar/0.1 calsync"},
        )

    def put(self, name: str, ics: bytes) -> None:
        resp = self._client.put(
            self.cfg.calendar_url + name, content=ics,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"PUT {name}: HTTP {resp.status_code} {resp.text[:200]}")

    def delete(self, name: str) -> None:
        resp = self._client.delete(self.cfg.calendar_url + name)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(f"DELETE {name}: HTTP {resp.status_code}")

    def _list(self) -> set[str]:
        resp = self._client.request(
            "PROPFIND", self.cfg.calendar_url, headers={"Depth": "1"},
        )
        if resp.status_code != 207:
            raise RuntimeError(f"PROPFIND: HTTP {resp.status_code}")
        names = set()
        for href in re.findall(r"<[^>]*href[^>]*>([^<]+)</", resp.text, re.IGNORECASE):
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".ics"):
                names.add(name)
        return names

    def list_ours(self) -> set[str]:
        return {n for n in self._list() if n.startswith("pc-")}

    def list_foreign(self) -> set[str]:
        return {n for n in self._list() if not n.startswith("pc-")}

    def get_ics(self, name: str) -> bytes:
        resp = self._client.get(self.cfg.calendar_url + name)
        if resp.status_code != 200:
            raise RuntimeError(f"GET {name}: HTTP {resp.status_code}")
        return resp.content

    def close(self) -> None:
        self._client.close()


def push_event(store: Store, config: Config, event: Event,
               client: CalDavClient | None = None) -> str:
    """Immediate single-event push (or removal) after an interactive write —
    the human should not wait for the nightly sync to see their decision.
    Returns what happened; never raises (a failed push is caught up nightly)."""
    nc = config.nextcloud
    own_client = client is None
    if own_client:
        if not nc.configured:
            return "sync-not-configured"
        client = CalDavClient(nc)
    try:
        if event.removed or not eligible(event, date.today()):
            client.delete(resource_name(event))
            store.mark_synced(event.uid, None)
            return "removed-from-calendar"
        client.put(resource_name(event), build_ics(event))
        store.mark_synced(event.uid, ical_uid(event))
        return "pushed"
    except RuntimeError as exc:
        return f"push-failed: {exc}"
    finally:
        if own_client:
            client.close()


def _adopt_foreign(store: Store, client: CalDavClient, report: dict) -> None:
    """Perfect-representation rule (issue #13): entries created outside the
    tool (phone, Nextcloud UI, other MCP) are imported into the store as
    Tier 0 manual records, then replaced by the managed version. The foreign
    resource is deleted ONLY after its store record is safely written."""
    import hashlib

    from .models import Provenance, SourceRef, slugify

    for name in sorted(client.list_foreign()):
        try:
            raw = client.get_ics(name)
            cal = ICalendar.from_ical(raw)
            vevent = next(iter(cal.walk("VEVENT")), None)
            if vevent is None:
                continue
            summary = str(vevent.get("SUMMARY", "")).strip() or "Unbenannter Termin"
            foreign_uid = str(vevent.get("UID", name)).strip()
            dtstart = vevent["DTSTART"].dt
            all_day = not isinstance(dtstart, datetime)
            start_iso = dtstart.isoformat()
            end_iso = None
            if vevent.get("DTEND") is not None:
                dtend = vevent["DTEND"].dt
                if all_day:
                    end_iso = (dtend - timedelta(days=1)).isoformat()
                    if end_iso == start_iso:
                        end_iso = None
                else:
                    end_iso = dtend.isoformat()
            location = str(vevent.get("LOCATION", "")).strip() or None
            categories = []
            if vevent.get("CATEGORIES") is not None:
                cats = vevent.get("CATEGORIES")
                for cat in (cats if isinstance(cats, list) else [cats]):
                    categories.extend(str(c) for c in getattr(cat, "cats", [cat]))
            tail = hashlib.sha256(foreign_uid.encode()).hexdigest()[:6]
            uid = f"pc-adopted-{slugify(summary)[:36].rstrip('-')}-{tail}"
            if not store.exists(uid):
                event = Event(
                    uid=uid, title_de=summary,
                    start=start_iso, end=end_iso, all_day=all_day,
                    tier=0, status=Status.scheduled, provenance=Provenance.manual,
                    location=location,
                    sectors=[str(c) for c in categories],
                    sources=[SourceRef(evidence=(
                        "adopted from a calendar entry created outside the tool "
                        f"(original UID {foreign_uid})"))],
                    note=str(vevent.get("DESCRIPTION", "")).strip()[:500] or None,
                )
                store.add(event, actor="sweep:adopt",
                          reason=f"adopted foreign calendar entry {name}")
                client.put(resource_name(event), build_ics(event))
                store.mark_synced(event.uid, ical_uid(event))
            elif location:
                # A restored/duplicate foreign copy can backfill fields the
                # first adoption missed.
                existing = store.get(uid)
                if not existing.location:
                    store.amend(uid, {"location": location}, actor="adopt:backfill",
                                reason="location backfilled from restored calendar copy")
            # store record confirmed on disk → now the foreign copy may go
            client.delete(name)
            report["adopted"] += 1
        except Exception as exc:  # one bad foreign entry must not stop the sync
            report["errors"].append(f"adopt {name}: {exc}")


def sync(store: Store, config: Config, client: CalDavClient | None = None,
         dry_run: bool = False, today: date | None = None) -> dict:
    """Push the eligible set, delete our orphans. Returns a report."""
    nc = config.nextcloud
    if client is None:
        if not nc.configured:
            return {"calsync": "skipped", "reason": "PC_NC_APP_PASSWORD not set"}
        client = CalDavClient(nc)

    today = today or date.today()
    report = {"calsync": True, "pushed": 0, "deleted": 0, "adopted": 0, "errors": []}
    try:
        if not dry_run:
            _adopt_foreign(store, client, report)
        want = {resource_name(e): e for e in store.iter_events() if eligible(e, today)}
        report["eligible"] = len(want)
        have = client.list_ours()
        for name, event in want.items():
            if dry_run:
                continue
            try:
                client.put(name, build_ics(event))
                store.mark_synced(event.uid, ical_uid(event))
                report["pushed"] += 1
            except RuntimeError as exc:
                report["errors"].append(str(exc))
        for name in sorted(have - set(want)):
            if dry_run:
                continue
            try:
                client.delete(name)
                uid = name[:-4]
                if store.exists(uid):
                    store.mark_synced(uid, None)
                report["deleted"] += 1
            except RuntimeError as exc:
                report["errors"].append(str(exc))
        if dry_run:
            report["would_push"] = sorted(want)
            report["would_delete"] = sorted(have - set(want))
    finally:
        client.close()
    return report
