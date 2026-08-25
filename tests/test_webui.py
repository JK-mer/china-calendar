import importlib

import pytest
from starlette.testclient import TestClient

from china_calendar.models import RawItem
from china_calendar.sources.base import content_hash


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    import china_calendar.webui as webui
    importlib.reload(webui)  # rebind CONFIG/STORE to the tmp store
    return TestClient(webui.app), webui.STORE, webui.CONFIG


def seed_pending(store):
    item = RawItem(
        content_hash=content_hash("noisy-feed", "Committee thing", "2026-09-01"),
        source_id="bundesrat-plenum-2026",  # must exist in sources.yaml for accept
        title="Committee thing",
        start="2026-09-01",
        route="triage",
    )
    store.save_raw(item)
    return item


def test_views_render(client):
    tc, store, _ = client
    for path in ("/", "/events", "/journal", "/digest"):
        assert tc.get(path).status_code == 200


def test_triage_accept_roundtrip(client):
    tc, store, _ = client
    item = seed_pending(store)
    assert "Committee thing" in tc.get("/").text
    resp = tc.post("/triage", data={"single": f"accept:{item.content_hash}", "reason": "test"},
                   follow_redirects=False)
    assert resp.status_code == 303
    events = list(store.iter_events())
    assert len(events) == 1 and "Committee thing" in events[0].title()
    # decided → gone from the queue, visible in the journal
    assert "Committee thing" not in tc.get("/").text.split("Preview")[0].split("Triage")[1]
    assert "created" in tc.get("/journal").text


def test_override_rejected(client):
    tc, store, config = client
    item = seed_pending(store)
    from china_calendar.gate import triage_decide
    triage_decide(store, config, item.content_hash, "reject", "noise", actor="auto:classifier")
    assert not list(store.iter_events())
    journal = tc.get("/journal").text
    assert "Committee thing" in journal and "auto:classifier" in journal
    resp = tc.post("/override", data={"id": [item.content_hash], "reason": "belongs after all"},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert len(list(store.iter_events())) == 1
    decision = store.decision_for(item.content_hash)
    assert decision.decision == "accept" and decision.actor == "human:webui"


def test_bulk_remove_requires_reason_and_restore(client):
    tc, store, _ = client
    item = seed_pending(store)
    tc.post("/triage", data={"single": f"accept:{item.content_hash}"}, follow_redirects=False)
    uid = next(store.iter_events()).uid

    resp = tc.post("/events/action", data={"action": "remove", "uid": [uid]},
                   follow_redirects=False)
    assert "needs+a+reason" in resp.headers["location"] or "needs%20a%20reason" in resp.headers["location"]
    assert not next(iter(store.iter_events(include_removed=True))).removed

    tc.post("/events/action", data={"action": "remove", "uid": [uid], "reason": "duplicate"},
            follow_redirects=False)
    assert next(iter(store.iter_events(include_removed=True))).removed

    tc.post("/events/action", data={"action": "restore", "uid": [uid], "reason": "wrong call"},
            follow_redirects=False)
    event = next(iter(store.iter_events()))
    assert event.removed is False
    assert any(h.field == "removed" and h.to is False for h in event.history)


def test_scraped_title_is_escaped(client):
    tc, store, _ = client
    item = RawItem(
        content_hash=content_hash("noisy-feed", "xss", "2026-09-01"),
        source_id="bundesrat-plenum-2026",
        title='<script>alert(1)</script>Sneaky event',
        start="2026-09-01",
        route="triage",
    )
    store.save_raw(item)
    text = tc.get("/").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def seed_event(store):
    from china_calendar.models import Event, Provenance, SourceRef, Status
    event = Event(
        uid="pc-apk-2026", title_en="APK 2026", start="2026-10-29", end="2026-10-31",
        tier=2, status=Status.scheduled, provenance=Provenance.scrape, actors=["APA"],
        sources=[SourceRef(url="https://example.org/apk", evidence="APK findet statt",
                           verified_at="2026-08-01T00:00:00+00:00")],
    )
    store.add(event, actor="human:test")
    return event


def test_calendar_view(client):
    tc, store, _ = client
    seed_event(store)
    text = tc.get("/kalender?month=2026-10").text
    assert "October 2026" in text and "APK 2026" in text
    assert "cont" in text  # multi-day event: dimmed continuation chips
    assert "k-business_formats" in text  # APA clusters as business format
    assert "APK 2026" not in tc.get("/kalender?month=2027-03").text


def test_bestand_filters(client):
    tc, store, _ = client
    seed_event(store)
    assert "APK 2026" in tc.get("/events?q=findet statt").text  # evidence text
    assert "APK 2026" in tc.get("/events?verified=1").text
    assert "APK 2026" in tc.get("/events?von=2026-10-01&bis=2026-11-01").text
    assert "APK 2026" not in tc.get("/events?von=2026-11-01").text


def test_bestand_folds_last_month_and_older(client):
    """#60: the store sorts ascending, so without the fold the view opens on
    2023 every time. Folded, never silently — and an explicit range wins."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    for uid, start, end in (("pc-old-2023", "2023-05-04", None),
                            ("pc-lastmonth-2026", "2026-07-20", None),
                            ("pc-spanning-2026", "2026-07-28", "2026-08-02")):
        store.add(Event(
            uid=uid, title_en=f"Event {uid}", start=start, end=end, tier=2,
            status=Status.scheduled, provenance=Provenance.scrape,
            sources=[SourceRef(evidence="e")],
        ), actor="human:test")
    seed_event(store)  # APK, 2026-10

    import china_calendar.webui as webui
    from datetime import date
    assert webui._fold_cutoff(date(2026, 8, 4)) == date(2026, 8, 1)
    original = webui._fold_cutoff
    webui._fold_cutoff = lambda today=None: date(2026, 8, 1)
    try:
        text = tc.get("/events").text
        assert "pc-old-2023" not in text and "pc-lastmonth-2026" not in text
        assert "APK 2026" in text
        # a July event running into August is not "last month or older"
        assert "pc-spanning-2026" in text
        # the fold announces itself and offers the way back
        assert "2 event(s) from last month or older" in text
        assert "past=1" in text

        assert "pc-old-2023" in tc.get("/events?past=1").text
        # an explicit from-date is the user asking for that range
        assert "pc-old-2023" in tc.get("/events?von=2023-01-01").text
        # a filter matching only folded events says so instead of going blank
        empty = tc.get("/events?q=pc-old-2023").text
        assert "1 event(s) from last month or older" in empty
    finally:
        webui._fold_cutoff = original


def test_remind_toggle(client):
    tc, store, _ = client
    event = seed_event(store)
    assert "reminder T-2" not in tc.get("/events").text  # scheduled: silent by policy
    resp = tc.post("/events/action", data={"action": "remind_on", "uid": [event.uid]},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert store.get(event.uid).remind is True
    assert "reminder T-2" in tc.get("/events").text
    tc.post("/events/action", data={"action": "remind_off", "uid": [event.uid]},
            follow_redirects=False)
    assert store.get(event.uid).remind is False


def test_calendar_long_spans_leave_the_grid(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    week = Event(
        uid="pc-sitzungswoche-test-2026", title_de="Sitzungswoche Deutscher Bundestag",
        start="2026-10-05", end="2026-10-09", tier=1, status=Status.scheduled,
        provenance=Provenance.feed, actors=["Bundestag"],
        sources=[SourceRef(evidence="test")],
    )
    store.add(week, actor="human:test")
    seed_event(store)  # 3-day APK stays in the grid
    text = tc.get("/kalender?month=2026-10").text
    assert "Multi-day spans" in text
    # the week renders exactly once (list row), not as five daily chips
    assert text.count("Sitzungswoche Deutscher Bundestag") == 1
    grid = text.split('<div class="calwrap">')[1]
    assert "Sitzungswoche" not in grid
    assert "APK 2026" in grid


def test_calendar_toggle_authorizes_projected(client):
    tc, store, _ = client
    from china_calendar.calsync import eligible
    from china_calendar.models import Event, Provenance, SourceRef, Status
    from datetime import date
    event = Event(
        uid="pc-two-sessions-2027", title_en="Two Sessions (projected)",
        start="2027-03-04", end="2027-03-12", tier=3,
        status=Status.projected, provenance=Provenance.research,
        sources=[SourceRef(evidence="seeded")],
    )
    store.add(event, actor="human:test")
    # #70: a projection syncs by default and carries no label — the toggle is
    # an opt-OUT now, so only the exception is worth showing.
    text = tc.get("/events").text
    assert "calendar off" not in text
    assert eligible(store.get(event.uid), date(2026, 8, 3))

    resp = tc.post("/events/action", data={"action": "cal_off", "uid": [event.uid]},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert store.get(event.uid).sync_authorized is False
    assert not eligible(store.get(event.uid), date(2026, 8, 3))
    assert "calendar off" in tc.get("/events").text

    resp = tc.post("/events/action", data={"action": "cal_on", "uid": [event.uid]},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert store.get(event.uid).sync_authorized is True
    assert eligible(store.get(event.uid), date(2026, 8, 3))
    # Switched explicitly on reads the same as never touched: no label. Only
    # the exclusion is worth a row-level flag.
    assert "calendar off" not in tc.get("/events").text


def test_unresolved_projection_flagged_on_watchlist(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    past_window = Event(
        uid="pc-beidaihe-2020", title_en="Beidaihe window (projected)",
        start="2020-07-25", end="2020-08-15", tier=3,
        status=Status.projected, provenance=Provenance.research,
        sources=[SourceRef(evidence="seeded")],
    )
    store.add(past_window, actor="human:test")
    text = tc.get("/").text
    assert "projection window closed" in text
    # digest carries the same flag
    from china_calendar.digest import build_digest
    digest = build_digest(store, store.config)
    assert "Projection windows closed" in digest and "Beidaihe" in digest


def test_elections_cluster_filter(client):
    tc, store, _ = client
    from china_calendar.clusters import cluster_of
    from china_calendar.models import Event, Provenance, SourceRef, Status
    election = Event(
        uid="pc-landtagswahl-sachsen-anhalt-2026", title_de="Landtagswahl Sachsen-Anhalt 2026",
        start="2026-09-06", tier=2, status=Status.scheduled, provenance=Provenance.scrape,
        sectors=["elections", "german_institutional"],
        sources=[SourceRef(url="https://example.invalid/wahltermine", evidence="06.09.")],
    )
    store.add(election, actor="human:test")
    seed_event(store)  # APK, business_formats — must not match the filter
    # elections wins over german_institutional in the sector fallback
    assert cluster_of(election) == "elections"
    filtered = tc.get("/events?cluster=elections").text
    assert "Landtagswahl Sachsen-Anhalt" in filtered
    assert "APK 2026" not in filtered
    assert ">Elections</option>" in filtered  # dropdown carries the new cluster


def test_api_summary_matches_the_page(client):
    tc, store, _ = client
    seed_pending(store)
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-beidaihe-2020", title_en="Beidaihe window (projected)",
        start="2020-07-25", end="2020-08-15", tier=3,
        status=Status.projected, provenance=Provenance.research,
        sources=[SourceRef(evidence="seeded")],
    ), actor="human:test")
    seed_event(store)  # scheduled, in the 90-day window? no — Oct 2026

    data = tc.get("/api/summary").json()
    assert data["pending"] == 1
    assert data["total_events"] == 2
    assert data["watch"] == 1  # the closed projection window
    assert data["sources"] == {"total": 0, "ok": 0, "sick": 0, "sick_ids": []}
    assert data["last_sweep"] is None  # no sweep in a fresh store
    # the counts are the same ones the header renders
    assert f"triage <strong>{data['pending']}</strong>" in tc.get("/").text
    # counts only — no event titles leak through this route
    assert "Beidaihe" not in tc.get("/api/summary").text


def test_api_queue_and_upcoming(client):
    tc, store, _ = client
    item = seed_pending(store)
    seed_event(store)

    queue = tc.get("/api/queue").json()
    assert queue["total"] == 1
    assert queue["items"][0]["id"] == item.content_hash
    assert queue["items"][0]["title"] == "Committee thing"

    # the APK is in 2026-10; a 1-day window must not reach it
    assert tc.get("/api/upcoming?days=1").json()["total"] == 0
    wide = tc.get("/api/upcoming?days=730").json()
    assert wide["total"] == 1
    assert wide["events"][0]["uid"] == "pc-apk-2026"
    assert wide["events"][0]["status"] == "scheduled"
    # nonsense params clamp instead of erroring
    assert tc.get("/api/upcoming?days=abc&limit=99999").status_code == 200


def test_api_triage_accepts_and_records_the_actor(client):
    tc, store, _ = client
    item = seed_pending(store)

    # a form POST cannot reach this route — that is the point of the 415
    assert tc.post("/api/triage", data={"ids": item.content_hash,
                                        "decision": "accept"}).status_code == 415

    resp = tc.post("/api/triage", json={"ids": [item.content_hash],
                                        "decision": "accept", "reason": "belongs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decided"] == 1 and body["failed"] == []
    assert len(body["accepted"]) == 1
    assert len(list(store.iter_events())) == 1
    # the ledger records where the decision came from
    assert store.decision_for(item.content_hash).actor == "human:dashboard"
    assert tc.get("/api/queue").json()["total"] == 0


def test_api_triage_rejects_bad_input_and_survives_a_bad_id(client):
    tc, store, _ = client
    item = seed_pending(store)
    assert tc.post("/api/triage", json={"ids": ["x"], "decision": "shrug"}).status_code == 400
    assert tc.post("/api/triage", json={"ids": [], "decision": "accept"}).status_code == 400

    # one unknown id must not discard the good decision beside it
    body = tc.post("/api/triage", json={"ids": ["nope", item.content_hash],
                                        "decision": "reject"}).json()
    assert body["decided"] == 1
    assert body["failed"][0]["id"] == "nope"
    assert store.decision_for(item.content_hash).decision == "reject"


def test_bestand_pagination_preserves_filters(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    for n in range(55):
        store.add(Event(
            uid=f"pc-wahl-{n:03d}-2026", title_en=f"Election {n:03d}",
            start=f"2026-{9 + n % 3:02d}-{1 + n % 27:02d}", tier=2,
            status=Status.scheduled, provenance=Provenance.scrape,
            sectors=["elections"], sources=[SourceRef(evidence="e")],
        ), actor="human:test")
    page1 = tc.get("/events?cluster=elections").text
    assert "page 1/2" in page1 and "55 entries" in page1
    assert page1.count('class="row') == 50
    assert "cluster=elections" in page1 and "page=2" in page1  # filter kept in pager link
    page2 = tc.get("/events?cluster=elections&page=2").text
    assert page2.count('class="row') == 5
    # out-of-range pages clamp instead of erroring
    assert tc.get("/events?page=99").status_code == 200
    assert tc.get("/journal?cp=3&rp=2").status_code == 200


def test_glossar_renders(client):
    tc, _, _ = client
    text = tc.get("/glossar").text
    assert "Beidaihe" in text and "Sitzungswoche" in text
    assert "Regierungskonsultationen" in text
    # tab is in the nav of every page
    assert 'href="/glossar"' in tc.get("/").text


def test_bestand_verify_form_and_human_statement(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-brics-2026", title_en="BRICS Summit", start="2026-09-12",
        tier=3, status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="asserted in conversation, no source")],
    ), actor="mcp")
    text = tc.get("/events").text
    assert 'id="vf-pc-brics-2026"' in text  # sibling form for the inline inputs
    assert "I checked this myself" in text

    resp = tc.post("/events/verify",
                   data={"uid": "pc-brics-2026", "mode": "human",
                         "evidence": "confirmed on brics2026.gov.in myself"},
                   follow_redirects=False)
    assert resp.status_code == 303
    event = store.get("pc-brics-2026")
    assert event.status.value == "scheduled"
    assert event.sources[-1].evidence == "confirmed on brics2026.gov.in myself"
    assert any(h.actor == "human:webui" and h.field == "status" for h in event.history)
    # confirmed events lose the verify affordance — and confirming needs
    # evidence that states the date, so this one carries it
    tc.post("/events/verify",
            data={"uid": "pc-brics-2026", "mode": "human", "official": "1",
                  "evidence": "official page: 12 September 2026"},
            follow_redirects=False)
    assert store.get("pc-brics-2026").status.value == "confirmed"
    assert 'id="vf-pc-brics-2026"' not in tc.get("/events").text


def test_dateless_evidence_cannot_confirm_from_the_dashboard(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-vague-2026", title_en="Vague", start="2026-09-12", tier=3,
        status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="asserted in conversation, no source")],
    ), actor="human:test")
    resp = tc.post("/events/verify",
                   data={"uid": "pc-vague-2026", "mode": "human", "official": "1",
                         "evidence": "the official page says so"},
                   follow_redirects=False)
    assert store.get("pc-vague-2026").status.value == "scheduled"
    from urllib.parse import unquote
    assert "cannot confirm" in unquote(resp.headers["location"])


def test_bestand_verify_needs_evidence(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-x-2026", title_en="X", start="2026-09-12",
        tier=3, status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    resp = tc.post("/events/verify", data={"uid": "pc-x-2026", "mode": "human"},
                   follow_redirects=False)
    assert resp.status_code == 303 and "evidence" in resp.headers["location"]
    assert store.get("pc-x-2026").status.value == "unverified"


def test_redirect_message_appends_to_existing_query(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-y-2026", title_en="Y", start="2026-09-12",
        tier=3, status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    resp = tc.post("/events/verify",
                   data={"uid": "pc-y-2026", "mode": "human", "evidence": "checked"},
                   follow_redirects=False)
    location = resp.headers["location"]
    assert location.count("?") == 1 and "&m=" in location  # ?q=…&m=…, not ?q=…?m=…


def test_triage_card_links_to_the_item_url(client):
    tc, store, _ = client
    from china_calendar.models import RawItem
    from china_calendar.sources.base import content_hash
    store.save_raw(RawItem(
        content_hash=content_hash("noisy-feed", "Linked thing", "2026-09-01"),
        source_id="bundesrat-plenum-2026", title="Linked thing",
        start="2026-09-01", route="triage",
        url="https://example.org/session-42",
    ))
    text = tc.get("/").text
    assert 'href="https://example.org/session-42"' in text
    # leaving the dashboard mid-triage must not cost the user their place
    assert 'target="_blank" rel="noopener noreferrer">source</a>' in text


def test_verify_available_from_overview_and_calendar(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-sco-2026", title_en="SCO Summit window", start="2026-09-01",
        end="2026-09-30", tier=3, status=Status.unverified,
        provenance=Provenance.research, sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    # Overview: the stamp is the verify trigger in EVERY section it appears
    # in — Preview and Watchlist both, each with its own form id (#51).
    overview = tc.get("/").text
    assert 'id="vf-p-pc-sco-2026"' in overview, "Preview must offer verification"
    assert 'id="vf-w-pc-sco-2026"' in overview, "Watchlist must offer verification"
    assert 'name="back" value="/"' in overview
    # Calendar: the 30-day span renders in the multi-day list with a form
    calendar = tc.get("/kalender?month=2026-09").text
    assert 'id="vf-pc-sco-2026"' in calendar
    assert 'name="back" value="/kalender"' in calendar


def test_duplicate_listing_keeps_its_own_inputs(client):
    """One event in two sections means two forms; their ids must differ or
    the browser hands the handler the first copy's inputs (#51)."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-dup-2026", title_en="Listed twice", start="2026-09-01",
        end="2026-09-30", tier=3, status=Status.unverified,
        provenance=Provenance.research, sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    overview = tc.get("/").text
    assert overview.count('id="vf-p-pc-dup-2026"') == 1
    assert overview.count('id="vf-w-pc-dup-2026"') == 1
    # and no unscoped id survives to collide with either
    assert 'id="vf-pc-dup-2026"' not in overview
    # each popup's inputs point at its own form
    assert overview.count('form="vf-p-pc-dup-2026" type="hidden" name="uid"') == 1
    assert overview.count('form="vf-w-pc-dup-2026" type="hidden" name="uid"') == 1


def test_verify_back_redirect_and_no_open_redirect(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-z-2026", title_en="Z", start="2026-09-12", tier=3,
        status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    resp = tc.post("/events/verify",
                   data={"uid": "pc-z-2026", "mode": "human",
                         "evidence": "checked", "back": "/"},
                   follow_redirects=False)
    assert resp.headers["location"].startswith("/?m=")
    resp = tc.post("/events/verify",
                   data={"uid": "pc-z-2026", "mode": "human",
                         "evidence": "checked again", "back": "//evil.example"},
                   follow_redirects=False)
    assert resp.headers["location"].startswith("/events?q=pc-z-2026")


def test_verify_popup_prefills_existing_source(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-pre-2026", title_en="Prefilled", start="2026-09-12", tier=3,
        status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(url="https://example.org/page", evidence="the date is set",
                           verify_strings=["the date is set"])],
    ), actor="mcp")
    text = tc.get("/events?q=pc-pre-2026").text
    assert 'name="url" placeholder="Source URL" value="https://example.org/page"' in text
    assert 'value="the date is set"' in text


def test_cross_origin_form_post_refused(client):
    """A malicious LAN page can guess a manual uid; what it cannot do is
    forge the Origin header."""
    tc, store, _ = client
    item = seed_pending(store)
    resp = tc.post("/triage",
                   data={"single": f"accept:{item.content_hash}", "reason": "csrf"},
                   headers={"Origin": "http://evil.example"},
                   follow_redirects=False)
    assert resp.status_code == 403
    assert store.get_raw(item.content_hash).route == "triage"  # unchanged


def test_same_origin_form_post_allowed(client):
    tc, store, _ = client
    item = seed_pending(store)
    resp = tc.post("/triage",
                   data={"single": f"accept:{item.content_hash}", "reason": "ok"},
                   headers={"Origin": "http://testserver"},
                   follow_redirects=False)
    assert resp.status_code == 303


def test_api_triage_stays_cross_origin_callable(client):
    """The dashboard tile (#32) posts JSON from another origin; its own
    content-type guard is what protects it, so the middleware must not."""
    tc, store, _ = client
    item = seed_pending(store)
    resp = tc.post("/api/triage",
                   json={"decision": "reject", "ids": [item.content_hash],
                         "reason": "tile"},
                   headers={"Origin": "http://dashboard.example"})
    assert resp.status_code == 200


def test_back_redirect_rejects_backslash(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-bs-2026", title_en="BS", start="2026-09-12", tier=3,
        status=Status.unverified, provenance=Provenance.research,
        sources=[SourceRef(evidence="e")],
    ), actor="mcp")
    resp = tc.post("/events/verify",
                   data={"uid": "pc-bs-2026", "mode": "human",
                         "evidence": "checked", "back": "/\\evil.example"},
                   follow_redirects=False)
    assert resp.headers["location"].startswith("/events?q=pc-bs-2026")


def test_javascript_href_from_a_feed_is_not_linked(client):
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-js-2026", title_en="Hostile source", start="2026-09-12", tier=2,
        status=Status.scheduled, provenance=Provenance.scrape,
        sources=[SourceRef(url="javascript:alert(1)", evidence="e")],
    ), actor="mcp")
    text = tc.get("/events?q=pc-js-2026").text
    # No clickable link. (It still appears as the verify form's prefilled
    # url value — inert there, and guard_url refuses it on submit.)
    assert 'href="javascript:' not in text
    assert ">source</a>" not in text
    assert "Hostile source" in text


def test_detail_tile_shows_note_sources_history(client):
    """#56: the row title opens a tile with everything the row cannot show —
    the COP31 Leaders' Summit misread happened because the note was invisible."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(
        uid="pc-wls-2026", title_en="Leaders' Summit", start="2026-11-11",
        end="2026-11-12", tier=3, status=Status.unverified,
        provenance=Provenance.research, note="Deliberately days 3-4 of the COP.",
        sources=[SourceRef(url="https://example.org/cop", evidence="11 to 12 November")],
    ), actor="mcp")
    text = tc.get("/events?q=pc-wls-2026").text
    assert "Deliberately days 3-4 of the COP." in text
    assert "11 to 12 November" in text  # source evidence in the tile
    assert 'id="af-pc-wls-2026"' in text  # sibling amend form
    assert "History (recent)" in text and "__created__" in text
    # scoped copies on the overview, like the verify forms (#51)
    overview = tc.get("/").text
    assert 'id="af-w-pc-wls-2026"' in overview
    assert 'id="af-pc-wls-2026"' not in overview


def test_amend_from_tile_requires_reason(client):
    tc, store, _ = client
    event = seed_event(store)
    resp = tc.post("/events/amend", data={"uid": event.uid, "start": "2026-10-30"},
                   follow_redirects=False)
    assert "needs+a+reason" in resp.headers["location"].replace("%20", "+")
    assert store.get(event.uid).start == "2026-10-29"


def test_amend_from_tile_moves_date_and_requeues(client):
    tc, store, _ = client
    event = seed_event(store)  # scheduled
    resp = tc.post("/events/amend",
                   data={"uid": event.uid, "start": "2026-10-30", "end": "2026-11-01",
                         "note": "", "reason": "official page corrected"},
                   follow_redirects=False)
    assert resp.status_code == 303
    amended = store.get(event.uid)
    assert amended.start == "2026-10-30" and amended.end == "2026-11-01"
    # the shared core rule (I2): a moved date without a new verified source
    # cannot stay scheduled
    assert amended.status.value == "unverified"
    assert any(h.actor == "human:webui" and h.field == "start" for h in amended.history)
    from urllib.parse import unquote
    assert "dropped to" in unquote(resp.headers["location"])


def test_amend_from_tile_note_only_keeps_status(client):
    tc, store, _ = client
    event = seed_event(store)
    tc.post("/events/amend",
            data={"uid": event.uid, "start": event.start, "end": event.end,
                  "note": "now with context", "reason": "context"},
            follow_redirects=False)
    amended = store.get(event.uid)
    assert amended.note == "now with context"
    assert amended.status.value == "scheduled"


def test_amend_from_tile_rejects_garbage_dates(client):
    tc, store, _ = client
    event = seed_event(store)
    resp = tc.post("/events/amend",
                   data={"uid": event.uid, "start": "next Tuesday", "reason": "typo"},
                   follow_redirects=False)
    from urllib.parse import unquote
    assert "Amend failed" in unquote(resp.headers["location"])
    assert store.get(event.uid).start == "2026-10-29"


def test_amend_from_tile_nothing_changed(client):
    tc, store, _ = client
    event = seed_event(store)
    resp = tc.post("/events/amend",
                   data={"uid": event.uid, "start": event.start, "end": event.end,
                         "note": "", "reason": "no-op"},
                   follow_redirects=False)
    from urllib.parse import unquote
    assert "nothing changed" in unquote(resp.headers["location"])
    assert not any(h.reason == "no-op" for h in store.get(event.uid).history)


# ---------------------------------------------------------------- #73 CSV export

def test_csv_export_matches_the_filtered_view(client):
    """The export must agree with the page. A file that quietly disagrees with
    what you were looking at is worse than no export."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    for uid, title, start, status in [
        ("pc-csv-a-2026", "APEC thing", "2026-11-18", Status.confirmed),
        ("pc-csv-b-2026", "Bundesrat thing", "2026-11-20", Status.scheduled),
        ("pc-csv-c-2027", "Later thing", "2027-06-01", Status.projected),
    ]:
        store.add(Event(uid=uid, title_en=title, start=start, end=start, tier=3,
                        status=status, provenance=Provenance.research,
                        sources=[SourceRef(url="https://example.org/x", evidence="ev")]),
                  actor="human:test")

    body = tc.get("/events.csv").text
    assert body.startswith("﻿"), "Excel needs the BOM or umlauts break"
    assert "APEC thing" in body and "Later thing" in body

    filtered = tc.get("/events.csv?q=APEC").text
    assert "APEC thing" in filtered
    assert "Bundesrat thing" not in filtered

    by_status = tc.get("/events.csv?status=projected").text
    assert "Later thing" in by_status and "APEC thing" not in by_status


def test_csv_export_has_the_requested_columns(client):
    tc, store, _ = client
    header = tc.get("/events.csv").text.lstrip("﻿").splitlines()[0]
    for column in ("start", "end", "status", "title", "note", "source_url"):
        assert column in header


def test_csv_export_neutralises_formula_injection(client):
    """Titles come from scraped sources; Excel executes a cell starting with
    '=' as a formula on open."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(uid="pc-csv-evil-2026", title_en="=1+1", start="2026-11-18",
                    end="2026-11-18", tier=3, status=Status.scheduled,
                    provenance=Provenance.research,
                    sources=[SourceRef(evidence="ev")]), actor="human:test")
    body = tc.get("/events.csv?q=1%2B1").text
    assert "'=1+1" in body
    assert ",=1+1" not in body


def test_csv_export_is_offered_on_the_events_page(client):
    tc, _, _ = client
    assert "/events.csv" in tc.get("/events").text


def test_csv_formula_guard_is_not_bypassed_by_leading_whitespace(client):
    """Excel trims leading whitespace before deciding a cell is a formula, so
    a naive startswith check let "\\t=HYPERLINK(...)" through. Titles are
    scraped, i.e. untrusted."""
    tc, store, _ = client
    from china_calendar.models import Event, Provenance, SourceRef, Status
    store.add(Event(uid="pc-csv-tab-2026", title_en="\t=HYPERLINK(\"http://evil\")",
                    start="2026-11-18", end="2026-11-18", tier=3,
                    status=Status.scheduled, provenance=Provenance.research,
                    sources=[SourceRef(evidence="ev")]), actor="human:test")
    body = tc.get("/events.csv").text
    assert "'\t=HYPERLINK" in body or '"\'\t=HYPERLINK' in body
    assert ",\t=HYPERLINK" not in body
