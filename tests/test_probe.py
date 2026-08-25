"""Reachability probe for disabled sources (#2): the sweep must notice the
day a blocked source (bundestag.de WAF) becomes reachable again."""

from pathlib import Path

import pytest

from china_calendar.config import Config
from china_calendar.fetch import FetchError, FetchResult
from china_calendar.sources.base import SourceConfig
from china_calendar.store import Store
from china_calendar.sweep import probe_disabled_source, sweep_source

FIXTURES = Path(__file__).parent / "fixtures"

DISABLED = SourceConfig(
    id="bundestag-sitzungskalender",
    tier=1,
    kind="ics",
    url="",
    enabled=False,
    probe_url="https://example.invalid/sitzungskalender",
)


class FakeFetcher:
    def __init__(self, ok: bool = True, content: bytes = b""):
        self.ok = ok
        self.content = content
        self.calls: list[str] = []

    def fetch_raw(self, url: str) -> FetchResult:
        self.calls.append(url)
        if not self.ok:
            raise FetchError(f"{url}: connection reset")
        return FetchResult(content=self.content, status=200)

    def get(self, source_id: str, url: str, force: bool = False,
            ignore_robots: bool = False) -> FetchResult:
        return self.fetch_raw(url)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


def test_probe_unreachable_records_state(store):
    report = probe_disabled_source(store, FakeFetcher(ok=False), DISABLED)
    assert report["skipped"] == "disabled"
    assert report["probe"].startswith("unreachable")
    state = store.source_state(DISABLED.id)
    assert state.probe_ok is False
    assert state.last_probe


def test_probe_flags_transition_to_reachable_once(store):
    probe_disabled_source(store, FakeFetcher(ok=False), DISABLED)
    report = probe_disabled_source(store, FakeFetcher(ok=True), DISABLED)
    assert report["probe"] == "reachable"
    assert report["reachable_again"] is True
    assert store.source_state(DISABLED.id).probe_ok is True
    # steady state: still reachable, but no longer news
    again = probe_disabled_source(store, FakeFetcher(ok=True), DISABLED)
    assert "reachable_again" not in again


def test_probe_without_any_url_is_a_plain_skip(store):
    cfg = SourceConfig(id="no-url", tier=1, kind="ics", url="", enabled=False)
    fetcher = FakeFetcher()
    report = probe_disabled_source(store, fetcher, cfg)
    assert report == {"source": "no-url", "skipped": "disabled"}
    assert fetcher.calls == []


def test_probe_prefers_probe_url_over_fetch_url(store):
    fetcher = FakeFetcher(ok=True)
    probe_disabled_source(store, fetcher, DISABLED)
    assert fetcher.calls == [DISABLED.probe_url]


def test_enabled_sweep_clears_probe_state(store):
    probe_disabled_source(store, FakeFetcher(ok=True), DISABLED)
    assert store.source_state(DISABLED.id).probe_ok is True
    enabled = SourceConfig(
        id=DISABLED.id, tier=1, kind="ics", auto_accept=True,
        url="https://example.invalid/kalender.ics",
        actors=["Bundestag"], sectors=["german_institutional"],
    )
    ics = (FIXTURES / "bundesrat-2026.ics").read_bytes()
    sweep_source(store, store.config, FakeFetcher(ok=True, content=ics),
                 enabled, use_llm=False)
    state = store.source_state(DISABLED.id)
    assert state.probe_ok is None
    assert state.last_probe is None
