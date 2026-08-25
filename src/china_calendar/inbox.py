"""Inbox drop folder (issue #17): manually exported files through the gate.

store/inbox/ is a directory on the media server; reaching it means the host
or a share mounted over it.
Each sweep parses what it finds (.ics for now), routes the items
through the normal selection gate, and moves the file to inbox/processed/.
Provenance is honest: Tier 0 / manual, no URL — the human vouches for the
file, so there is nothing for the verify pass to string-match. First use
case: the Bundestag Sitzungswochen ICS, downloaded in the user's own browser
while their WAF locks our fetchers out.
"""

from __future__ import annotations

from datetime import date

from .config import Config
from .gate import run_gate
from .models import slugify
from .sources.base import SourceConfig
from .sources.tier1_ics import parse_ics
from .store import Store


def process_inbox(store: Store, config: Config, use_llm: bool = True,
                  since: date | None = None) -> list[dict]:
    inbox_dir = config.store_dir / "inbox"
    if not inbox_dir.exists():
        return []
    processed_dir = inbox_dir / "processed"
    reports = []
    for path in sorted(inbox_dir.glob("*.ics")):
        cfg = SourceConfig(
            id=f"inbox-{slugify(path.stem)}",
            tier=0,
            kind="ics",
            url="",
            auto_accept=False,
            notes=f"manual file drop: {path.name}",
        )
        try:
            items = list(parse_ics(cfg, path.read_bytes()))
        except Exception as exc:
            reports.append({"inbox": path.name, "error": f"parse failed: {exc}"})
            continue  # file stays in the inbox so the problem is visible
        for item in items:
            item.url = None  # nothing fetchable behind a hand-delivered file
        cutoff = since or date.today()
        items = [i for i in items if (i.end or i.start or "")[:10] >= cutoff.isoformat()]
        counts = run_gate(store, config, cfg, items, use_llm=use_llm)
        processed_dir.mkdir(parents=True, exist_ok=True)
        path.rename(processed_dir / path.name)
        reports.append({"inbox": path.name, "items": len(items), **counts})
    return reports
