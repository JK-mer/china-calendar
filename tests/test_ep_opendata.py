"""EP Open Data API, KEY DEBATE items (#64). Fixtures captured live from the
NUC 2026-08-11."""

from datetime import date
from pathlib import Path

import pytest

from china_calendar.sources.base import SourceConfig
from china_calendar.sources.ep_opendata import (agenda_url, fetch_ep_opendata,
                                                    meetings_url, parse_agenda,
                                                    sittings_in_window)

FIXTURES = Path(__file__).parent / "fixtures"
MEETINGS = (FIXTURES / "ep-meetings-2026.json").read_bytes()
AGENDA = (FIXTURES / "ep-agenda-2026-09-16.json").read_bytes()
SITTING = "MTG-PL-2026-09-16"


@pytest.fixture
def cfg() -> SourceConfig:
    return SourceConfig(id="ep-opendata-agenda", tier=1, kind="api:ep-opendata",
                        url="https://data.europarl.europa.eu/api/v2/meetings")


def test_urls_pin_the_json_ld_representation():
    """Load-bearing: the API content-negotiates to RDF/XML and the fetcher
    sends no Accept header, so evidence stored against a bare URL could never
    match."""
    assert "format=application%2Fld%2Bjson" in meetings_url(2026)
    assert "format=application%2Fld%2Bjson" in agenda_url(SITTING)


def test_sittings_window_selects_only_the_horizon():
    sittings = sittings_in_window(MEETINGS, date(2026, 8, 11), horizon_days=60)
    assert sittings[:4] == ["MTG-PL-2026-09-14", "MTG-PL-2026-09-15",
                            "MTG-PL-2026-09-16", "MTG-PL-2026-09-17"]
    assert all(s > "MTG-PL-2026-08-10" for s in sittings)
    # 2026-12-14 is a real sitting but far outside a 60-day horizon.
    assert "MTG-PL-2026-12-14" not in sittings


def test_past_sittings_are_excluded():
    assert "MTG-PL-2026-01-19" not in sittings_in_window(MEETINGS, date(2026, 8, 11))


def test_key_debate_yields_soteu_with_the_parent_start_time(cfg):
    """The child carries the label, the parent carries the clock."""
    items = list(parse_agenda(cfg, AGENDA, SITTING))
    assert len(items) == 1
    item = items[0]
    assert item.title == "EP plenary key debate: State of the Union"
    assert item.start == "2026-09-16T09:00:00"
    assert item.start.startswith("2026-09-16")


def test_external_id_excludes_the_renumbering_agenda_point(cfg):
    """-OJ-ITM-D-63 renumbers as the draft firms up; keying on it would mint a
    second uid and orphan the first."""
    item = next(iter(parse_agenda(cfg, AGENDA, SITTING)))
    assert item.external_id == "MTG-PL-2026-09-16/state-of-the-union"
    assert "ITM" not in item.external_id and "D-63" not in item.external_id


def test_evidence_is_contiguous_and_present_in_the_document(cfg):
    """The per-language label map comes back in unstable key order, so no
    evidence string may span it."""
    item = next(iter(parse_agenda(cfg, AGENDA, SITTING)))
    assert item.verify_strings == ['"en":"State of the Union"']
    page = AGENDA.decode("utf-8")
    for needle in item.verify_strings:
        assert page.count(needle) == 1, needle


def test_ordinary_debates_and_votes_are_not_emitted(cfg):
    """65 agenda items across the part-session, exactly one KEY DEBATE. If
    this starts emitting votes, the scope decision has been lost."""
    items = list(parse_agenda(cfg, AGENDA, SITTING))
    assert len(items) == 1


def test_empty_204_body_is_normal_not_an_error(cfg):
    """Every sitting beyond the next part-session returns 204 with no body."""
    assert list(parse_agenda(cfg, b"", "MTG-PL-2026-12-14")) == []
    assert list(parse_agenda(cfg, b"   ", "MTG-PL-2026-12-14")) == []


def test_malformed_body_is_skipped_not_fatal(cfg):
    assert list(parse_agenda(cfg, b"{not json", SITTING)) == []


def test_fetch_asks_for_next_year_so_the_rollover_is_automatic(cfg):
    """The API held 2020-2026 and 204d for 2027; asking every run is what
    makes the year roll over on a sweep rather than needing a human."""
    asked: list[str] = []

    class FakeResult:
        def __init__(self, content):
            self.content = content
            self.not_modified = False

    class FakeFetcher:
        def get(self, source_id, url, force=False, ignore_robots=False):
            asked.append(url)
            return FakeResult(MEETINGS if "year=2026" in url else b"")

        def fetch_raw(self, url):
            asked.append(url)
            return FakeResult(AGENDA if SITTING in url else b"")

    items = list(fetch_ep_opendata(cfg, FakeFetcher(), today=date(2026, 8, 11)))
    assert any("year=2026" in u for u in asked)
    assert any("year=2027" in u for u in asked)
    # Four sitting days probed, only the one with a KEY DEBATE yields.
    assert len(items) == 1
    assert items[0].external_id == "MTG-PL-2026-09-16/state-of-the-union"


def test_204_from_either_hop_is_skipped_not_fatal(cfg):
    """Found live: the fetcher raises on 204, and 204 is the NORMAL answer
    both for next year's sittings and for any sitting past the next
    part-session. Without this the whole source errors out every run."""
    from china_calendar.fetch import NoContent

    class FakeResult:
        def __init__(self, content):
            self.content = content
            self.not_modified = False

    class FakeFetcher:
        def get(self, source_id, url, force=False, ignore_robots=False):
            if "year=2027" in url:
                raise NoContent("HTTP 204")
            return FakeResult(MEETINGS)

        def fetch_raw(self, url):
            if SITTING in url:
                return FakeResult(AGENDA)
            raise NoContent("HTTP 204")      # every other sitting day

    items = list(fetch_ep_opendata(cfg, FakeFetcher(), today=date(2026, 8, 11)))
    assert len(items) == 1
    assert items[0].external_id == "MTG-PL-2026-09-16/state-of-the-union"


# ------------------------------------------------------- #69 ordinary items

def test_items_parser_emits_debates_and_votes(cfg):
    from china_calendar.sources.ep_opendata import parse_agenda_items
    items = list(parse_agenda_items(cfg, AGENDA, SITTING))
    assert len(items) > 10, "the bulk of an agenda is votes and debates"
    kinds = {i.title.split(":")[0] for i in items}
    assert kinds == {"EP plenary vote", "EP plenary debate"}


def test_items_parser_excludes_the_key_debate(cfg):
    """SOTEU is a standalone timed event from parse_agenda; carrying it here
    too would put it in the calendar AND in the part-session's note."""
    from china_calendar.sources.ep_opendata import parse_agenda_items
    titles = [i.title for i in parse_agenda_items(cfg, AGENDA, SITTING)]
    assert not any("State of the Union" in t for t in titles)


def test_items_parser_skips_structural_containers(cfg):
    """MEETING_PART and the TF-HHMM time frames name no business."""
    from china_calendar.sources.ep_opendata import parse_agenda_items
    for item in parse_agenda_items(cfg, AGENDA, SITTING):
        assert "TF-" not in (item.external_id or "")
        assert item.start and len(item.start) == 10


def test_items_carry_a_verifiable_anchor(cfg):
    from china_calendar.sources.ep_opendata import parse_agenda_items
    page = AGENDA.decode("utf-8")
    for item in parse_agenda_items(cfg, AGENDA, SITTING):
        for needle in item.verify_strings:
            assert needle in page, needle
