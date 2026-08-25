"""NPC Observer Session Watch parser (#37) — fixture is the live feed of
2026-08-03, which carries two Session Watch posts (23rd and 22nd sessions)
plus monthly NPC Calendar posts that must be skipped."""

from pathlib import Path

from china_calendar.sources.base import SourceConfig
from china_calendar.sources.npcobserver import parse_npcobserver

FIXTURE = (Path(__file__).parent / "fixtures" / "npcobserver.xml").read_bytes()


def cfg() -> SourceConfig:
    return SourceConfig(id="npcobserver-sessions", tier=1,
                        kind="rss:npcobserver", url="https://npcobserver.com/feed/")


def test_extracts_both_session_watch_posts():
    items = list(parse_npcobserver(cfg(), FIXTURE))
    assert len(items) == 2  # calendar/comment posts skipped
    by_id = {i.external_id: i for i in items}
    s23 = by_id["npcsc|23"]
    assert s23.title == "NPCSC: 23rd session"
    assert s23.start == "2026-06-23" and s23.end == "2026-06-26"
    assert s23.verify_strings == ["session from June 23 to 26"]
    assert s23.url.startswith("https://npcobserver.com/")
    s22 = by_id["npcsc|22"]
    assert s22.start == "2026-04-27" and s22.end == "2026-04-30"


def test_cross_month_and_year_rollover():
    feed = """<?xml version="1.0"?>
    <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
    <item><title>NPCSC Session Watch: Year End</title>
      <link>https://npcobserver.com/x/</link>
      <pubDate>Mon, 21 Dec 2026 12:00:00 +0000</pubDate>
      <content:encoded>The NPCSC will convene for its 26th session from
      December 28 to January 2, as decided.</content:encoded></item>
    <item><title>NPCSC Session Watch: New Year</title>
      <link>https://npcobserver.com/y/</link>
      <pubDate>Tue, 22 Dec 2026 12:00:00 +0000</pubDate>
      <content:encoded>The NPCSC will convene for its 27th session from
      January 25 to 28, as decided.</content:encoded></item>
    </channel></rss>"""
    items = {i.external_id: i for i in parse_npcobserver(cfg(), feed.encode())}
    assert items["npcsc|26"].start == "2026-12-28"
    assert items["npcsc|26"].end == "2027-01-02"  # end month rolls the year
    assert items["npcsc|27"].start == "2027-01-25"  # announced in Dec for Jan


def test_post_without_date_range_is_skipped():
    feed = """<?xml version="1.0"?>
    <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
    <item><title>NPCSC Session Watch: undated</title>
      <link>https://npcobserver.com/z/</link>
      <pubDate>Mon, 03 Aug 2026 12:00:00 +0000</pubDate>
      <content:encoded>The NPCSC will convene in late August; the Council of
      Chairpersons has not yet set the dates.</content:encoded></item>
    </channel></rss>"""
    assert list(parse_npcobserver(cfg(), feed.encode())) == []


def test_superscript_ordinal_markup_survives():
    feed = """<?xml version="1.0"?>
    <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
    <item><title>NPCSC Session Watch: markup</title>
      <link>https://npcobserver.com/m/</link>
      <pubDate>Tue, 16 Jun 2026 12:00:00 +0000</pubDate>
      <content:encoded>&lt;p&gt;will convene for its 24&lt;sup&gt;th&lt;/sup&gt;
      session from August 25 to 29, the Council decided.&lt;/p&gt;</content:encoded>
    </item></channel></rss>"""
    items = list(parse_npcobserver(cfg(), feed.encode()))
    assert items and items[0].external_id == "npcsc|24"
    assert items[0].start == "2026-08-25"
