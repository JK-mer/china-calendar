"""One-off: take bare Bundesrat sittings out of the calendar (#74).

New records get `sync_authorized=False` from `calendar_default_off`, but the
14 already in the store predate it. A sitting that already carries an agenda
note has earned its place and is left alone, as is anything a human set by
hand — the same two exemptions the runtime rule uses.

Idempotent. Run inside the container:
    docker exec china-calendar-mcp python /tmp/mig74.py [--apply]
"""

import json
import pathlib
import sys

STORE = pathlib.Path("/data/store/events")
SOURCES = ("bundesrat-plenum-2026", "bundesrat-plenum-2027")


def is_bundesrat_skeleton(record: dict) -> bool:
    return any(
        (s.get("url") or "").find("termine-sitzungen-bundesrat") >= 0
        for s in record.get("sources") or []
    ) or record.get("uid", "").startswith(tuple(f"pc-{s}" for s in SOURCES))


def human_touched(record: dict) -> bool:
    return any(h.get("field") == "sync_authorized"
               and str(h.get("actor", "")).startswith("human:")
               for h in record.get("history") or [])


def main() -> int:
    apply = "--apply" in sys.argv
    off = kept_note = kept_human = 0
    for path in sorted(STORE.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not is_bundesrat_skeleton(record) or record.get("removed"):
            continue
        if (record.get("note") or "").strip():
            kept_note += 1
            print(f"  KEEP  {record['uid']} — carries an agenda note")
            continue
        if human_touched(record):
            kept_human += 1
            print(f"  KEEP  {record['uid']} — a human set the calendar flag")
            continue
        if record.get("sync_authorized") is False:
            continue  # already off
        off += 1
        if apply:
            record["sync_authorized"] = False
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    verb = "switched off" if apply else "would switch off"
    print(f"{verb} {off} | kept {kept_note} with notes, {kept_human} human-set")
    if not apply:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
