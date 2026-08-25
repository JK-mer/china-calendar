from datetime import date
from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.inbox import process_inbox
from china_calendar.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def test_dropped_ics_goes_through_gate_and_is_archived(store):
    inbox = store.config.store_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "bundesrat-export.ics").write_bytes((FIXTURES / "bundesrat-2026.ics").read_bytes())

    reports = process_inbox(store, store.config, use_llm=False, since=date(2026, 8, 2))
    assert len(reports) == 1
    report = reports[0]
    assert report["inbox"] == "bundesrat-export.ics"
    assert report["items"] == 4  # future sessions only
    assert report["triage"] == 4  # never auto-accepted; human decides

    # file archived, not deleted
    assert not (inbox / "bundesrat-export.ics").exists()
    assert (inbox / "processed" / "bundesrat-export.ics").exists()

    # provenance: tier 0, manual, no fetchable URL
    from china_calendar.gate import pending_triage, triage_decide
    items = pending_triage(store)
    assert all(i.url is None and i.source_id == "inbox-bundesrat-export" for i in items)
    event = triage_decide(store, store.config, items[0].content_hash, "accept",
                          "from my own export", actor="human:test")
    assert event.tier == 0 and event.provenance.value == "manual"


def test_empty_or_missing_inbox_is_fine(store):
    assert process_inbox(store, store.config) == []
