from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.gate import accept_item
from china_calendar.models import Event, Provenance, RawItem, SourceRef, Status
from china_calendar.sources.base import SourceConfig
from china_calendar.sources.bundesrat_to import parse_landing, parse_to_page
from china_calendar.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

TO_SOURCE = SourceConfig(
    id="bundesrat-tagesordnung", tier=1, kind="html:bundesrat-to",
    url="https://www.bundesrat.de/DE/plenum/to-plenum/to-plenum-node.html",
    enrich_actor="Bundesrat", actors=["Bundesrat"], sectors=["german_institutional"],
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def test_parse_landing_pairs_session_and_date():
    sessions = parse_landing((FIXTURES / "bundesrat-to.html").read_bytes())
    assert sessions, "no dated Tagesordnung link found on landing fixture"
    number, date_iso, url = sessions[0]
    assert number == "1067"
    assert date_iso == "2026-07-10"
    assert url.endswith("SharedDocs/TO/1067/tagesordnung-1067.html")


def test_parse_to_page_yields_unique_tops():
    items = list(parse_to_page(TO_SOURCE, "1067", "2026-07-10",
                               (FIXTURES / "bundesrat-to-1067.html").read_bytes()))
    assert len(items) >= 80  # 88 unique TOPs on the fixture
    assert len({i.content_hash for i in items}) == len(items)
    sample = items[0]
    assert sample.start == "2026-07-10"
    assert sample.title.startswith("TOP")
    assert sample.verify_strings and sample.url.startswith("https://www.bundesrat.de/SharedDocs/TO/1067/")


def make_skeleton(store):
    event = Event(
        uid="pc-bundesrat-plenum-2026-bundesrat-plenarsitzung-test01",
        title_de="Bundesrat: Plenarsitzung",
        start="2026-07-10T09:30:00+02:00", all_day=False,
        tier=1, status=Status.scheduled, provenance=Provenance.feed,
        actors=["Bundesrat"], sectors=["german_institutional"],
        sources=[SourceRef(url="https://example.org/ics", evidence="Plenarsitzung")],
    )
    store.add(event, actor="auto:whitelist")
    return event


def test_accepted_top_enriches_skeleton_event(store):
    skeleton = make_skeleton(store)
    item = RawItem(
        content_hash="abc123abc123abc123abc123", source_id=TO_SOURCE.id,
        title="TOP 14 (1067. Sitzung): 999/26 Gesetz über die Investitionsprüfung",
        start="2026-07-10", url="https://www.bundesrat.de/SharedDocs/TO/1067/tagesordnung-1067.html?topNr=14",
        date_text="1067. Plenarsitzung, 2026-07-10",
        verify_strings=["999/26 Gesetz über die Investitionsprüfung"],
    )
    event = accept_item(store, item, TO_SOURCE, actor="human:test", reason="China FDI angle")
    assert event.uid == skeleton.uid  # enriched, not duplicated
    assert len(list(store.iter_events())) == 1
    assert "TOP 14" in (event.note or "")
    assert len(event.sources) == 2  # corroborating source attached
    assert store.decision_for(item.content_hash).decision == "accept"

    # idempotent note: accepting a re-hashed duplicate does not double the line
    item2 = item.model_copy(deep=True)
    item2.content_hash = "def456def456def456def456"
    event = accept_item(store, item2, TO_SOURCE, actor="human:test")
    assert (event.note or "").count("TOP 14") == 1


def test_accept_falls_back_to_standalone_without_skeleton(store):
    item = RawItem(
        content_hash="fed789fed789fed789fed789", source_id=TO_SOURCE.id,
        title="TOP 3 (1068. Sitzung): 1001/26 Something relevant",
        start="2026-09-25", url="https://www.bundesrat.de/SharedDocs/TO/1068/tagesordnung-1068.html?topNr=3",
        verify_strings=["1001/26 Something relevant"],
    )
    event = accept_item(store, item, TO_SOURCE, actor="human:test")
    assert event.uid != ""
    assert len(list(store.iter_events())) == 1
