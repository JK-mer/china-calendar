from datetime import date
from pathlib import Path

import pytest

from china_calendar.calsync import build_ics, eligible, ical_uid, sync
from china_calendar.config import Config
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store

TODAY = date(2026, 8, 2)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def make(uid, status, start="2026-10-01", end=None, removed=False, authorized=None,
         all_day=True, tier=3):
    return Event(
        uid=uid, title_en=f"Event {uid}", start=start, end=end, all_day=all_day,
        tier=tier, status=status, provenance=Provenance.manual,
        sectors=["trade"], removed=removed, sync_authorized=authorized,
        sources=[SourceRef(url="https://example.org/x", evidence="ev",
                           verified_at="2026-08-01T00:00:00+00:00")],
    )


def test_eligibility_rules():
    assert eligible(make("pc-a-2026", Status.confirmed), TODAY)
    assert eligible(make("pc-b-2026", Status.scheduled), TODAY)
    assert eligible(make("pc-c-2026", Status.unverified), TODAY)
    # #70: projected/rumored sync too, labelled — only an explicit calendar-off
    # excludes anything now.
    assert eligible(make("pc-d-2026", Status.projected), TODAY)
    assert eligible(make("pc-e-2026", Status.projected, authorized=True), TODAY)
    assert eligible(make("pc-f-2026", Status.rumored), TODAY)
    assert not eligible(make("pc-f2-2026", Status.rumored, authorized=False), TODAY)
    assert not eligible(make("pc-g-2026", Status.confirmed, removed=True), TODAY)
    # past events stay in the calendar (it is also the archive); only the
    # forward horizon is capped
    assert eligible(make("pc-h-2026", Status.confirmed, start="2026-05-01"), TODAY)
    assert eligible(make("pc-h-2023", Status.confirmed, start="2023-11-12"), TODAY)
    # horizon is 18 months since #71, so 2028-01 is now inside it
    assert eligible(make("pc-i-2028", Status.confirmed, start="2028-01-01"), TODAY)
    assert not eligible(make("pc-i-2029", Status.confirmed, start="2029-01-01"), TODAY)


def test_ics_carries_provenance():
    event = make("pc-apk-2026", Status.scheduled, start="2026-10-29", end="2026-10-31")
    ics = build_ics(event).decode()
    assert "UID:pc-apk-2026@china-calendar.example.internal" in ics
    assert "SUMMARY:(Scheduled) Event pc-apk-2026" in ics
    assert "status: scheduled" in ics.replace("\r\n ", "")  # unfold
    assert "https://example.org/x" in ics.replace("\r\n ", "")
    assert "CATEGORIES:trade" in ics
    # all-day: DTEND exclusive → Nov 1 for an Oct 29-31 event
    assert "DTSTART;VALUE=DATE:20261029" in ics
    assert "DTEND;VALUE=DATE:20261101" in ics


def test_unverified_forced_all_day():
    event = make("pc-x-2026", Status.unverified,
                 start="2026-09-25T09:30:00+02:00", all_day=False)
    ics = build_ics(event).decode()
    assert "DTSTART;VALUE=DATE:20260925" in ics
    assert "(Unverified)" in ics


class FakeClient:
    def __init__(self, existing=None, foreign=None):
        self.putted, self.deleted = {}, []
        self.existing = set(existing or [])
        self.foreign = dict(foreign or {})  # name -> ics bytes

    def put(self, name, ics):
        self.putted[name] = ics

    def delete(self, name):
        self.deleted.append(name)

    def list_ours(self):
        return set(self.existing)

    def list_foreign(self):
        return set(self.foreign)

    def get_ics(self, name):
        return self.foreign[name]

    def close(self):
        pass


def test_sync_pushes_and_prunes_only_ours(store):
    store.add(make("pc-keep-2026", Status.confirmed), actor="human:test")
    store.add(make("pc-gone-2026", Status.confirmed, removed=True), actor="human:test")
    client = FakeClient(existing={"pc-gone-2026.ics", "pc-orphan-2025.ics"})
    report = sync(store, store.config, client=client, today=TODAY)
    assert set(client.putted) == {"pc-keep-2026.ics"}
    # removed record and stale orphan pruned; both are pc-*.ics we own
    assert sorted(client.deleted) == ["pc-gone-2026.ics", "pc-orphan-2025.ics"]
    assert report["pushed"] == 1 and report["deleted"] == 2 and not report["errors"]
    assert store.get("pc-keep-2026").calendar_uid == ical_uid(make("pc-keep-2026", Status.confirmed))
    assert store.get("pc-gone-2026").calendar_uid is None


def test_sync_skips_gracefully_without_credentials(store, monkeypatch):
    monkeypatch.delenv("PC_NC_APP_PASSWORD", raising=False)
    report = sync(store, Config(store_dir=store.config.store_dir), client=None)
    assert report["calsync"] == "skipped"


FOREIGN_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Nextcloud//EN
BEGIN:VEVENT
UID:phone-add-123@nextcloud
SUMMARY:Mittagessen mit Delegation
DTSTART;VALUE=DATE:20261015
DTEND;VALUE=DATE:20261016
DESCRIPTION:added from the phone
END:VEVENT
END:VCALENDAR
"""


def test_foreign_entry_is_adopted(store):
    client = FakeClient(foreign={"nextcloud-abc.ics": FOREIGN_ICS})
    report = sync(store, store.config, client=client, today=TODAY)
    assert report["adopted"] == 1
    events = list(store.iter_events())
    assert len(events) == 1
    adopted = events[0]
    assert adopted.tier == 0 and adopted.provenance.value == "manual"
    assert adopted.title() == "Mittagessen mit Delegation"
    assert adopted.start == "2026-10-15" and adopted.end is None
    assert "phone-add-123@nextcloud" in adopted.sources[0].evidence
    # foreign copy replaced by the managed version, deleted only after import
    assert "nextcloud-abc.ics" in client.deleted
    managed = [n for n in client.putted if n.startswith("pc-adopted-")]
    assert managed and report["errors"] == []


FOREIGN_ICS_WITH_LOCATION = FOREIGN_ICS.replace(
    b"DESCRIPTION:added from the phone",
    b"LOCATION:Kanzleramt\nDESCRIPTION:added from the phone")


def test_restored_foreign_copy_backfills_and_is_deleted(store):
    """The backfill branch runs against an already-adopted record, and every
    adopted record is Tier 0 — so gating this actor strands the foreign copy
    in the calendar forever and leaves a permanent duplicate (#52)."""
    first = FakeClient(foreign={"nextcloud-abc.ics": FOREIGN_ICS})
    sync(store, store.config, client=first, today=TODAY)
    adopted = next(iter(store.iter_events()))
    assert adopted.tier == 0 and not adopted.location

    # the user re-creates the entry, this time with a location
    second = FakeClient(foreign={"nextcloud-def.ics": FOREIGN_ICS_WITH_LOCATION})
    report = sync(store, store.config, client=second, today=TODAY)

    assert report["errors"] == []
    assert store.get(adopted.uid).location == "Kanzleramt"
    assert "nextcloud-def.ics" in second.deleted, \
        "the foreign copy must be deleted, or it duplicates the managed entry"
    assert report["adopted"] == 1


def test_push_event_immediate(store):
    from china_calendar.calsync import push_event
    event = make("pc-live-2026", Status.confirmed)
    store.add(event, actor="human:test")
    client = FakeClient()
    assert push_event(store, store.config, store.get("pc-live-2026"), client=client) == "pushed"
    assert "pc-live-2026.ics" in client.putted
    store.remove("pc-live-2026", actor="human:test", reason="x")
    client2 = FakeClient()
    assert push_event(store, store.config, store.get("pc-live-2026"), client=client2) == "removed-from-calendar"
    assert client2.deleted == ["pc-live-2026.ics"]


def test_dry_run_touches_nothing(store):
    store.add(make("pc-keep-2026", Status.confirmed), actor="human:test")
    client = FakeClient(existing={"pc-orphan-2025.ics"})
    report = sync(store, store.config, client=client, dry_run=True, today=TODAY)
    assert not client.putted and not client.deleted
    assert report["would_push"] == ["pc-keep-2026.ics"]
    assert report["would_delete"] == ["pc-orphan-2025.ics"]


def test_reminder_policy_and_override():
    # policy (#78): no status alarms on its own — the calendar is shared
    confirmed = make("pc-r1-2026", Status.confirmed)
    assert "BEGIN:VALARM" not in build_ics(confirmed).decode()
    scheduled = make("pc-r2-2026", Status.scheduled)
    assert "BEGIN:VALARM" not in build_ics(scheduled).decode()
    # opting in per event is the only way to an alarm
    scheduled.remind = True
    ics = build_ics(scheduled).decode()
    assert "BEGIN:VALARM" in ics and "TRIGGER:-P2D" in ics
    confirmed.remind = False
    assert "BEGIN:VALARM" not in build_ics(confirmed).decode()


# ---------------------------------------------------------------- #70 sync set

def _ev(status, sync_authorized=None, start="2026-10-15"):
    from china_calendar.models import Event, Provenance, SourceRef
    return Event(uid=f"pc-t-{status.value}", title_en="T", start=start, end=start,
                 tier=3, status=status, provenance=Provenance.manual,
                 sync_authorized=sync_authorized,
                 sources=[SourceRef(evidence="per test fixture")])


def test_projected_syncs_by_default(tmp_path):
    """#70: withholding a projection makes the calendar silently incomplete;
    a colleague reads an empty October as 'nothing is happening'."""
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    assert eligible(_ev(Status.projected), date(2026, 8, 11)) is True
    assert eligible(_ev(Status.rumored), date(2026, 8, 11)) is True


def test_every_status_syncs_by_default(tmp_path):
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    for status in Status:
        assert eligible(_ev(status), date(2026, 8, 11)) is True, status


def test_explicit_calendar_off_is_the_only_exclusion(tmp_path):
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    assert eligible(_ev(Status.confirmed, sync_authorized=False), date(2026, 8, 11)) is False
    assert eligible(_ev(Status.projected, sync_authorized=False), date(2026, 8, 11)) is False


def test_none_is_not_off(tmp_path):
    """The whole point of the tri-state: `not None` would read as off and
    empty the calendar for every untouched record."""
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    event = _ev(Status.projected, sync_authorized=None)
    assert event.sync_authorized is None
    assert eligible(event, date(2026, 8, 11)) is True


def test_projected_keeps_its_prefix_and_stays_all_day(tmp_path):
    """Syncing a projection is only honest if it is labelled as one."""
    from china_calendar.calsync import build_ics
    from china_calendar.models import Status
    ics = build_ics(_ev(Status.projected)).decode()
    assert "(Projected)" in ics
    assert "DTSTART;VALUE=DATE" in ics


def test_horizon_covers_the_21st_party_congress():
    """#71: the Congress opens 2027-10-01, 416 days past 2026-08-11. A 365-day
    horizon dropped the most consequential date in the calendar's own subject
    area. Pinned so a future horizon change cannot silently undo it."""
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    congress = _ev(Status.projected, start="2027-10-01")
    assert eligible(congress, date(2026, 8, 11)) is True


def test_horizon_still_cuts_the_far_tail():
    """The cap earns its place: IPU election data reaches 2031."""
    from datetime import date
    from china_calendar.calsync import eligible
    from china_calendar.models import Status
    assert eligible(_ev(Status.scheduled, start="2031-05-31"), date(2026, 8, 11)) is False
