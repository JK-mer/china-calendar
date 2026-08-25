from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store, StoreError, TierZeroProtected


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def make_event(uid="pc-test-2026", tier=0, status=Status.scheduled) -> Event:
    return Event(
        uid=uid,
        title_en="Test event",
        start="2026-10-01",
        end="2026-10-03",
        tier=tier,
        status=status,
        provenance=Provenance.manual,
        sources=[SourceRef(evidence="per test fixture")],
    )


def test_add_get_roundtrip(store):
    store.add(make_event(), actor="human:test")
    event = store.get("pc-test-2026")
    assert event.title_en == "Test event"
    assert event.history[0].field == "__created__"


def test_add_duplicate_refused(store):
    store.add(make_event(), actor="human:test")
    with pytest.raises(StoreError):
        store.add(make_event(), actor="human:test")


def test_amend_appends_history(store):
    store.add(make_event(), actor="human:test")
    event = store.amend("pc-test-2026", {"start": "2026-10-02"}, actor="human:test", reason="date moved")
    assert event.start == "2026-10-02"
    moves = [h for h in event.history if h.field == "start"]
    assert moves and moves[0].from_ == "2026-10-01" and moves[0].reason == "date moved"


def test_tier0_immune_to_automation(store):
    store.add(make_event(tier=0), actor="human:test")
    with pytest.raises(TierZeroProtected):
        store.amend("pc-test-2026", {"start": "2026-12-01"}, actor="sweep")
    with pytest.raises(TierZeroProtected):
        store.remove("pc-test-2026", actor="auto:classifier", reason="x")
    # a human may still amend
    store.amend("pc-test-2026", {"start": "2026-12-01"}, actor="human:test", reason="ok")


def test_tier1_amendable_by_sweep(store):
    store.add(make_event(uid="pc-feed-2026", tier=1), actor="auto:whitelist")
    event = store.amend("pc-feed-2026", {"start": "2026-10-05"}, actor="sweep", reason="feed update")
    assert event.start == "2026-10-05"


def test_remove_is_soft(store):
    store.add(make_event(), actor="human:test")
    store.remove("pc-test-2026", actor="human:test", reason="cancelled")
    assert store.get("pc-test-2026").removed is True
    assert not list(store.iter_events())
    assert list(store.iter_events(include_removed=True))


def test_unknown_field_refused(store):
    store.add(make_event(), actor="human:test")
    with pytest.raises(StoreError):
        store.amend("pc-test-2026", {"uid": "pc-other-2026"}, actor="human:test")


def test_search_filters(store):
    store.add(make_event(), actor="human:test")
    event2 = make_event(uid="pc-other-2027")
    event2.start, event2.end = "2027-03-01", None
    event2.sectors = ["trade"]
    store.add(event2, actor="human:test")

    assert len(store.search()) == 2
    assert [e.uid for e in store.search(sectors=["trade"])] == ["pc-other-2027"]
    from datetime import date
    assert [e.uid for e in store.search(from_=date(2027, 1, 1))] == ["pc-other-2027"]
    assert [e.uid for e in store.search(to=date(2026, 12, 31))] == ["pc-test-2026"]


def test_rebuild_index_writes_every_event(store):
    """index.json is for external consumers; the sweep and the CLI rebuild
    it, and nothing inside reads it back."""
    store.add(make_event(), actor="human:test")
    assert store.rebuild_index()["count"] == 1
    store.add(make_event(uid="pc-second-2026"), actor="human:test")
    index = store.rebuild_index()
    assert index["count"] == 2
    assert set(index["events"]) == {"pc-test-2026", "pc-second-2026"}


def test_uid_namespace_enforced():
    with pytest.raises(Exception):
        make_event(uid="not-our-namespace")


def test_dotfiles_ignored(store):
    store.add(make_event(), actor="human:test")
    (store.config.events_dir / ".sync_journal.json").write_text("{broken")
    assert len(list(store.iter_events())) == 1


def test_search_evidence_verified_and_range(store):
    plain = make_event()
    rich = make_event(uid="pc-rich-2026")
    rich.sources = [SourceRef(url="https://example.org/apk", evidence="APK findet in Seoul statt",
                              verified_at="2026-08-01T00:00:00+00:00")]
    store.add(plain, actor="human:test")
    store.add(rich, actor="human:test")
    # free text matches source evidence and url, not just titles/notes
    assert [e.uid for e in store.search(query="findet in seoul")] == ["pc-rich-2026"]
    assert [e.uid for e in store.search(query="example.org/apk")] == ["pc-rich-2026"]
    # verified-only keeps events with at least one verified source
    assert [e.uid for e in store.search(verified=True)] == ["pc-rich-2026"]
    assert len(store.search()) == 2


def test_alerts_cursor_and_filtering(store):
    from china_calendar.alerts import run_alerts

    event = make_event(tier=1)
    store.add(event, actor="auto:whitelist")
    # first run arms the cursor silently — no backlog flood
    assert run_alerts(store, store.config) == []
    # backdate the cursor: history timestamps are second-precision, so
    # same-second changes are invisible (fine daily, not in a test)
    (store.config.store_dir / ".alerts-cursor").write_text("2020-01-01T00:00:00+00:00")
    # automated date move and status change alert; human edits don't
    store.amend(event.uid, {"start": "2026-10-02"}, actor="auto:whitelist",
                reason="feed update")
    store.amend(event.uid, {"status": "confirmed"}, actor="sweep",
                reason="evidence verified")
    store.amend(event.uid, {"note": "n"}, actor="sweep")          # not an alert field
    store.amend(event.uid, {"end": "2026-10-05"}, actor="human:webui")  # human
    lines = run_alerts(store, store.config)
    assert len(lines) == 3
    assert any("start 2026-10-01 → 2026-10-02" in line for line in lines)
    assert any("status" in line and "confirmed" in line for line in lines)
    # the auto-creation itself alerts too: it went to the calendar
    assert any("NEW" in line for line in lines)
    # cursor advanced: quiet now
    assert run_alerts(store, store.config) == []


def test_deferred_alert_cursor_survives_a_failed_delivery(store):
    """The cursor must not move until the notification is out."""
    from china_calendar.alerts import ack_alerts, run_alerts

    event = make_event(tier=1)
    store.add(event, actor="auto:whitelist")
    assert run_alerts(store, store.config) == []
    (store.config.store_dir / ".alerts-cursor").write_text("2020-01-01T00:00:00+00:00")
    store.amend(event.uid, {"start": "2026-10-02"}, actor="auto:whitelist")

    first = run_alerts(store, store.config, defer=True)
    assert any("start" in line for line in first)
    # delivery failed: no ack, so the same lines come back rather than vanish
    assert run_alerts(store, store.config, defer=True) == first
    # delivery succeeded: ack commits the cursor and it goes quiet
    assert ack_alerts(store.config) is True
    assert run_alerts(store, store.config, defer=True) == []
    assert ack_alerts(store.config) is True
    assert ack_alerts(store.config) is False  # nothing pending twice over


def test_system_journal_records_and_reads_back(store):
    store.record_system_event("profile_amend", "profile rewritten", actor="mcp",
                              detail="because I said so")
    entries = list(store.iter_system_events())
    assert len(entries) == 1 and entries[0]["kind"] == "profile_amend"
    assert list(store.iter_system_events(since=entries[0]["ts"])) == []


def test_system_events_reach_the_alerts(store):
    from china_calendar.alerts import run_alerts

    assert run_alerts(store, store.config) == []
    (store.config.store_dir / ".alerts-cursor").write_text("2020-01-01T00:00:00+00:00")
    store.record_system_event("profile_amend", "profile rewritten", actor="mcp")
    lines = run_alerts(store, store.config)
    assert any("profile_amend" in line for line in lines)


def test_truncated_system_journal_line_does_not_hide_the_rest(store):
    store.record_system_event("a", "first", actor="mcp")
    path = store.config.store_dir / Store.SYSTEM_JOURNAL
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026', )
    store.record_system_event("b", "second", actor="mcp")
    assert [e["kind"] for e in store.iter_system_events()] == ["a", "b"]


def test_amend_and_requeue_drops_moved_dates_to_unverified(store):
    store.add(make_event(tier=3, status=Status.scheduled), actor="human:test")
    event, requeued = store.amend_and_requeue(
        "pc-test-2026", {"start": "2026-10-02"}, actor="human:webui", reason="fix")
    assert requeued is True
    assert event.status is Status.unverified
    assert any(h.field == "status" and "requeued" in (h.reason or "")
               for h in event.history)


def test_amend_and_requeue_leaves_non_date_amends_alone(store):
    store.add(make_event(tier=3, status=Status.scheduled), actor="human:test")
    event, requeued = store.amend_and_requeue(
        "pc-test-2026", {"note": "context"}, actor="human:webui", reason="add note")
    assert requeued is False and event.status is Status.scheduled
    # an unchanged date in the patch is not a move either
    event, requeued = store.amend_and_requeue(
        "pc-test-2026", {"start": "2026-10-01", "note": "more"},
        actor="human:webui", reason="noop date")
    assert requeued is False and event.status is Status.scheduled


def test_amend_and_requeue_only_guards_verified_statuses(store):
    store.add(make_event(tier=3, status=Status.rumored), actor="human:test")
    event, requeued = store.amend_and_requeue(
        "pc-test-2026", {"end": "2026-10-04"}, actor="human:webui", reason="press update")
    assert requeued is False and event.status is Status.rumored
