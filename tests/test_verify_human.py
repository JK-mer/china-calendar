"""Human verification and source correction (issue #33).

Status stays a function of provenance, assigned in the core: a literal string
match on a fresh fetch, or a human's own statement — never the caller's word
alone. Sources are append-only; promotion never downgrades.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from china_calendar.config import Config
from china_calendar.fetch import FetchError
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store
from china_calendar.verify import human_verify, source_verify


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


class FakeFetcher:
    def __init__(self, text: str = "", fail: bool = False):
        self.text, self.fail = text, fail

    def fetch_raw(self, url: str):
        if self.fail:
            raise FetchError("connection refused")
        return SimpleNamespace(text=self.text)


def make_unverified(uid="pc-cop31-2026", tier=3) -> Event:
    return Event(
        uid=uid, title_en="COP31", start="2026-11-09", end="2026-11-20",
        tier=tier, status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(url="https://example.org/cop31", evidence="wrong string",
                           verify_strings=["wrong string"])],
    )


def test_human_verify_promotes_and_appends(store):
    store.add(make_unverified(), actor="human:test")
    event, _ = human_verify(store, "pc-cop31-2026",
                            "checked unfccc.int myself, 9 November 2026",
                            actor="human:webui")
    assert event.status is Status.scheduled
    assert len(event.sources) == 2  # append-only: the wrong source stays
    added = event.sources[-1]
    assert added.url is None and added.verified_at and not added.verify_strings
    assert any(h.field == "sources" for h in event.history)
    assert any(h.field == "status" and h.to == "scheduled" for h in event.history)


def test_human_verify_official_confirms(store):
    store.add(make_unverified(), actor="human:test")
    event, note = human_verify(
        store, "pc-cop31-2026",
        "the official UNFCCC page states 9 November 2026",
        actor="human:webui", official=True)
    assert event.status is Status.confirmed and note is None


def test_official_without_a_date_in_the_evidence_caps_at_scheduled(store):
    """Confirmed is the status nothing ever re-checks, so it has to be
    earned by evidence that actually states the date."""
    store.add(make_unverified(), actor="human:test")
    event, note = human_verify(store, "pc-cop31-2026", "official UNFCCC page",
                               actor="human:webui", official=True)
    assert event.status is Status.scheduled
    assert "cannot confirm" in note
    assert any(h.field == "official_declined" for h in event.history)


def test_human_verify_never_downgrades(store):
    store.add(make_unverified(), actor="human:test")
    human_verify(store, "pc-cop31-2026", "official, 9 November 2026",
                 actor="human:webui", official=True)
    event, _ = human_verify(store, "pc-cop31-2026", "second reading",
                            actor="human:webui")
    assert event.status is Status.confirmed  # not pulled back to scheduled


def test_human_verify_requires_evidence(store):
    store.add(make_unverified(), actor="mcp")
    with pytest.raises(ValueError):
        human_verify(store, "pc-cop31-2026", "   ", actor="human:webui")


def test_human_verify_tier0_allowed_for_humans(store):
    event = make_unverified(uid="pc-manual-2026", tier=0)
    store.add(event, actor="human:test")
    verified, _ = human_verify(store, "pc-manual-2026", "I said so",
                               actor="human:webui")
    assert verified.status is Status.scheduled


def test_source_verify_match_promotes(store):
    store.add(make_unverified(), actor="mcp")
    fetcher = FakeFetcher(text="COP 31 will be held from 9 to 20 November 2026.")
    event, matched, note = source_verify(
        store, fetcher, "pc-cop31-2026", "https://unfccc.int/cop31",
        "from 9 to 20 November 2026", actor="human:webui")
    assert matched and note is None
    assert event.status is Status.scheduled
    added = event.sources[-1]
    assert added.verified_at and added.verify_strings == ["from 9 to 20 November 2026"]


def test_source_verify_official_confirms(store):
    store.add(make_unverified(), actor="mcp")
    fetcher = FakeFetcher(text="from 9 to 20 November 2026")
    event, matched, _ = source_verify(
        store, fetcher, "pc-cop31-2026", "https://unfccc.int/cop31",
        "from 9 to 20 November 2026", actor="mcp", official=True)
    assert matched and event.status is Status.confirmed


def test_source_verify_mismatch_attaches_but_keeps_status(store):
    store.add(make_unverified(), actor="mcp")
    fetcher = FakeFetcher(text="a page that says something else entirely")
    event, matched, note = source_verify(
        store, fetcher, "pc-cop31-2026", "https://unfccc.int/cop31",
        "from 9 to 20 November 2026", actor="human:webui")
    assert not matched and "NOT found" in note
    assert event.status is Status.unverified
    added = event.sources[-1]
    # attached for the nightly sweep to re-check, retrieved but not verified
    assert added.retrieved_at and not added.verified_at
    assert added.verify_strings == ["from 9 to 20 November 2026"]


def test_source_verify_fetch_failure_attaches_unretrieved(store):
    store.add(make_unverified(), actor="mcp")
    event, matched, note = source_verify(
        store, FakeFetcher(fail=True), "pc-cop31-2026",
        "https://unfccc.int/cop31", "some evidence", actor="human:webui")
    assert not matched and "fetch failed" in note
    assert event.status is Status.unverified
    assert event.sources[-1].retrieved_at is None


def test_source_verify_requires_url_and_evidence(store):
    store.add(make_unverified(), actor="mcp")
    with pytest.raises(ValueError):
        source_verify(store, FakeFetcher(), "pc-cop31-2026", "", "evidence",
                      actor="human:webui")
    with pytest.raises(ValueError):
        source_verify(store, FakeFetcher(), "pc-cop31-2026",
                      "https://example.org", "  ", actor="human:webui")


def test_source_verify_dedupes_identical_source(store):
    store.add(make_unverified(), actor="mcp")
    fetcher = FakeFetcher(text="page says wrong string exists here")
    # same url+evidence as the event's existing source → re-check, no append
    event, matched, _ = source_verify(
        store, fetcher, "pc-cop31-2026", "https://example.org/cop31",
        "wrong string", actor="human:webui")
    assert matched
    assert len(event.sources) == 1  # refreshed in place, not duplicated
    assert event.sources[0].verified_at
    assert event.status is Status.scheduled
    assert any(h.field == "source_recheck" and h.to == "matched"
               for h in event.history)


def test_source_verify_dedupe_failed_recheck_keeps_old_verified_at(store):
    store.add(make_unverified(), actor="mcp")
    event, matched, note = source_verify(
        store, FakeFetcher(text="something else"), "pc-cop31-2026",
        "https://example.org/cop31", "wrong string", actor="human:webui")
    assert not matched and len(event.sources) == 1
    assert event.sources[0].verified_at is None
    assert event.status is Status.unverified


def test_recheck_stamps_and_history_commit_together(tmp_path, monkeypatch):
    """#55: as two writes, a failure between them left refreshed stamps with
    nothing on record saying why they moved — indistinguishable from a
    verification nobody performed."""
    from china_calendar.config import Config
    from china_calendar.models import Event, Provenance, SourceRef, Status
    from china_calendar.store import Store
    from china_calendar.verify import source_verify

    store = Store(Config(store_dir=tmp_path / "store"))
    url, evidence = "https://example.org/x", "meets on 5 March 2027"
    store.add(Event(
        uid="pc-recheck-2027", title_en="Thing", start="2027-03-05", end="2027-03-05",
        tier=3, status=Status.scheduled, provenance=Provenance.research,
        sources=[SourceRef(url=url, evidence=evidence, verify_strings=[evidence])],
    ), actor="human:test")

    class FakeResult:
        text = "The body meets on 5 March 2027 in Beijing."

    class FakeFetcher:
        def fetch_raw(self, url, ignore_robots=None):
            return FakeResult()

    event, matched, _ = source_verify(store, FakeFetcher(), "pc-recheck-2027",
                                      url, evidence, actor="human:test")
    assert matched
    stored = store.get("pc-recheck-2027")
    # exactly one source (refreshed, not duplicated) and the check recorded
    assert len(stored.sources) == 1
    assert stored.sources[0].verified_at
    rechecks = [h for h in stored.history if h.field == "source_recheck"]
    assert len(rechecks) == 1 and rechecks[0].to == "matched"


def test_recheck_history_is_not_written_without_the_stamps(tmp_path):
    """The other direction of the same invariant: one call, one write."""
    from china_calendar.config import Config
    from china_calendar.models import Event, HistoryEntry, Provenance, SourceRef, Status
    from china_calendar.store import Store

    store = Store(Config(store_dir=tmp_path / "store"))
    store.add(Event(
        uid="pc-atomic-2027", title_en="Thing", start="2027-03-05", end="2027-03-05",
        tier=3, status=Status.scheduled, provenance=Provenance.research,
        sources=[SourceRef(evidence="e")],
    ), actor="human:test")

    event = store.get("pc-atomic-2027")
    event.sources[0].verified_at = "2026-08-11T00:00:00+00:00"
    store.attach_verification(event, history=HistoryEntry(
        field="source_recheck", to="matched", actor="human:test", reason="r"))

    stored = store.get("pc-atomic-2027")
    assert stored.sources[0].verified_at == "2026-08-11T00:00:00+00:00"
    assert [h.field for h in stored.history].count("source_recheck") == 1


def test_evidence_that_normalises_to_nothing_is_refused(tmp_path):
    """"&nbsp;" is non-empty raw but normalises to "", and "" is a substring of
    every page — such evidence would stamp verified_at and promote."""
    import pytest
    from china_calendar.config import Config
    from china_calendar.models import Event, Provenance, SourceRef, Status
    from china_calendar.store import Store
    from china_calendar.verify import source_verify

    store = Store(Config(store_dir=tmp_path / "store"))
    store.add(Event(uid="pc-empty-2027", title_en="T", start="2027-03-05",
                    end="2027-03-05", tier=3, status=Status.unverified,
                    provenance=Provenance.research,
                    sources=[SourceRef(evidence="e")]), actor="human:test")

    class FakeFetcher:
        def fetch_raw(self, url, ignore_robots=None):
            raise AssertionError("must be refused before any fetch")

    for junk in ("&nbsp;", "<br/>", "   "):
        with pytest.raises(ValueError):
            source_verify(store, FakeFetcher(), "pc-empty-2027",
                          "https://example.org/x", junk, actor="human:test")
