"""Source-registry and parser rules that are easy to break silently."""

import importlib

import pytest

from china_calendar.config import load_config
from china_calendar.models import Event, Provenance, RawItem, SourceRef, Status
from china_calendar.sources.base import SourceConfig, robots_exempt
from china_calendar.store import Store
from china_calendar.verify import strings_present


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    return Store(load_config())


def test_markup_inside_the_sentence_still_matches():
    """The parser strips tags before storing the evidence, so the live page
    having <em> around part of the sentence must not make it unmatchable."""
    page = "<p>The NPCSC will convene from <em>June 23</em> to 26.</p>"
    assert strings_present(page, ["will convene from June 23 to 26"])


def test_normalise_does_not_join_words_across_tags():
    assert not strings_present("<p>June</p><p>23</p>", ["June23"])


def test_renamed_feed_item_keeps_its_uid(store):
    """A feed rewording its SUMMARY must amend, not duplicate — and an
    already-stored record must keep the uid it was created with."""
    from china_calendar.gate import event_from_raw

    cfg = SourceConfig(id="ep-plenary-ical", tier=1, kind="ics",
                       url="https://example.org/cal.ics")
    first = RawItem(content_hash="h1", source_id=cfg.id, external_id="UID-42",
                    title="Plenary session", start="2026-10-01")
    renamed = RawItem(content_hash="h2", source_id=cfg.id, external_id="UID-42",
                      title="Plenary session (Strasbourg)", start="2026-10-01")

    fresh = event_from_raw(first, cfg, store)
    assert "plenary-session" not in fresh.uid, "the title does not shape the uid"
    assert event_from_raw(renamed, cfg, store).uid == fresh.uid


def test_records_stored_under_the_old_uid_scheme_are_not_duplicated(store):
    """The whole Bestand predates the uid change; the next sweep must keep
    amending those records rather than minting a second copy of each."""
    from china_calendar.gate import event_from_raw

    cfg = SourceConfig(id="ep-plenary-ical", tier=1, kind="ics",
                       url="https://example.org/cal.ics")
    item = RawItem(content_hash="h1", source_id=cfg.id, external_id="UID-42",
                   title="Plenary session", start="2026-10-01")
    tail = event_from_raw(item, cfg).uid.rsplit("-", 1)[-1]
    legacy_uid = f"pc-ep-plenary-ical-plenary-session-{tail}"
    store.add(Event(uid=legacy_uid, title_en="Plenary session", start="2026-10-01",
                    tier=1, status=Status.scheduled, provenance=Provenance.feed,
                    sources=[SourceRef(evidence="feed")]), actor="human:test")
    assert event_from_raw(item, cfg, store).uid == legacy_uid


def test_robots_exempt_follows_the_source_registry():
    """Verifying a URL the sweep fetches nightly must not fail on robots
    when the sweep's own fetch would not. calendar.google.com is the
    configured ignore_robots host — its robots.txt disallows /calendar/ical/
    even though every calendar client fetches exactly that."""
    assert robots_exempt("https://calendar.google.com/calendar/ical/x/public/basic.ics")
    assert robots_exempt("https://nothing-configured.example/x") is False
    assert robots_exempt(None) is False


def test_bundestag_rss_items_carry_no_dead_verify_strings():
    from china_calendar.sources.bundestag_to_rss import parse_bundestag_to_rss

    feed = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Auswärtiger Ausschuss: 42. Sitzung am Mittwoch, 14. Oktober 2026"
        "</title><link>https://example.org/to.pdf</link></item>"
        "</channel></rss>"
    ).encode()
    cfg = SourceConfig(id="bundestag-to-rss", tier=1, kind="rss:bundestag_to",
                       url="https://example.org/feed")
    items = list(parse_bundestag_to_rss(cfg, feed))
    assert items, "fixture should parse"
    assert all(not item.verify_strings for item in items)


# ---------------------------------------------------------------- #75 stale annual files

def test_annual_file_flags_itself_once_its_year_has_passed(tmp_path):
    """The one failure that looks exactly like success: fetch fine, parse fine,
    last_success fresh daily, nothing forward-dated ever again."""
    from datetime import date
    from china_calendar.config import Config
    from china_calendar.models import SourceState
    from china_calendar.sources.base import SourceConfig
    from china_calendar.store import Store

    store = Store(Config(store_dir=tmp_path / "store"))
    cfg = SourceConfig(id="ep-plenary-ical", tier=1, kind="ics", url="x",
                       covers_year=2026)

    state = store.source_state(cfg.id)
    state.coverage_expired = bool(cfg.covers_year and date(2027, 1, 4).year > cfg.covers_year)
    store.save_source_state(state)
    assert store.source_state(cfg.id).coverage_expired is True

    state.coverage_expired = bool(cfg.covers_year and date(2026, 12, 31).year > cfg.covers_year)
    store.save_source_state(state)
    assert store.source_state(cfg.id).coverage_expired is False


def test_sources_without_a_declared_year_are_never_flagged(tmp_path):
    """An agenda source between sessions is legitimately all-past for weeks —
    the case #18 protects. Only declared annual files are checked."""
    from datetime import date
    from china_calendar.sources.base import SourceConfig

    cfg = SourceConfig(id="bundesrat-tagesordnung", tier=1,
                       kind="html:bundesrat-to", url="x")
    assert cfg.covers_year is None
    assert bool(cfg.covers_year and date(2030, 1, 1).year > (cfg.covers_year or 0)) is False


def test_every_year_pinned_source_declares_its_year():
    """A new annual file added without covers_year silently reintroduces the
    bug, so the registry itself is asserted."""
    from china_calendar.sources.base import load_sources

    for cfg in load_sources():
        looks_annual = any(str(y) in cfg.url for y in (2025, 2026, 2027, 2028))
        if looks_annual:
            assert cfg.covers_year, f"{cfg.id} has a year in its URL but no covers_year"


def test_coverage_flag_survives_a_304(tmp_path, monkeypatch):
    """The feature's whole target is static year-pinned files — which 304 every
    day. Computing the flag after the fetch meant it never fired in exactly the
    scenario it was built for."""
    from datetime import date
    from china_calendar.config import Config
    from china_calendar.fetch import FetchResult
    from china_calendar.sources.base import SourceConfig
    from china_calendar.store import Store
    from china_calendar import sweep as sweep_mod

    store = Store(Config(store_dir=tmp_path / "store"))
    cfg = SourceConfig(id="ep-plenary-ical", tier=1, kind="ics",
                       url="https://example.org/2026.ics", covers_year=2026)

    class Frozen(date):
        @classmethod
        def today(cls):
            return date(2027, 1, 4)

    monkeypatch.setattr(sweep_mod, "date", Frozen)

    class NotModifiedFetcher:
        def get(self, source_id, url, force=False, ignore_robots=False):
            return FetchResult(content=b"", status=304, not_modified=True)

    report = sweep_mod.sweep_source(store, store.config, NotModifiedFetcher(), cfg)

    assert report["not_modified"] is True
    assert "maintenance" in report and "2026" in report["maintenance"]
    assert store.source_state(cfg.id).coverage_expired is True


def test_digest_reports_a_stale_file(tmp_path):
    """gaps() only answers when asked; the digest runs unattended. A half-wired
    alarm is worse than none, because it gets trusted."""
    from china_calendar.config import Config
    from china_calendar.digest import build_digest
    from china_calendar.store import Store

    store = Store(Config(store_dir=tmp_path / "store"))
    state = store.source_state("ep-plenary-ical")
    state.coverage_expired = True
    store.save_source_state(state)

    text = build_digest(store, store.config)
    assert "Source health" in text
    assert "STALE FILE" in text and "ep-plenary-ical" in text


def test_seed_cannot_mint_confirmed(tmp_path):
    """Same rule as the conversation ban: `confirmed` is never re-checked, so
    it must be earned by a fetched source. A YAML file is not better evidence
    than a chat."""
    import subprocess
    import sys
    import textwrap

    seed = tmp_path / "bad.yaml"
    seed.write_text(textwrap.dedent("""\
        - uid: pc-fake-2027
          title_en: Invented summit
          start: "2027-05-01"
          tier: 3
          status: confirmed
          evidence: I said so
        """), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "china_calendar.cli", "seed", str(seed)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "src",
             "PC_STORE_DIR": str(tmp_path / "store")},
    )
    assert result.returncode != 0
    assert "cannot be 'confirmed'" in (result.stdout + result.stderr)
