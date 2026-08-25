"""Concurrent read-modify-write on the shared store.

The store is a plain directory three processes write to, so these use real
subprocesses and a real flock — an in-process test would prove nothing about
the case that actually bit.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from china_calendar.config import load_config
from china_calendar.models import Event, Provenance, SourceRef, Status
from china_calendar.store import Store, TierZeroProtected


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    return Store(load_config())


def _seed(store, uid="pc-race-2026", tier=3):
    return store.add(Event(
        uid=uid, title_en="Race", start="2026-09-12", tier=tier,
        status=Status.scheduled, provenance=Provenance.feed,
        sources=[SourceRef(evidence="e")],
    ), actor="human:cli")


WRITER = textwrap.dedent("""
    import sys, time
    from china_calendar.config import load_config
    from china_calendar.store import Store

    store = Store(load_config())
    uid, field, value = sys.argv[1], sys.argv[2], sys.argv[3]
    # Widen the read-modify-write window so an unlocked store loses reliably.
    real_get = store.get
    def slow_get(u):
        event = real_get(u)
        time.sleep(0.4)
        return event
    store.get = slow_get
    store.amend(uid, {field: value}, actor="human:cli", reason="concurrent")
""")


def test_concurrent_amends_do_not_lose_a_write(store, tmp_path):
    """Two writers, two different fields. Without the lock the second read
    predates the first write and clobbers it — the shape that silently
    resurrected removed events."""
    event = _seed(store)
    env = {**os.environ, "PC_STORE_DIR": str(tmp_path / "store")}
    script = tmp_path / "writer.py"
    script.write_text(WRITER)

    procs = [
        subprocess.Popen([sys.executable, str(script), event.uid, "note", "from A"], env=env),
        subprocess.Popen([sys.executable, str(script), event.uid, "location", "Berlin"], env=env),
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    after = store.get(event.uid)
    assert after.note == "from A"
    assert after.location == "Berlin"
    fields = [h.field for h in after.history]
    assert "note" in fields and "location" in fields


def test_tier_zero_still_accepts_corroboration(store):
    """Attaching a source cannot change what a record says, so the Tier-0
    gate must not block it — gating it makes manual events unverifiable from
    every automated and conversational surface (#52)."""
    event = _seed(store, uid="pc-manual-2026", tier=0)
    store.attach_source(event.uid, SourceRef(url="https://example.org/x", evidence="x"),
                        actor="auto:verify")
    store.attach_source(event.uid, SourceRef(url="https://example.org/y", evidence="y"),
                        actor="mcp")
    assert len(store.get(event.uid).sources) == 3


def test_tier_zero_still_refuses_automated_content_changes(store):
    event = _seed(store, uid="pc-manual-amend-2026", tier=0)
    with pytest.raises(TierZeroProtected):
        store.amend(event.uid, {"start": "2026-12-01"}, actor="sweep")
    with pytest.raises(TierZeroProtected):
        store.remove(event.uid, actor="auto:expire", reason="x")
    # calsync's adopt path must keep working — every adopted record is Tier 0
    store.amend(event.uid, {"location": "Berlin"}, actor="adopt:backfill",
                reason="location backfilled")
    assert store.get(event.uid).location == "Berlin"


def test_tier_zero_still_takes_automated_history_notes(store):
    """A note changes no field; an automated pass must be able to record
    that it looked."""
    event = _seed(store, uid="pc-manual-note-2026", tier=0)
    store.note_history(event.uid, "recheck_unreachable", "2026-09-12", actor="sweep")
    assert store.get(event.uid).history[-1].field == "recheck_unreachable"
