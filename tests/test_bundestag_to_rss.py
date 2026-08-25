"""Committee Tagesordnungen RSS parser (#11) against a live-captured fixture
(2026-08-03) whose titles cover the secretariats' format spread."""

from pathlib import Path

from china_calendar.sources.base import SourceConfig
from china_calendar.sources.bundestag_to_rss import parse_bundestag_to_rss

FIXTURES = Path(__file__).parent / "fixtures"

CFG = SourceConfig(
    id="bundestag-ausschuss-to",
    tier=1,
    kind="rss:bundestag-to",
    url="https://example.invalid/tagesordnungen.rss",
    actors=["Bundestag"],
    sectors=["german_institutional"],
)


def parse():
    return list(parse_bundestag_to_rss(CFG, (FIXTURES / "bundestag-to.rss").read_bytes()))


def test_parses_dated_sittings_and_skips_undated():
    items = parse()
    # fixture holds 15 RSS items; two carry no date and must be skipped:
    # "Parlament: Tagesordnung komplett" and a dateless Änderungsmitteilung
    # (whose base TO item carries the sitting's date)
    assert len(items) == 13
    assert not any("Parlament" in i.title for i in items)


def test_title_format_spread():
    items = parse()
    by_desc = {i.description: i for i in items}
    # plain form with "dem" and dotted time
    inneres = by_desc["Inneres: 39. Sitzung am Dienstag, dem 4. August 2026, 13.00 Uhr - nicht öffentlich"]
    assert inneres.start == "2026-08-04T13:00:00"
    assert inneres.title == "Inneres: 39. Sitzung (Bundestag-Ausschuss)"
    # verbose form ("Tagesordnung der N. Sitzung des ... Ausschusses")
    ausw = next(i for i in items if "28. Sitzung des Auswärtigen" in i.description)
    assert ausw.start == "2026-07-10T14:00:00"
    assert ausw.external_id == "Auswärtiges|28"
    # "(Stand: 10.07.2026, 11:30 Uhr)" must NOT hijack date or time
    vert = next(i for i in items if "Verteidigungsausschusses" in i.description)
    assert vert.start == "2026-07-10T13:00:00"
    # Mitteilung counter must not win over the session number
    haushalt = next(i for i in items if i.description.startswith("Haushalt"))
    assert haushalt.external_id == "Haushalt|44"
    # "(alt 36.)" renumbering: current number wins
    arbeit = next(i for i in items if i.description.startswith("Arbeit"))
    assert arbeit.external_id == "Arbeit, Soziales|35"


def test_ergaenzung_shares_the_sitting_identity():
    items = parse()
    inneres = [i for i in items if i.external_id == "Inneres|39"]
    assert len(inneres) == 2  # TO + Ergänzung
    # distinct gate items (own ledger decisions) but the same event identity
    assert len({i.content_hash for i in inneres}) == 2
    assert len({i.title for i in inneres}) == 1
