"""PECC event calendar, two-hop JSON-LD (#39). Fixtures captured live from
the NUC 2026-08-11."""

from pathlib import Path

import pytest

from china_calendar.sources.base import SourceConfig
from china_calendar.sources.pecc import fetch_pecc, parse_event, parse_listing

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "pecc-upcoming.html").read_bytes()
EVENT = (FIXTURES / "pecc-event-798.html").read_bytes()
EVENT_URL = ("https://www.pecc.org/event-calendar/upcoming-events/event/"
             "798-the-49th-asean-summit-and-related-meetings")


@pytest.fixture
def cfg() -> SourceConfig:
    return SourceConfig(
        id="pecc-events", tier=2, kind="html:pecc",
        url="https://www.pecc.org/event-calendar/upcoming-events",
    )


def test_listing_yields_absolute_deduplicated_event_urls():
    urls = parse_listing(LISTING, "https://www.pecc.org/event-calendar/upcoming-events")
    assert len(urls) == len(set(urls))
    assert all(u.startswith("https://www.pecc.org/") for u in urls)
    assert EVENT_URL in urls
    # Eight upcoming events on the captured page; the nav links to
    # /past-events and /general-meetings must not come through.
    assert all("/upcoming-events/event/" in u for u in urls)
    assert len(urls) == 8


def test_event_page_yields_one_item_with_iso_dates(cfg):
    items = list(parse_event(cfg, EVENT, EVENT_URL))
    assert len(items) == 1
    item = items[0]
    assert item.title == "The 49th ASEAN Summit and Related Meetings"
    assert item.start == "2026-11-10"
    assert item.end == "2026-11-12"
    assert item.location == "Philippines, Manila"


def test_the_spurious_json_ld_time_is_discarded(cfg):
    """startDate reads 2026-11-10T11:20:00+08:00 — a CMS publication stamp,
    not an 11:20 start. Carrying it through would invent a start time."""
    item = next(iter(parse_event(cfg, EVENT, EVENT_URL)))
    assert "T" not in item.start and len(item.start) == 10
    assert "11:20" not in (item.date_text or "")


def test_url_and_verify_strings_point_at_the_detail_page(cfg):
    """House rule: verify_strings must be findable at the item's own url. The
    JSON-LD lives on the detail page, so url must not be the listing."""
    item = next(iter(parse_event(cfg, EVENT, EVENT_URL)))
    assert item.url == EVENT_URL
    assert item.external_id == EVENT_URL
    page = EVENT.decode("utf-8", errors="replace")
    for needle in item.verify_strings:
        assert needle in page, needle


def test_two_hop_fetch_walks_listing_then_details(cfg):
    fetched: list[str] = []

    class FakeResult:
        def __init__(self, content):
            self.content = content
            self.not_modified = False

    class FakeFetcher:
        def get(self, source_id, url, force=False, ignore_robots=False):
            fetched.append(url)
            return FakeResult(LISTING)

        def fetch_raw(self, url):
            fetched.append(url)
            return FakeResult(EVENT)

    items = list(fetch_pecc(cfg, FakeFetcher()))
    assert fetched[0] == cfg.url
    assert len(fetched) == 9          # one listing + eight detail pages
    assert len(items) == 8
    assert all(i.start and len(i.start) == 10 for i in items)


def test_malformed_json_ld_is_skipped_not_fatal(cfg):
    broken = b'<html><script type="application/ld+json">{not json</script></html>'
    assert list(parse_event(cfg, broken, EVENT_URL)) == []


def test_non_event_json_ld_is_ignored(cfg):
    other = (b'<html><script type="application/ld+json">'
             b'{"@context":"https://schema.org","@type":"Organization",'
             b'"name":"PECC","startDate":"2026-01-01"}</script></html>')
    assert list(parse_event(cfg, other, EVENT_URL)) == []
