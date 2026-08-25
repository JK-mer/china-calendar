"""File-per-record event store on a plain directory, bind-mounted into the
containers.

Design: one JSON file per event under events/<uid>.json, plus an index.json
written for external consumers by the sweep and the CLI (nothing in here
reads it back). Dotfiles are ignored.

Three processes write concurrently (sweep, web container, MCP calls) with
nothing arbitrating between them, so every read-modify-write takes an
exclusive flock on a per-uid lockfile. Without it the loser of a
get-modify-replace race vanishes silently — a removal overwritten by a
concurrent amend leaves the event alive with no removal in its history.

Rules enforced here, not in the adapters (invariant I2):
  - Tier 0 records are immune to automated modification (see _is_automated).
  - Nothing is ever hard-deleted; removal sets removed=True with a reason.
  - Every field change appends to history.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from .config import Config, load_config
from .models import Decision, Event, HistoryEntry, RawItem, SourceState, Status, utcnow


class StoreError(Exception):
    pass


class TierZeroProtected(StoreError):
    """Raised when an automated actor tries to modify a Tier 0 (manual) record."""


def _is_automated(actor: str) -> bool:
    """Actors barred from modifying Tier 0.

    Deliberately narrow. The gate protects manual records from unattended
    passes; it is not the place to restrict the conversational path, because
    every actor listed here is also refused corroboration and calendar
    bookkeeping. `calsync` relies on `adopt:*` staying out of it. Chat is
    restricted in the MCP adapter instead.
    """
    return actor.startswith(("auto:", "sweep")) or actor == "system"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _dump(model) -> str:
    return model.model_dump_json(indent=2, by_alias=True) + "\n"


class Store:
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self._held: set[str] = set()

    # ------------------------------------------------------------- events

    def _event_path(self, uid: str) -> Path:
        return self.config.events_dir / f"{uid}.json"

    @contextmanager
    def _locked(self, uid: str):
        """Exclusive lock for one event's read-modify-write cycle.

        Sidecar file, not the record: locking the record would race with the
        atomic rename that replaces it. Held uids are tracked because flock is
        per open-file-description, so a nested acquire deadlocks on itself.
        """
        if uid in self._held:
            yield
            return
        lock_dir = self.config.store_dir / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_dir / f"{uid}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._held.add(uid)
            yield
        finally:
            self._held.discard(uid)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def exists(self, uid: str) -> bool:
        return self._event_path(uid).exists()

    def get(self, uid: str) -> Event:
        path = self._event_path(uid)
        if not path.exists():
            raise StoreError(f"no event {uid!r}")
        return Event.model_validate_json(path.read_text(encoding="utf-8"))

    def add(self, event: Event, actor: str, reason: str | None = None) -> Event:
        with self._locked(event.uid):
            if self.exists(event.uid):
                raise StoreError(f"event {event.uid!r} already exists; use amend")
            event.history.append(
                HistoryEntry(field="__created__", to=event.status.value, actor=actor, reason=reason)
            )
            _atomic_write(self._event_path(event.uid), _dump(event))
            return event

    AMENDABLE = {
        "title_de", "title_en", "title_zh", "start", "end", "all_day", "timezone",
        "status", "sectors", "actors", "location", "note", "calendar_uid", "tier",
        "provenance", "sync_authorized", "remind",
    }

    def mark_synced(self, uid: str, calendar_uid: str | None) -> Event:
        """Bookkeeping after a calendar push/delete — bypasses the Tier 0
        actor gate deliberately: sync state is not content."""
        with self._locked(uid):
            event = self.get(uid)
            if event.calendar_uid == calendar_uid:
                return event
            data = event.model_dump()
            data["calendar_uid"] = calendar_uid
            data["updated_at"] = utcnow()
            event = Event.model_validate(data)
            _atomic_write(self._event_path(uid), _dump(event))
            return event

    def amend(self, uid: str, patch: dict, actor: str, reason: str | None = None) -> Event:
        with self._locked(uid):
            event = self.get(uid)
            if event.tier == 0 and _is_automated(actor):
                raise TierZeroProtected(f"{uid} is Tier 0 (manual); automated amendment refused")
            unknown = set(patch) - self.AMENDABLE
            if unknown:
                raise StoreError(f"fields not amendable: {sorted(unknown)}")
            data = event.model_dump()
            changed = False
            for field_name, new_value in patch.items():
                old_value = data[field_name]
                old_cmp = old_value.value if hasattr(old_value, "value") else old_value
                if old_cmp == new_value:
                    continue
                data[field_name] = new_value
                event = Event.model_validate(data)  # validate incrementally
                event.history.append(
                    HistoryEntry(field=field_name, from_=old_cmp, to=new_value,
                                 actor=actor, reason=reason)
                )
                data = event.model_dump()
                changed = True
            if changed:
                data["updated_at"] = utcnow()
                event = Event.model_validate(data)
                _atomic_write(self._event_path(uid), _dump(event))
            return event

    def amend_and_requeue(self, uid: str, patch: dict, actor: str,
                          reason: str | None = None) -> tuple[Event, bool]:
        """amend() plus the shared date-move rule (I2 — every interactive
        adapter needs it identically): moving start/end of a confirmed or
        scheduled event invalidates whatever verification earned that status,
        so the event drops to unverified for the next sweep to re-check.
        Returns (event, requeued)."""
        before = self.get(uid)
        date_moved = ("start" in patch and patch["start"] != before.start) or \
                     ("end" in patch and patch.get("end") != before.end)
        event = self.amend(uid, patch, actor=actor, reason=reason)
        if date_moved and event.status in (Status.confirmed, Status.scheduled):
            event = self.amend(uid, {"status": Status.unverified.value}, actor=actor,
                               reason="date changed without a verified source; requeued")
            return event, True
        return event, False

    def attach_source(self, uid: str, source, actor: str, reason: str | None = None) -> Event:
        """Corroboration path: attaches a source without touching the date.

        Not gated on Tier 0 — attaching evidence cannot change what the record
        says, and gating it makes manual events unverifiable from any
        automated or conversational surface.
        """
        with self._locked(uid):
            event = self.get(uid)
            event.sources.append(source)
            event.history.append(
                HistoryEntry(field="sources", to=getattr(source, "url", None) or "evidence",
                             actor=actor, reason=reason or "source attached")
            )
            data = event.model_dump()
            data["updated_at"] = utcnow()
            event = Event.model_validate(data)
            _atomic_write(self._event_path(uid), _dump(event))
            return event

    def restore(self, uid: str, actor: str, reason: str) -> Event:
        """Undo a soft delete. The record and its history were never gone."""
        with self._locked(uid):
            event = self.get(uid)
            if event.tier == 0 and _is_automated(actor):
                raise TierZeroProtected(f"{uid} is Tier 0 (manual); automated restore refused")
            if not event.removed:
                return event
            data = event.model_dump()
            data["removed"] = False
            data["removed_reason"] = None
            data["updated_at"] = utcnow()
            event = Event.model_validate(data)
            event.history.append(
                HistoryEntry(field="removed", from_=True, to=False, actor=actor, reason=reason)
            )
            _atomic_write(self._event_path(uid), _dump(event))
            return event

    def note_history(self, uid: str, field: str, to, actor: str,
                     reason: str | None = None) -> Event:
        """Append a history entry without changing any field (e.g. a failed
        re-check) — the record of what happened is part of the data.

        Tier 0 is exempt from the automated-actor gate here: a note changes
        no field, and an automated pass must still be able to record that it
        looked at a manual record.
        """
        with self._locked(uid):
            event = self.get(uid)
            event.history.append(HistoryEntry(field=field, to=to, actor=actor, reason=reason))
            data = event.model_dump()
            data["updated_at"] = utcnow()
            event = Event.model_validate(data)
            _atomic_write(self._event_path(uid), _dump(event))
            return event

    def attach_verification(self, event: Event, history: HistoryEntry | None = None) -> None:
        """Persist verified_at stamps set by verify.py; no field changes.

        A history entry may ride along in the SAME write (#55). The stamps and
        the record of the check that produced them have to commit together —
        as two writes, a failure in between leaves refreshed stamps with
        nothing on record saying why they moved, which is indistinguishable
        from a verification nobody performed.
        """
        with self._locked(event.uid):
            if history is not None:
                event.history.append(history)
            data = event.model_dump()
            data["updated_at"] = utcnow()
            _atomic_write(self._event_path(event.uid), _dump(Event.model_validate(data)))

    def remove(self, uid: str, actor: str, reason: str) -> Event:
        with self._locked(uid):
            event = self.get(uid)
            if event.tier == 0 and _is_automated(actor):
                raise TierZeroProtected(f"{uid} is Tier 0 (manual); automated removal refused")
            if event.removed:
                return event
            data = event.model_dump()
            data["removed"] = True
            data["removed_reason"] = reason
            data["updated_at"] = utcnow()
            event = Event.model_validate(data)
            event.history.append(
                HistoryEntry(field="removed", from_=False, to=True, actor=actor, reason=reason)
            )
            _atomic_write(self._event_path(uid), _dump(event))
            return event

    def iter_events(self, include_removed: bool = False) -> Iterator[Event]:
        events_dir = self.config.events_dir
        if not events_dir.exists():
            return
        for path in sorted(events_dir.iterdir()):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            try:
                event = Event.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                # Fail loudly: a corrupt record means a sync conflict or a bad
                # writer, and silently skipping it would hide data loss.
                raise StoreError(f"corrupt event file {path.name}: {exc}") from exc
            if event.removed and not include_removed:
                continue
            yield event

    def search(
        self,
        query: str | None = None,
        from_: date | None = None,
        to: date | None = None,
        tier: int | None = None,
        status: str | None = None,
        sectors: list[str] | None = None,
        actors: list[str] | None = None,
        include_removed: bool = False,
        verified: bool | None = None,
    ) -> list[Event]:
        results = []
        q = query.lower() if query else None
        for event in self.iter_events(include_removed=include_removed):
            if from_ and event.end_date() < from_:
                continue
            if to and event.start_date() > to:
                continue
            if tier is not None and event.tier != tier:
                continue
            if status and event.status.value != status:
                continue
            if sectors and not set(sectors) & set(event.sectors):
                continue
            if actors and not set(actors) & set(event.actors):
                continue
            if verified and not any(s.verified_at for s in event.sources):
                continue
            if q:
                haystack = " ".join(
                    filter(None, [event.title_de, event.title_en, event.title_zh,
                                  event.note, " ".join(event.sectors), " ".join(event.actors),
                                  event.uid, event.location,
                                  " ".join(s.evidence for s in event.sources),
                                  " ".join(s.url or "" for s in event.sources)])
                ).lower()
                if q not in haystack:
                    continue
            results.append(event)
        results.sort(key=lambda e: e.start)
        return results

    # -------------------------------------------------------------- index

    def rebuild_index(self) -> dict:
        entries = {}
        for event in self.iter_events(include_removed=True):
            entries[event.uid] = {
                "title": event.title(),
                "start": event.start,
                "end": event.end,
                "status": event.status.value,
                "tier": event.tier,
                "removed": event.removed,
            }
        index = {"generated_at": utcnow(), "count": len(entries), "events": entries}
        _atomic_write(self.config.index_path, json.dumps(index, indent=2, ensure_ascii=False) + "\n")
        return index

    # index.json is written for external consumers and rebuilt by the sweep
    # and the CLI; it is stale between runs by design. There is deliberately
    # no reader for it here — everything internal goes through
    # iter_events/search, and a second read path over the same data would be
    # a divergence waiting to happen.

    # ---------------------------------------------------- raw items / gate

    def save_raw(self, item: RawItem) -> None:
        _atomic_write(self.config.raw_dir / f"{item.content_hash}.json", _dump(item))

    def get_raw(self, content_hash: str) -> RawItem:
        path = self.config.raw_dir / f"{content_hash}.json"
        if not path.exists():
            raise StoreError(f"no raw item {content_hash}")
        return RawItem.model_validate_json(path.read_text(encoding="utf-8"))

    def iter_raw(self, route: str | None = None) -> Iterator[RawItem]:
        raw_dir = self.config.raw_dir
        if not raw_dir.exists():
            return
        for path in sorted(raw_dir.iterdir()):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            item = RawItem.model_validate_json(path.read_text(encoding="utf-8"))
            if route and item.route != route:
                continue
            yield item

    # ------------------------------------------------------------- ledger

    def decision_for(self, content_hash: str) -> Decision | None:
        path = self.config.ledger_dir / f"{content_hash}.json"
        if not path.exists():
            return None
        return Decision.model_validate_json(path.read_text(encoding="utf-8"))

    def record_decision(self, decision: Decision) -> None:
        _atomic_write(self.config.ledger_dir / f"{decision.content_hash}.json", _dump(decision))

    def iter_decisions(self) -> Iterator[Decision]:
        ledger_dir = self.config.ledger_dir
        if not ledger_dir.exists():
            return
        for path in sorted(ledger_dir.iterdir()):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            yield Decision.model_validate_json(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------- system journal

    SYSTEM_JOURNAL = "system-journal.jsonl"

    def record_system_event(self, kind: str, summary: str, actor: str,
                            detail: str | None = None) -> dict:
        """Log a change to the machinery itself — the profile being rewritten,
        anything else with no event to hang history on.

        Deliberately NOT the decision ledger: `_fewshot_examples` samples
        non-auto ledger entries as classifier training examples, so a
        synthetic decision there would teach the gate nonsense. Append-only
        JSONL, read by the Journal view and the alert pass.
        """
        entry = {"ts": utcnow(), "kind": kind, "summary": summary,
                 "actor": actor, "detail": detail}
        path = self.config.store_dir / self.SYSTEM_JOURNAL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as fh:
            # Start a fresh line if a previous write was cut off mid-record,
            # otherwise this entry would be glued onto the broken one and
            # both would be unreadable.
            if fh.tell():
                fh.seek(fh.tell() - 1)
                if fh.read(1) != "\n":
                    fh.write("\n")
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def iter_system_events(self, since: str | None = None) -> Iterator[dict]:
        path = self.config.store_dir / self.SYSTEM_JOURNAL
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a truncated tail must not hide the rest
            if since and entry.get("ts", "") <= since:
                continue
            yield entry

    # ------------------------------------------------------- source state

    def source_state(self, source_id: str) -> SourceState:
        path = self.config.sources_state_dir / f"{source_id}.json"
        if not path.exists():
            return SourceState(source_id=source_id)
        return SourceState.model_validate_json(path.read_text(encoding="utf-8"))

    def save_source_state(self, state: SourceState) -> None:
        _atomic_write(self.config.sources_state_dir / f"{state.source_id}.json", _dump(state))

    def iter_source_states(self) -> Iterator[SourceState]:
        state_dir = self.config.sources_state_dir
        if not state_dir.exists():
            return
        for path in sorted(state_dir.iterdir()):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            yield SourceState.model_validate_json(path.read_text(encoding="utf-8"))
