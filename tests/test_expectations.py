from datetime import date
from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.expectations import (evaluate, expected_periods,
                                             load_registry, summarise)
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def add(store, uid, title, start, end=None, status=Status.scheduled):
    store.add(Event(
        uid=uid, title_en=title, start=start, end=end or start,
        tier=3, status=status, provenance=Provenance.manual,
        sources=[SourceRef(evidence="per test fixture")],
    ), actor="human:test")


def item(name="Format", cluster="Test", **expect):
    return {"name": name, "cluster": cluster, "expect": expect}


# ------------------------------------------------------------ rule expansion

def test_per_month_rule_yields_one_period_per_listed_month():
    periods = expected_periods(
        {"rule": "every_n_months", "months": [2, 4, 6, 8, 10, 12]},
        date(2026, 1, 1), date(2026, 12, 31))
    assert periods == ["2026-02", "2026-04", "2026-06",
                       "2026-08", "2026-10", "2026-12"]


def test_annual_rule_yields_one_period_per_year_not_per_month():
    """The G20 drifts across Nov/Dec; listing both months must not invent a
    second summit."""
    periods = expected_periods(
        {"rule": "annual", "months": [10, 11, 12]},
        date(2026, 1, 1), date(2027, 12, 31))
    assert periods == ["2026", "2027"]


def test_annual_rule_skips_a_year_whose_months_fall_outside_the_window():
    periods = expected_periods({"rule": "annual", "months": [12]},
                               date(2026, 1, 1), date(2026, 3, 31))
    assert periods == []


def test_n_yearly_respects_the_cycle():
    periods = expected_periods(
        {"rule": "n_yearly", "every_years": 5, "anchor_year": 2022, "months": [10]},
        date(2025, 1, 1), date(2032, 12, 31))
    assert periods == ["2027", "2032"]


def test_irregular_never_expands():
    assert expected_periods({"rule": "irregular"}, date(2020, 1, 1),
                            date(2030, 1, 1)) == []


# ------------------------------------------------------------ evaluation

def test_missing_when_nothing_matches(store):
    result = evaluate(store, date(2027, 1, 1), date(2027, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="annual", months=[10],
                                     match=["Party Congress"])])
    assert result[0].verdict == "missing"
    assert [o.period for o in result[0].occurrences] == ["2027"]


def test_covered_when_a_stored_event_matches(store):
    add(store, "pc-x-2027", "21st Party Congress", "2027-10-14", "2027-10-20")
    result = evaluate(store, date(2027, 1, 1), date(2027, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="annual", months=[10],
                                     match=["Party Congress"])])
    assert result[0].verdict == "covered"
    assert result[0].occurrences[0].events[0]["uid"] == "pc-x-2027"


def test_partial_when_only_some_periods_are_covered(store):
    add(store, "pc-s-1", "NPCSC session", "2026-10-20", "2026-10-28")
    result = evaluate(store, date(2026, 10, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[10, 12],
                                     match=["NPCSC"])])
    assert result[0].verdict == "partial"
    assert summarise(result)["missing"][0]["periods"] == ["2026-12"]


def test_irregular_is_a_watch_never_a_miss(store):
    """The whole point of `irregular`: a registry that is permanently
    half-red gets ignored."""
    result = evaluate(store, date(2026, 1, 1), date(2027, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(name="EU-China Summit", rule="irregular",
                                     match=["EU-China Summit"])])
    assert result[0].verdict == "watch"
    out = summarise(result)
    assert out["missing"] == []
    assert out["standing_watch"][0]["format"] == "EU-China Summit"


def test_irregular_reports_last_seen_from_outside_the_window(store):
    add(store, "pc-euchina-2025", "EU-China Summit", "2025-07-24")
    result = evaluate(store, date(2026, 1, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="irregular", match=["EU-China Summit"])])
    assert result[0].last_seen["uid"] == "pc-euchina-2025"


def test_stale_projection_is_flagged(store):
    """A projected window that closed with nothing promoting it: nothing else
    in the system notices, because expire_stale_triage only covers triage."""
    add(store, "pc-old-proj", "NPCSC session", "2026-06-19", "2026-06-30",
        status=Status.projected)
    result = evaluate(store, date(2026, 1, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[6],
                                     match=["NPCSC"])])
    assert result[0].stale_projections[0]["uid"] == "pc-old-proj"
    assert summarise(result)["stale_projections"][0]["format"] == "Format"


def test_promoted_projection_is_not_stale(store):
    add(store, "pc-ok", "NPCSC session", "2026-06-19", "2026-06-30",
        status=Status.confirmed)
    result = evaluate(store, date(2026, 1, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[6],
                                     match=["NPCSC"])])
    assert result[0].stale_projections == []


def test_future_projection_is_not_stale(store):
    add(store, "pc-future", "NPCSC session", "2026-10-19", "2026-10-31",
        status=Status.projected)
    result = evaluate(store, date(2026, 1, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[10],
                                     match=["NPCSC"])])
    assert result[0].stale_projections == []


def test_event_later_in_the_period_but_past_the_window_edge_still_covers(store):
    """A window ending 9 November still asks 'does November have a plenary?'.
    Judging coverage by the caller's edges manufactures a miss at every
    boundary — found live against the real store, 2026-08-11."""
    add(store, "pc-ep-nov", "EP - Plenary", "2026-11-23", "2026-11-26")
    result = evaluate(store, date(2026, 8, 11), date(2026, 11, 9),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[11],
                                     match=["EP - Plenary"])])
    assert result[0].verdict == "covered"


def test_annual_format_later_in_the_year_than_the_window_still_covers(store):
    """Same bug, annual flavour: the G20 sat in December while the window
    ended in November."""
    add(store, "pc-g20-2026", "G20 Leaders' Summit 2026, Miami", "2026-12-14")
    result = evaluate(store, date(2026, 8, 11), date(2026, 11, 9),
                      today=date(2026, 8, 11),
                      registry=[item(rule="annual", months=[10, 11, 12],
                                     match=["G20 Leaders"])])
    assert result[0].verdict == "covered"


def test_removed_events_do_not_count_as_coverage(store):
    add(store, "pc-gone", "21st Party Congress", "2027-10-14")
    store.remove("pc-gone", reason="test", actor="human:test")
    result = evaluate(store, date(2027, 1, 1), date(2027, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="annual", months=[10],
                                     match=["Party Congress"])])
    assert result[0].verdict == "missing"


# ------------------------------------------------------------ the real file

def test_shipped_registry_is_valid():
    from china_calendar.expectations import RULES
    registry = load_registry()
    assert registry, "glossary.yaml carries no expect: blocks"
    for entry in registry:
        expect = entry["expect"]
        assert expect.get("rule") in RULES, entry["name"]
        if expect["rule"] != "irregular":
            assert expect.get("months"), f"{entry['name']} has no months"
        assert expect.get("match"), f"{entry['name']} has no match strings"


def test_shipped_registry_expands_without_error():
    for entry in load_registry():
        expected_periods(entry["expect"], date(2026, 8, 1), date(2027, 12, 31))


# ------------------------------------------------------- review findings

def test_unknown_rule_is_a_watch_not_an_all_clear():
    """A typo (`rule: anual`) expanded to no occurrences, and no occurrences
    meant 'covered' — one misspelling silenced a format forever, in the tool
    whose whole job is noticing absence."""
    class EmptyStore:
        def search(self, **kw): return []
        def iter_events(self): return []

    result = evaluate(EmptyStore(), date(2026, 1, 1), date(2027, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(name="Typo", rule="anual", months=[10],
                                     match=["party congress"])])
    assert result[0].verdict == "watch"
    assert "CONFIG ERROR" in (result[0].note or "")
    assert summarise(result)["missing"] == []


def test_a_projection_that_expired_before_the_window_is_still_flagged(store):
    """The freshest stale projections are the ones just behind the window
    start; querying only from there excluded exactly those."""
    add(store, "pc-expired", "NPCSC session", "2026-07-19", "2026-07-31",
        status=Status.projected)
    result = evaluate(store, date(2026, 8, 1), date(2026, 12, 31),
                      today=date(2026, 8, 11),
                      registry=[item(rule="every_n_months", months=[8, 10, 12],
                                     match=["NPCSC"])])
    assert [s["uid"] for s in result[0].stale_projections] == ["pc-expired"]
