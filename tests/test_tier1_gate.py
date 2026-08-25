from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.gate import pending_triage, run_gate, triage_decide
from china_calendar.models import RawItem
from china_calendar.sources.base import SourceConfig, content_hash
from china_calendar.sources.tier1_ics import parse_ics
from china_calendar.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

BUNDESRAT = SourceConfig(
    id="bundesrat-plenum-2026",
    tier=1,
    kind="ics",
    url="https://example.invalid/2026.ics",
    auto_accept=True,
    sectors=["german_institutional"],
    actors=["Bundesrat"],
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(Config(store_dir=tmp_path / "store"))
    return s


def test_parse_bundesrat_fixture():
    items = list(parse_ics(BUNDESRAT, (FIXTURES / "bundesrat-2026.ics").read_bytes()))
    assert len(items) >= 8  # roughly monthly plenary sessions
    first = items[0]
    assert first.start.startswith("2026-")
    assert "Plenarsitzung" in first.title or "Bundesrat" in first.title or first.title
    # deterministic hashes: same input, same identity
    again = list(parse_ics(BUNDESRAT, (FIXTURES / "bundesrat-2026.ics").read_bytes()))
    assert [i.content_hash for i in items] == [i.content_hash for i in again]


def test_whitelist_auto_accept_and_ledger_dedupe(store):
    items = list(parse_ics(BUNDESRAT, (FIXTURES / "bundesrat-2026.ics").read_bytes()))
    counts = run_gate(store, store.config, BUNDESRAT, items, use_llm=False)
    assert counts["accepted"] == len(items)
    assert len(list(store.iter_events())) == len(items)
    events = list(store.iter_events())
    assert all(e.tier == 1 and e.status.value == "scheduled" for e in events)
    assert all(e.sources[0].url for e in events)

    # Second run: ledger short-circuits everything.
    counts2 = run_gate(store, store.config, BUNDESRAT, items, use_llm=False)
    assert counts2["already_decided"] == len(items)
    assert counts2["accepted"] == 0


def test_date_change_amends_same_record_with_history(store):
    items = list(parse_ics(BUNDESRAT, (FIXTURES / "bundesrat-2026.ics").read_bytes()))
    run_gate(store, store.config, BUNDESRAT, items, use_llm=False)
    original_uid = items[0].event_uid
    assert original_uid is not None
    events_before = len(list(store.iter_events()))

    # The feed moves a session by a day: same ICS UID → new content hash, same
    # event uid, amended in place with history (a date moving IS the signal).
    from datetime import datetime, timedelta

    moved = items[0].model_copy(deep=True)
    moved.start = (datetime.fromisoformat(moved.start) + timedelta(days=1)).isoformat()
    moved.content_hash = content_hash(BUNDESRAT.id, moved.title, moved.start, moved.end)
    moved.event_uid = None

    from china_calendar.gate import accept_item

    amended = accept_item(store, moved, BUNDESRAT, actor="auto:whitelist")
    assert amended.uid == original_uid
    assert len(list(store.iter_events())) == events_before
    starts = [h for h in amended.history if h.field == "start"]
    assert starts and starts[-1].actor == "auto:whitelist"


def test_non_whitelisted_goes_to_triage_without_llm(store):
    cfg = SourceConfig(id="noisy-feed", tier=1, kind="ics", url="https://example.invalid/x.ics")
    item = RawItem(
        content_hash=content_hash("noisy-feed", "Some committee meeting", "2026-09-01"),
        source_id="noisy-feed",
        title="Some committee meeting",
        start="2026-09-01",
    )
    counts = run_gate(store, store.config, cfg, [item], use_llm=False)
    assert counts["triage"] == 1
    assert len(pending_triage(store)) == 1
    assert not list(store.iter_events())


def test_triage_reject_sticks(store, monkeypatch):
    cfg = SourceConfig(id="noisy-feed", tier=1, kind="ics", url="https://example.invalid/x.ics")
    item = RawItem(
        content_hash=content_hash("noisy-feed", "Irrelevant thing", "2026-09-02"),
        source_id="noisy-feed",
        title="Irrelevant thing",
        start="2026-09-02",
    )
    run_gate(store, store.config, cfg, [item], use_llm=False)
    triage_decide(store, store.config, item.content_hash, "reject", "not our topic")
    assert pending_triage(store) == []
    # re-sweep: the ledger blocks it from resurfacing
    counts = run_gate(store, store.config, cfg, [item], use_llm=False)
    assert counts["already_decided"] == 1


def test_expire_stale_triage(store):
    from datetime import date
    from china_calendar.models import RawItem
    from china_calendar.sources.base import content_hash as ch
    from china_calendar.sweep import expire_stale_triage

    for title, start, end in (("past", "2026-07-01", None),
                              ("still running", "2026-07-30", "2026-08-10"),
                              ("future", "2026-09-01", None),
                              ("undated", None, None)):
        item = RawItem(content_hash=ch("s", title), source_id="bundesrat-plenum-2026",
                       title=title, start=start, end=end, route="triage")
        store.save_raw(item)

    report = expire_stale_triage(store, today=date(2026, 8, 3))
    assert report["expired_triage"] == 1
    remaining = {i.title for i in pending_triage(store)}
    assert remaining == {"still running", "future", "undated"}
    decision = store.decision_for(ch("s", "past"))
    assert decision.actor == "auto:expire" and decision.decision == "reject"


def test_enrichment_matches_anywhere_in_the_skeleton_span():
    """#69: an EP part-session runs Mon-Thu, so a Wednesday debate must match a
    record starting on the Monday. Bundesrat sittings are single-day, so this
    generalisation leaves them unchanged."""
    from datetime import date as _date
    from pathlib import Path
    import tempfile
    from china_calendar.config import Config
    from china_calendar.gate import _enrich_target
    from china_calendar.models import Event, Provenance, RawItem, SourceRef, Status
    from china_calendar.sources.base import SourceConfig
    from china_calendar.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Config(store_dir=Path(tmp) / "store"))
        store.add(Event(
            uid="pc-ep-plenary-ical-a1b2c3", title_en="EP - Plenary (September part-session)",
            start="2026-09-14", end="2026-09-17", tier=3, status=Status.scheduled,
            provenance=Provenance.research, actors=["European Parliament"],
            sources=[SourceRef(evidence="per test fixture")],
        ), actor="human:test")
        cfg = SourceConfig(id="ep-opendata-items", tier=1, kind="api:ep-opendata-items",
                           url="https://example.org", enrich_actor="European Parliament")

        mid = RawItem(content_hash="h1", source_id=cfg.id, title="a vote",
                      start="2026-09-16", end="2026-09-16")
        assert _enrich_target(store, mid, cfg).uid == "pc-ep-plenary-ical-a1b2c3"

        first = RawItem(content_hash="h2", source_id=cfg.id, title="a vote",
                        start="2026-09-14", end="2026-09-14")
        assert _enrich_target(store, first, cfg).uid == "pc-ep-plenary-ical-a1b2c3"

        outside = RawItem(content_hash="h3", source_id=cfg.id, title="a vote",
                          start="2026-09-18", end="2026-09-18")
        assert _enrich_target(store, outside, cfg) is None


def _gate_store(tmp):
    from pathlib import Path
    from china_calendar.config import Config
    from china_calendar.store import Store
    return Store(Config(store_dir=Path(tmp) / "store"))


def test_calendar_default_off_keeps_bare_skeletons_out(tmp_path):
    """#74: a Bundesrat sitting with no agenda item is structure, not content."""
    from china_calendar.gate import event_from_raw
    from china_calendar.models import RawItem
    from china_calendar.sources.base import SourceConfig

    cfg = SourceConfig(id="bundesrat-plenum-2026", tier=1, kind="ics", url="x",
                       actors=["Bundesrat"], calendar_default_off=True)
    event = event_from_raw(RawItem(content_hash="h", source_id=cfg.id,
                                   title="Plenarsitzung", start="2026-09-25"), cfg)
    assert event.sync_authorized is False


def test_bundestag_skeleton_is_untouched(tmp_path):
    """The sitting-week rhythm stays: it structures everything else, and the
    profile calls it always relevant."""
    from china_calendar.gate import event_from_raw
    from china_calendar.models import RawItem
    from china_calendar.sources.base import SourceConfig

    cfg = SourceConfig(id="bundestag-sitzungskalender", tier=1, kind="ics", url="x",
                       actors=["Bundestag"])
    event = event_from_raw(RawItem(content_hash="h", source_id=cfg.id,
                                   title="Sitzungswoche", start="2026-09-07"), cfg)
    assert event.sync_authorized is None


def test_an_accepted_agenda_item_earns_a_calendar_place(tmp_path):
    from china_calendar.gate import accept_item, event_from_raw
    from china_calendar.models import RawItem
    from china_calendar.sources.base import SourceConfig

    store = _gate_store(tmp_path)
    skeleton_cfg = SourceConfig(id="bundesrat-plenum-2026", tier=1, kind="ics", url="x",
                                actors=["Bundesrat"], calendar_default_off=True)
    skeleton = event_from_raw(RawItem(content_hash="h0", source_id=skeleton_cfg.id,
                                      title="Plenarsitzung", start="2026-09-25"), skeleton_cfg)
    store.add(skeleton, actor="human:test")
    assert store.get(skeleton.uid).sync_authorized is False

    to_cfg = SourceConfig(id="bundesrat-tagesordnung", tier=1, kind="html:bundesrat-to",
                          url="x", actors=["Bundesrat"], enrich_actor="Bundesrat")
    top = RawItem(content_hash="h1", source_id=to_cfg.id, url="https://example.org/to",
                  title="TOP 14: Verordnung zu Exportkontrollen", start="2026-09-25")
    accept_item(store, top, to_cfg, actor="human:test")

    after = store.get(skeleton.uid)
    assert after.sync_authorized is True
    assert "TOP 14" in (after.note or "")


def test_enrichment_does_not_overrule_a_human_calendar_off(tmp_path):
    """Otherwise the dashboard toggle silently stops meaning anything the next
    time an agenda item lands on that day."""
    from china_calendar.gate import accept_item, event_from_raw
    from china_calendar.models import RawItem
    from china_calendar.sources.base import SourceConfig

    store = _gate_store(tmp_path)
    cfg = SourceConfig(id="bundesrat-plenum-2026", tier=1, kind="ics", url="x",
                       actors=["Bundesrat"], calendar_default_off=True)
    skeleton = event_from_raw(RawItem(content_hash="h0", source_id=cfg.id,
                                      title="Plenarsitzung", start="2026-09-25"), cfg)
    store.add(skeleton, actor="human:test")
    # On, then off — the path that leaves a trace. `amend` skips no-op writes,
    # so a human re-affirming an already-off record records nothing and cannot
    # be distinguished from the automatic off. Accepted: the dashboard already
    # shows such a record as off, so there is nothing to click, and the worst
    # outcome is a sitting appearing because it genuinely has a China TOP.
    store.amend(skeleton.uid, {"sync_authorized": True}, actor="human:webui",
                reason="wanted it visible")
    store.amend(skeleton.uid, {"sync_authorized": False}, actor="human:webui",
                reason="not interested after all")

    to_cfg = SourceConfig(id="bundesrat-tagesordnung", tier=1, kind="html:bundesrat-to",
                          url="x", actors=["Bundesrat"], enrich_actor="Bundesrat")
    accept_item(store, RawItem(content_hash="h1", source_id=to_cfg.id,
                               url="https://example.org/to", title="TOP 9: Handel",
                               start="2026-09-25"), to_cfg, actor="human:test")

    assert store.get(skeleton.uid).sync_authorized is False


def test_only_containers_are_enrichment_targets(tmp_path, monkeypatch):
    """Review finding: SOTEU and the part-session containing it were both legal
    owners of 16 September, and uid alphabetical order decided. An agenda item
    can never be the home for another agenda item."""
    from china_calendar.config import Config
    from china_calendar import gate as gate_mod
    from china_calendar.models import Event, Provenance, SourceRef, Status
    from china_calendar.sources.base import SourceConfig
    from china_calendar.store import Store

    monkeypatch.setattr(gate_mod, "_skeleton_uid_prefixes",
                        lambda: ("pc-ep-plenary-ical-",))
    store = Store(Config(store_dir=tmp_path / "store"))

    def add(uid, title, start, end):
        store.add(Event(uid=uid, title_en=title, start=start, end=end, tier=1,
                        status=Status.scheduled, provenance=Provenance.feed,
                        actors=["European Parliament"],
                        sources=[SourceRef(evidence="e")]), actor="human:test")

    # the container, and a leaf that sorts EARLIER alphabetically
    add("pc-ep-plenary-ical-abc123", "EP - Plenary (September part-session)",
        "2026-09-14", "2026-09-17")
    add("pc-ep-opendata-agenda-zzz", "EP plenary key debate: State of the Union",
        "2026-09-16", "2026-09-16")

    cfg = SourceConfig(id="ep-opendata-items", tier=1, kind="api:ep-opendata-items",
                       url="x", enrich_actor="European Parliament")
    item = RawItem(content_hash="h", source_id=cfg.id, title="a vote",
                   start="2026-09-16", end="2026-09-16")
    target = gate_mod._enrich_target(store, item, cfg)
    assert target.uid == "pc-ep-plenary-ical-abc123", \
        "an agenda item must attach to the sitting, never to another agenda item"


def test_shipped_skeleton_sources_are_the_sitting_calendars():
    """A new sitting calendar added without skeleton: true silently becomes
    un-enrichable."""
    from china_calendar.sources.base import load_sources
    skeletons = {c.id for c in load_sources() if c.skeleton}
    assert skeletons == {
        "bundesrat-plenum-2026", "bundesrat-plenum-2027",
        "bundestag-sitzungskalender", "bundestag-sitzungskalender-2027",
        "ep-plenary-ical",
    }
