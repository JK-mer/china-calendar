"""One-off: sync_authorized False → None where nobody chose it (#70).

The field became a tri-state (None = default policy, True/False = explicit
override). Every record predating that carries the old dataclass default
`False`, which under the new reading would mean "the human switched this
off" — and would empty the calendar.

History is the discriminator: a value nobody ever set is not a decision. The
audit at the time showed 191 False, 9 None, 2 True, and exactly two history
entries touching the field, both False → True — i.e. nobody had ever turned
a calendar off. This still checks per record rather than trusting that.

DO NOT RE-RUN after migration 74. 74 switches the Bundesrat sittings off
with a raw write and no history entry, so this script's discriminator
(history alone) would read those flags as untouched defaults and convert
them back to None — silently undoing #74 and returning 14 contextless
sittings to the shared calendar. The guard below refuses that.

`deploy/` is NOT copied into the image, so copy it in first:
    docker cp deploy/migrations/70-sync-authorized-tristate.py china-calendar-mcp:/tmp/mig70.py
    docker exec china-calendar-mcp python /tmp/mig70.py [--apply]
"""

import json
import pathlib
import sys

STORE = pathlib.Path("/data/store/events")


def chose_off(record: dict) -> bool:
    """Did a human ever set this to False? Only then is False a decision."""
    for entry in record.get("history") or []:
        if entry.get("field") == "sync_authorized" and entry.get("to") in (False, "False", "false"):
            return True
    return False


def _migration_74_has_run() -> bool:
    """74 marks Bundesrat sittings calendar-off without writing history."""
    for path in STORE.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("sync_authorized") is not False:
            continue
        if any("termine-sitzungen-bundesrat" in (s.get("url") or "")
               for s in record.get("sources") or []):
            return True
    return False


def main() -> int:
    apply = "--apply" in sys.argv
    if _migration_74_has_run():
        print("REFUSING: migration 74 has already run. Re-running this would "
              "convert its calendar-off flags back to None and put 14 "
              "contextless Bundesrat sittings back in the shared calendar.")
        return 1
    converted = kept = untouched = 0
    for path in sorted(STORE.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("sync_authorized") is not False:
            untouched += 1
            continue
        if chose_off(record):
            kept += 1
            print(f"  KEEP  {record['uid']} — a human set calendar off")
            continue
        converted += 1
        if apply:
            record["sync_authorized"] = None
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    verb = "converted" if apply else "would convert"
    print(f"{verb} {converted} False→None | kept {kept} deliberate off | "
          f"{untouched} already None/True")
    if not apply:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
