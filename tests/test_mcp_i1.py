"""What a conversation may and may not write.

Every MCP call is made by a model, and that model reads untrusted web pages,
so each of these is an escalation reachable from a single tool call if the
corresponding guard is removed.
"""

import importlib

import pytest

from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import TierZeroProtected
from china_calendar.verify import evidence_carries_date


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    import china_calendar.mcp_server as server
    importlib.reload(server)
    monkeypatch.setattr(server, "push_event", lambda *a, **k: "skipped", raising=False)
    import china_calendar.calsync as calsync
    monkeypatch.setattr(calsync, "push_event", lambda *a, **k: "skipped")
    return server


def _manual(store, uid="pc-kanzler-2026"):
    return store.add(Event(
        uid=uid, title_en="Kanzlerreise", start="2026-10-01", tier=0,
        status=Status.confirmed, provenance=Provenance.manual,
        sources=[SourceRef(evidence="the user said so, 1 October 2026")],
    ), actor="human:cli")


def test_chat_stated_event_is_tier_3_and_not_confirmed(mcp):
    result = mcp.event_add(title="Xi visits Berlin", start="2026-11-05",
                          human_stated=True, official=True,
                          evidence="the user stated this on 5 November 2026")
    assert result["assigned_status"] == "scheduled"
    event = mcp.STORE.get(result["uid"])
    assert event.tier == 3, "an immortal Tier-0 record must not come from a chat"
    assert "confirmed" in result["verification"]


def test_chat_cannot_amend_or_remove_a_manual_record(mcp):
    _manual(mcp.STORE)
    with pytest.raises(TierZeroProtected):
        mcp.event_amend(uid="pc-kanzler-2026", patch={"start": "2026-10-08"},
                        reason="a poisoned page said so")
    with pytest.raises(TierZeroProtected):
        mcp.event_remove(uid="pc-kanzler-2026", reason="a poisoned page said so")
    assert mcp.STORE.get("pc-kanzler-2026").start == "2026-10-01"
    assert not mcp.STORE.get("pc-kanzler-2026").removed


def test_tier_zero_records_stay_verifiable_from_a_conversation(mcp):
    """Verification attaches provenance and cannot change a date, so it must
    keep working on manual records — phone-adopted, inbox-dropped and
    `pcal add` events are all Tier 0 (#52)."""
    _manual(mcp.STORE)
    result = mcp.event_verify(uid="pc-kanzler-2026", human_stated=True,
                              evidence="I checked this myself, 1 October 2026")
    assert result["written"] is True
    event = mcp.STORE.get("pc-kanzler-2026")
    assert len(event.sources) == 2
    assert event.tier == 0, "verifying must not change the tier"


def test_chat_cannot_authorize_calendar_egress(mcp):
    mcp.STORE.add(Event(
        uid="pc-rumor-2026", title_en="Rumored trip", start="2026-12-01", tier=3,
        status=Status.rumored, provenance=Provenance.research,
        sources=[SourceRef(evidence="chatter")],
    ), actor="human:cli")
    with pytest.raises(ValueError, match="sync_authorized"):
        mcp.event_amend(uid="pc-rumor-2026", patch={"sync_authorized": True},
                        reason="please put it on the calendar")
    # Unchanged from the value it was created with. Since #70 that default is
    # None rather than False; what matters is that chat did not move it.
    assert mcp.STORE.get("pc-rumor-2026").sync_authorized is None


def test_generic_evidence_cannot_buy_confirmed(mcp, monkeypatch):
    """Without the date check, any page containing "2026" is enough to
    confirm any 2026 date on the first try."""
    from china_calendar.fetch import FetchResult

    monkeypatch.setattr(mcp.Fetcher, "fetch_raw",
                        lambda self, url, ignore_robots=None:
                        FetchResult(content=b"a page that mentions 2026 somewhere",
                                    status=200))
    result = mcp.event_add(title="Invented summit", start="2026-11-05",
                          source_url="https://example.org/anything",
                          evidence="2026", official=True)
    assert result["assigned_status"] == "scheduled"
    assert "does not state the date" in result["verification"]


def test_real_evidence_still_confirms(mcp, monkeypatch):
    from china_calendar.fetch import FetchResult

    page = "The summit opens on 5 November 2026 in Berlin.".encode()
    monkeypatch.setattr(mcp.Fetcher, "fetch_raw",
                        lambda self, url, ignore_robots=None:
                        FetchResult(content=page, status=200))
    result = mcp.event_add(title="Real summit", start="2026-11-05",
                          source_url="https://example.org/real",
                          evidence="opens on 5 November 2026",
                          official=True)
    assert result["assigned_status"] == "confirmed"


@pytest.mark.parametrize("evidence,expected", [
    ("die Sitzung findet am 5. November 2026 statt", True),
    ("the meeting is on November 5", True),
    ("会议将于2026年11月5日举行", True),
    ("am 05.11.2026", True),
    ("scheduled for 2026-11-05", True),
    ("sometime in 2026", False),
    ("the official page says so", False),
    ("the meeting is on November 6", False),
])
def test_date_token_recognition(evidence, expected):
    assert evidence_carries_date(evidence, "2026-11-05") is expected


def test_chat_cannot_batch_corroborate(mcp):
    """Review finding: `triage` passed `decision` through unvalidated, so a
    conversation could attach sources to a LIST of arbitrary events — the one
    hole in #68's human-confirm design, and `pending_list` never even shows
    the suggestion, so the model would be choosing blind."""
    with pytest.raises(ValueError, match="accept|reject|defer"):
        mcp.triage(item_ids=["anything"], decision="corroborate")


def test_chat_cannot_invent_a_decision(mcp):
    with pytest.raises(ValueError):
        mcp.triage(item_ids=["anything"], decision="approve")
