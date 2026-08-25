from pathlib import Path

from china_calendar.sources.base import SourceConfig
from china_calendar.sources.dates import parse_date_range
from china_calendar.sources.tier2.apa import parse_apa
from china_calendar.sources.tier2.oav import parse_oav
from china_calendar.verify import strings_present

FIXTURES = Path(__file__).parent / "fixtures"

OAV = SourceConfig(id="oav-veranstaltungen", tier=2, kind="html:oav",
                   url="https://www.oav.de/veranstaltungen", actors=["OAV"])
APA = SourceConfig(id="apa-events", tier=2, kind="html:apa",
                   url="https://asien-pazifik-ausschuss.de/en/events/", actors=["APA"])


def test_parse_date_range_german_forms():
    assert parse_date_range("9. September 2026, Berlin") == ("2026-09-09", None)
    assert parse_date_range("vom 29. bis 31. Oktober 2026 in Seoul") == ("2026-10-29", "2026-10-31")
    assert parse_date_range("29.–31. Oktober 2026") == ("2026-10-29", "2026-10-31")
    assert parse_date_range("vom 30. September bis 2. Oktober 2026") == ("2026-09-30", "2026-10-02")
    assert parse_date_range("29 October 2026") == ("2026-10-29", None)
    assert parse_date_range("no date here") == (None, None)


def test_oav_parser_finds_apk():
    items = list(parse_oav(OAV, (FIXTURES / "oav.html").read_bytes()))
    assert len(items) >= 4
    apk = [i for i in items if "Asien-Pazifik-Konferenz" in i.title]
    assert apk, [i.title for i in items]
    apk = apk[0]
    # the range comes from the teaser ("vom 29. bis 31. Oktober 2026"), not
    # the date line, which only carries the start
    assert apk.start == "2026-10-29"
    assert apk.end == "2026-10-31"
    assert apk.verify_strings and apk.url and apk.url != OAV.url


def test_apa_parser_reads_jsonld():
    items = list(parse_apa(APA, (FIXTURES / "apa.html").read_bytes()))
    assert len(items) >= 10  # includes historical APKs; sweep filters by date
    apk = [i for i in items if i.start == "2026-10-29"]
    assert apk, "APK 2026 missing from JSON-LD parse"
    assert apk[0].end == "2026-10-31"
    assert apk[0].location == "Seoul"
    assert "2026-10-29" in apk[0].verify_strings
    # JSON-LD descriptions carry embedded markup; it must not reach the store
    assert apk[0].description and "<" not in apk[0].description


def test_verify_strings_against_fixture():
    items = list(parse_apa(APA, (FIXTURES / "apa.html").read_bytes()))
    apk = [i for i in items if i.start == "2026-10-29"][0]
    content = (FIXTURES / "apa.html").read_text(encoding="utf-8", errors="replace")
    assert strings_present(content, apk.verify_strings)
    assert not strings_present(content, ["this string is definitely not on the page 12345"])


def test_verify_strings_oav_fixture():
    items = list(parse_oav(OAV, (FIXTURES / "oav.html").read_bytes()))
    content = (FIXTURES / "oav.html").read_text(encoding="utf-8", errors="replace")
    for item in items:
        assert strings_present(content, item.verify_strings), item.title


def test_zero_item_regression_guard():
    # An empty page must yield zero items, not crash — the sweep counts this
    # and raises the maintenance flag after consecutive zero runs.
    assert list(parse_oav(OAV, b"<html><body></body></html>")) == []
    assert list(parse_apa(APA, b"<html><body></body></html>")) == []


def test_parse_bdi_fixture():
    from china_calendar.sources.base import SourceConfig
    from china_calendar.sources.tier2.bdi import parse_bdi

    cfg = SourceConfig(id="bdi-veranstaltungen", tier=2, kind="html:bdi",
                       url="https://example.invalid/veranstaltungen",
                       sectors=["business_formats"], actors=["BDI"])
    items = list(parse_bdi(cfg, (FIXTURES / "bdi-veranstaltungen.html").read_bytes()))
    assert len(items) == 6  # the 7th .bdi-event block is an empty template node
    tag = next(i for i in items if "Energiesteuertag" in i.title)
    # soft hyphens stripped from the rendered title
    assert tag.title == "Deutscher Energiesteuertag 2026"
    assert tag.start == "2026-11-05" and tag.end == "2026-11-06"
    assert tag.location and "Berlin" in tag.location
    assert tag.url.startswith("https://bdi.eu/de/veranstaltungen/")
    assert all(i.start and i.start >= "2026-11" for i in items if i.start)


def test_parse_bundespraesident_fixture():
    from china_calendar.sources.tier2.bundespraesident import parse_bundespraesident

    cfg = SourceConfig(id="bundespraesident-termine", tier=2,
                       kind="html:bundespraesident",
                       url="https://www.bundespraesident.de/DE/reden-und-aktuelles/termine/termine_node.html",
                       sectors=["german_institutional"], actors=["Bundespraesident"])
    items = list(parse_bundespraesident(
        cfg, (FIXTURES / "bundespraesident-termine.html").read_bytes()))
    assert len(items) >= 15  # July recess + first September entries
    assert len({i.external_id for i in items}) == len(items)  # deduped by href
    alijew = next(i for i in items if "Aserbaidschan" in i.title)
    assert alijew.start == "2026-07-21"
    assert alijew.location == "Berlin"
    assert "Alijew" in (alijew.description or "")
    assert alijew.url.startswith("https://www.bundespraesident.de/SharedDocs/Termine/DE/")
    # date recoverable from the path alone (fallback), e.g. 260904-Buergerfest
    buergerfest = next(i for i in items if "rgerfest" in i.title)
    assert buergerfest.start == "2026-09-04"


def test_parse_ipu_elections_fixture():
    from china_calendar.sources.tier2.ipu_elections import parse_ipu_elections

    cfg = SourceConfig(id="ipu-elections", tier=2, kind="html:ipu-elections",
                       url="https://data.ipu.org/elections/", sectors=["elections"])
    items = list(parse_ipu_elections(
        cfg, (FIXTURES / "ipu-elections.html").read_bytes()))
    assert len(items) > 100  # most member parliaments carry an expected date
    # suspended parliaments are skipped
    assert not any("Afghanistan" in i.title for i in items)
    # a known fixed date: US House midterms
    us = next(i for i in items if "United States" in i.title and "Representatives" in i.title)
    assert us.start == "2026-11-03"
    assert us.external_id.startswith("United States")
    assert "United States of America" in us.verify_strings[0]
    # every yielded item has a parseable ISO start
    assert all(i.start and len(i.start) == 10 for i in items)
    # stable identity: same input, same hashes
    again = list(parse_ipu_elections(cfg, (FIXTURES / "ipu-elections.html").read_bytes()))
    assert [i.content_hash for i in items] == [i.content_hash for i in again]


def test_parse_wahltermine_fixture():
    from china_calendar.sources.tier2.wahltermine import parse_wahltermine

    cfg = SourceConfig(id="bundeswahlleiterin-termine", tier=2, kind="html:wahltermine",
                       url="https://example.invalid/wahltermine.html", sectors=["elections"])
    items = list(parse_wahltermine(cfg, (FIXTURES / "wahltermine.html").read_bytes()))
    assert len(items) >= 8
    lsa = next(i for i in items if "Sachsen-Anhalt" in i.title)
    assert lsa.title == "Landtagswahl Sachsen-Anhalt 2026"
    assert lsa.start == "2026-09-06"
    # year carries across the rowspan group: NRW is a 2027 row without a year cell
    nrw = next(i for i in items if "Nordrhein-Westfalen" in i.title)
    assert nrw.start == "2027-04-25"
    # "Herbst" placeholder rows are skipped until a real date is announced
    assert not any(i.title == "Landtagswahl Niedersachsen 2027" for i in items)


def test_parse_electionguide_presidency_only():
    from china_calendar.sources.tier2.electionguide import parse_electionguide

    cfg = SourceConfig(id="electionguide-presidency", tier=2, kind="html:electionguide",
                       url="https://www.electionguide.org/", sectors=["elections"])
    items = list(parse_electionguide(cfg, (FIXTURES / "electionguide.html").read_bytes()))
    assert items, "no presidential races parsed"
    assert all("presiden" in i.title.lower() for i in items)
    assert all(i.start and len(i.start) == 10 for i in items)
    zambia = next(i for i in items if "Zambia" in i.title)
    assert zambia.start == "2026-08-13"
    assert zambia.external_id.startswith("/elections/id/")
    assert zambia.url.startswith("https://www.electionguide.org/elections/id/")


def test_oav_and_bdi_do_not_store_a_date_the_detail_page_lacks():
    """#76: both lifted the date from the LISTING while url points at the
    DETAIL page. The nightly re-check reads an absent evidence string as the
    fabrication signal, so these drifted toward rumored for a parser defect."""
    from pathlib import Path
    from china_calendar.sources.base import SourceConfig
    from china_calendar.sources.tier2.bdi import parse_bdi
    from china_calendar.sources.tier2.oav import parse_oav

    fixtures = Path(__file__).parent / "fixtures"
    cases = [
        (parse_oav, SourceConfig(id="oav-veranstaltungen", tier=2, kind="html:oav",
                                 url="https://www.oav.de/veranstaltungen"),
         fixtures / "oav.html"),
        (parse_bdi, SourceConfig(id="bdi-veranstaltungen", tier=2, kind="html:bdi",
                                 url="https://bdi.eu/de/veranstaltungen"),
         fixtures / "bdi-veranstaltungen.html"),
    ]
    for parser, cfg, path in cases:
        items = list(parser(cfg, path.read_bytes()))
        assert items, cfg.id
        for item in items:
            assert item.verify_strings == [item.title], (
                f"{cfg.id}: only the title survives on the detail page")
