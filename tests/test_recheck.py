"""Re-check semantics: unreachable is not evidence of fabrication."""

import pytest

from china_calendar.config import load_config
from china_calendar.fetch import FetchError, FetchResult
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store
from china_calendar.sweep import RECHECK_DEMOTE_AFTER, recheck_unverified


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    return Store(load_config())


class WalledFetcher:
    """Stands in for unfccc.int, which answers our user agent with a wall."""

    def fetch_raw(self, url, ignore_robots=None):
        raise FetchError(f"HTTP 403 for {url}")


class MismatchFetcher:
    def fetch_raw(self, url, ignore_robots=None):
        return FetchResult(content=b"a page that says nothing of the sort", status=200)


class MatchFetcher:
    def fetch_raw(self, url, ignore_robots=None):
        return FetchResult(content="COP31 opens on 9 November 2026".encode(), status=200)


def seed(store, uid="pc-cop31-2026"):
    return store.add(Event(
        uid=uid, title_en="COP31", start="2026-11-09", tier=3,
        status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(url="https://unfccc.int/cop31",
                           evidence="COP31 opens on 9 November 2026",
                           verify_strings=["COP31 opens on 9 November 2026"])],
    ), actor="mcp")


def test_unreachable_source_never_demotes_a_real_event(store):
    event = seed(store)
    for _ in range(RECHECK_DEMOTE_AFTER + 3):
        report = recheck_unverified(store, WalledFetcher())
    after = store.get(event.uid)
    assert after.status is Status.unverified, "a bot wall is not a fabrication"
    assert report["unreachable"] == 1
    assert report["demoted"] == 0
    assert all(h.field != "recheck_failed" for h in after.history)


def test_fetched_page_without_the_evidence_still_demotes(store):
    event = seed(store)
    for _ in range(RECHECK_DEMOTE_AFTER):
        recheck_unverified(store, MismatchFetcher())
    assert store.get(event.uid).status is Status.rumored


def test_unreachable_runs_do_not_count_toward_a_later_demotion(store):
    """The counters are separate: a week of walls followed by one real
    mismatch must not add up to a demotion."""
    event = seed(store)
    for _ in range(5):
        recheck_unverified(store, WalledFetcher())
    recheck_unverified(store, MismatchFetcher())
    assert store.get(event.uid).status is Status.unverified
    recheck_unverified(store, MismatchFetcher())
    assert store.get(event.uid).status is Status.rumored


def test_persistently_unreachable_source_backs_off(store):
    from china_calendar.sweep import UNREACHABLE_BACKOFF_AFTER

    event = seed(store)
    for _ in range(UNREACHABLE_BACKOFF_AFTER):
        recheck_unverified(store, WalledFetcher())
    report = recheck_unverified(store, WalledFetcher())
    assert report["resting"] == 1 and report["checked"] == 0


def test_evidence_found_on_re_check_promotes(store):
    event = seed(store)
    report = recheck_unverified(store, MatchFetcher())
    assert report["promoted"] == 1
    assert store.get(event.uid).status is Status.scheduled


# ---------------------------------------------------------------- #55 punctuation

def test_markup_abutting_punctuation_still_matches():
    """Found live on Pre-COP31: the page has `in <strong>Nadi, Fiji</strong>.`
    which tag-stripping turns into `Nadi, Fiji .`, while evidence copied from
    the rendered page has the period flush. The two never matched."""
    from china_calendar.verify import strings_present
    page = "The formal Pre-COP31 runs from 5 to 8 October 2026 in <strong>Nadi, Fiji</strong>."
    assert strings_present(page, ["in Nadi, Fiji."])
    assert strings_present(page, ["8 October 2026 in Nadi, Fiji."])


def test_punctuation_collapse_is_symmetric():
    """Both sides normalise identically, so stored evidence that itself came
    from markup still matches a clean page."""
    from china_calendar.verify import strings_present
    assert strings_present("Program, and more", ["<em>Program</em> , and more"])
    assert strings_present("a <b>list</b>: one; two!", ["list: one; two!"])


def test_collapse_does_not_join_separate_words():
    """Only whitespace BEFORE punctuation goes; ordinary spacing is untouched,
    or evidence would match text that does not actually contain it."""
    from china_calendar.verify import strings_present
    assert not strings_present("Nadi Fiji October", ["NadiFiji"])
    assert strings_present("Nadi, Fiji", ["Nadi, Fiji"])
