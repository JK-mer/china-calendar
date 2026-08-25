"""TOP extraction orchestration (#22): PDF text and model are faked; the
tests pin down selection → note append, per-PDF idempotency, past-event and
failure handling."""

from datetime import date
from pathlib import Path

import pytest

import china_calendar.top_extract as tx
from china_calendar.config import Config
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store

TODAY = date(2026, 8, 3)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(Config(store_dir=tmp_path / "store"))


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def fetch_raw(self, url):
        self.calls.append(url)
        class R:  # only .content is used
            content = b"%PDF-fake"
        return R()


def sitting(uid_tail: str, start: str, pdf: str) -> Event:
    return Event(
        uid=f"pc-bundestag-ausschuss-to-{uid_tail}",
        title_de="Auswärtiges: 29. Sitzung (Bundestag-Ausschuss)",
        start=start, all_day=len(start) == 10, tier=1,
        status=Status.scheduled, provenance=Provenance.feed,
        actors=["Bundestag"],
        sources=[SourceRef(url=f"https://example.invalid/{pdf}", evidence="e")],
    )


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(tx, "_pdf_text", lambda content: "TO text")
    calls = []

    def fake_select(cfg, profile, text):
        calls.append(text)
        return ["TOP 4: Menschenrechtslage in Xinjiang"]

    monkeypatch.setattr(tx, "select_tops", fake_select)
    return calls


def test_relevant_tops_land_in_the_note(store, patched):
    store.add(sitting("ausw-29", "2026-09-10T14:00:00", "to029.pdf"), actor="human:test")
    report = tx.run_top_extraction(store, store.config, FakeFetcher(), today=TODAY)
    assert report["checked"] == 1 and report["annotated"] == 1
    event = list(store.iter_events())[0]
    assert "TOP 4: Menschenrechtslage in Xinjiang [to029.pdf]" in event.note
    assert any(h.field == "top_extract" and h.to == "to029.pdf" for h in event.history)


def test_processed_pdf_is_not_reread(store, patched):
    store.add(sitting("ausw-29", "2026-09-10T14:00:00", "to029.pdf"), actor="human:test")
    tx.run_top_extraction(store, store.config, FakeFetcher(), today=TODAY)
    again = tx.run_top_extraction(store, store.config, FakeFetcher(), today=TODAY)
    assert again["checked"] == 0
    event = list(store.iter_events())[0]
    assert event.note.count("to029.pdf") == 1


def test_quiet_agenda_marked_without_note(store, monkeypatch):
    monkeypatch.setattr(tx, "_pdf_text", lambda content: "TO text")
    monkeypatch.setattr(tx, "select_tops", lambda cfg, profile, text: [])
    store.add(sitting("ausw-29", "2026-09-10T14:00:00", "to029.pdf"), actor="human:test")
    report = tx.run_top_extraction(store, store.config, FakeFetcher(), today=TODAY)
    assert report["annotated"] == 0
    event = list(store.iter_events())[0]
    assert event.note is None
    assert any(h.field == "top_extract" for h in event.history)  # not re-read tomorrow


def test_past_sittings_and_foreign_events_skipped(store, patched):
    store.add(sitting("ausw-27", "2026-07-08T09:00:00", "to027.pdf"), actor="human:test")
    other = sitting("x", "2026-09-10", "to.pdf")
    other = other.model_copy(update={"uid": "pc-oav-something-2026"})
    store.add(other, actor="human:test")
    fetcher = FakeFetcher()
    report = tx.run_top_extraction(store, store.config, fetcher, today=TODAY)
    assert report["checked"] == 0 and fetcher.calls == []


def test_model_failure_skips_but_reports(store, monkeypatch):
    monkeypatch.setattr(tx, "_pdf_text", lambda content: "TO text")

    def boom(cfg, profile, text):
        raise RuntimeError("model down")

    monkeypatch.setattr(tx, "select_tops", boom)
    store.add(sitting("ausw-29", "2026-09-10T14:00:00", "to029.pdf"), actor="human:test")
    report = tx.run_top_extraction(store, store.config, FakeFetcher(), today=TODAY)
    assert len(report["errors"]) == 1
    # not marked processed — retried next sweep
    event = list(store.iter_events())[0]
    assert not any(h.field == "top_extract" for h in event.history)
