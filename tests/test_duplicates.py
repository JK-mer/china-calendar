"""Cross-source duplicate suggestion (#68). The pairs below are the real ones
from the live triage queue on 2026-08-11 — the first implementation got three
of eight wrong, and every case here is one it got wrong or nearly did."""

from datetime import date
from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.duplicates import find_candidate, score
from china_calendar.gate import _corroborate, triage_decide
from china_calendar.models import (Decision, Event, Provenance, RawItem,
                                       SourceRef, Status)
from china_calendar.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def add_event(store, uid, title, start, end=None, sources=None):
    store.add(Event(
        uid=uid, title_en=title, start=start, end=end or start, tier=3,
        status=Status.scheduled, provenance=Provenance.research,
        sources=sources or [SourceRef(evidence="per test fixture")],
    ), actor="human:test")


def raw(title, start, end=None, url="https://example.org/item", **kw):
    return RawItem(content_hash=f"h-{abs(hash((title, start)))}", source_id="pecc-events",
                   title=title, start=start, end=end, url=url,
                   verify_strings=["ev"], date_text="ev", **kw)


# ------------------------------------------------------- scoring

def test_letters_in_common_are_not_evidence():
    """The first implementation scored these 0.47 on sequence similarity with
    not one word in common, and suggested attaching an APEC finance meeting to
    an ASEAN defence meeting."""
    value, shared, _ = score("APEC Finance Ministerial Meeting (FMM)",
                             "ADMM-Plus (ASEAN Defence Ministers' Meeting-Plus), Philippines")
    assert shared == 0
    assert value == 0.0


def test_a_single_generic_word_is_not_a_match():
    """'Summit' is shared by half the calendar."""
    _, shared, _ = score("The 49th ASEAN Summit and Related Meetings",
                         "COP31 World Leaders' Summit, Antalya")
    assert shared == 0  # "summit" is noise


def test_letter_digit_runs_split_so_cop31_matches_cop_31():
    value, shared, _ = score("2026 UN Climate Change Conference (UNFCCC COP 31)",
                             "COP31 (UNFCCC), Antalya")
    assert shared >= 3 and value > 0.5


def test_decorated_title_still_matches():
    value, _, fully = score("APEC Economic Leaders' Meeting",
                            "APEC Economic Leaders' Meeting 2026, Shenzhen")
    assert fully and value == 1.0


# ------------------------------------------------------- candidate selection

def test_finds_the_right_event_not_merely_a_near_one(store):
    """SOTEU: the correct record and the containing part-session both share two
    words with the item. The first implementation picked the part-session."""
    add_event(store, "pc-soteu-2026", "State of the Union Address (SOTEU) 2026",
              "2026-09-16", "2026-09-16")
    add_event(store, "pc-ep-plenary-2026-09", "EP - Plenary (September part-session, Strasbourg)",
              "2026-09-14", "2026-09-17")
    found = find_candidate(store, raw("EP plenary key debate: State of the Union",
                                      "2026-09-16", "2026-09-16"))
    assert found["uid"] == "pc-soteu-2026"


def test_no_candidate_for_a_genuinely_new_event(store):
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting 2026, Shenzhen",
              "2026-11-18", "2026-11-19")
    assert find_candidate(store, raw("The 2026 Annual Meetings of the International "
                                     "Monetary Fund (IMF) and the World Bank Group (WBG)",
                                     "2026-10-12", "2026-10-18")) is None


def test_dates_must_overlap(store):
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting, Shenzhen",
              "2026-11-18", "2026-11-19")
    assert find_candidate(store, raw("APEC Economic Leaders' Meeting", "2027-11-18")) is None


def test_removed_events_are_not_suggested(store):
    add_event(store, "pc-gone", "APEC Economic Leaders' Meeting, Shenzhen", "2026-11-18")
    store.remove("pc-gone", reason="test", actor="human:test")
    assert find_candidate(store, raw("APEC Economic Leaders' Meeting", "2026-11-18")) is None


def test_an_already_attached_source_is_the_match_not_a_reason_to_skip(store):
    """Skipping it (the first implementation) let a worse candidate win: the
    ASEAN summit matched its own record at 100%, was skipped for already
    carrying the same PECC url, and fell through to COP31's leaders' summit."""
    url = "https://www.pecc.org/event/798"
    add_event(store, "pc-asean-49", "49th ASEAN Summit and Related Summits, Manila",
              "2026-11-10", "2026-11-12", sources=[SourceRef(url=url, evidence="ev")])
    add_event(store, "pc-cop31-wls", "COP31 World Leaders' Summit, Antalya",
              "2026-11-11", "2026-11-12")
    found = find_candidate(store, raw("The 49th ASEAN Summit and Related Meetings",
                                      "2026-11-10", "2026-11-12", url=url))
    assert found["uid"] == "pc-asean-49"
    assert found["already_attached"] is True


# ------------------------------------------------------- corroboration

def test_corroborate_attaches_instead_of_creating(store):
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting 2026, Shenzhen",
              "2026-11-18", "2026-11-19")
    item = raw("APEC Economic Leaders' Meeting", "2026-11-18", "2026-11-19")
    item.duplicate_of = find_candidate(store, item)
    store.save_raw(item)

    before = len(list(store.iter_events()))
    event = _corroborate(store, item, reason=None, actor="human:test")

    assert len(list(store.iter_events())) == before, "must not mint a second event"
    assert event.uid == "pc-apec-leaders-2026"
    assert any(s.url == item.url for s in store.get("pc-apec-leaders-2026").sources)
    assert store.get_raw(item.content_hash).event_uid == "pc-apec-leaders-2026"


def test_corroborate_without_a_candidate_is_refused(store):
    item = raw("Something new", "2026-11-18")
    store.save_raw(item)
    with pytest.raises(ValueError, match="no duplicate candidate"):
        _corroborate(store, item, reason=None, actor="human:test")


def test_corroborate_is_idempotent_on_the_source_list(store):
    url = "https://www.pecc.org/event/817"
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting 2026, Shenzhen",
              "2026-11-18", "2026-11-19", sources=[SourceRef(url=url, evidence="ev")])
    item = raw("APEC Economic Leaders' Meeting", "2026-11-18", "2026-11-19", url=url)
    item.duplicate_of = find_candidate(store, item)
    store.save_raw(item)
    _corroborate(store, item, reason=None, actor="human:test")
    assert len(store.get("pc-apec-leaders-2026").sources) == 1


def test_triage_decide_accepts_corroborate(store):
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting 2026, Shenzhen",
              "2026-11-18", "2026-11-19")
    item = raw("APEC Economic Leaders' Meeting", "2026-11-18", "2026-11-19")
    item.duplicate_of = find_candidate(store, item)
    store.save_raw(item)
    event = triage_decide(store, store.config, item.content_hash, "corroborate",
                          reason=None, actor="human:test")
    assert event.uid == "pc-apec-leaders-2026"


def test_corroborate_never_trains_the_classifier(store):
    """The whole reason #68 exists: a duplicate says nothing about relevance,
    and bucketing it as a reject teaches the gate that APEC leaders' meetings
    are irrelevant."""
    from china_calendar.gate import _fewshot_examples

    for decision in ("accept", "reject", "corroborate", "defer"):
        store.record_decision(Decision(
            content_hash=f"h-{decision}", source_id="pecc-events",
            title=f"APEC thing ({decision})", decision=decision,
            reason="test", actor="human:test",
        ))
    titles = [e["item"]["title"] for e in _fewshot_examples(store)]
    assert "APEC thing (accept)" in titles
    assert "APEC thing (reject)" in titles
    assert "APEC thing (corroborate)" not in titles
    assert "APEC thing (defer)" not in titles


def test_corroborate_refuses_a_removed_target(store):
    """The suggestion is a snapshot; the event may be removed between the sweep
    that computed it and the click."""
    add_event(store, "pc-apec-leaders-2026", "APEC Economic Leaders' Meeting 2026, Shenzhen",
              "2026-11-18", "2026-11-19")
    item = raw("APEC Economic Leaders' Meeting", "2026-11-18", "2026-11-19")
    item.duplicate_of = find_candidate(store, item)
    store.save_raw(item)
    store.remove("pc-apec-leaders-2026", reason="test", actor="human:test")
    with pytest.raises(ValueError, match="removed"):
        _corroborate(store, item, reason=None, actor="human:test")
