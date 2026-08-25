"""AI token-usage bookkeeping (issue #16).

Per-day aggregate per model, written next to the store so the dashboard can
show it. Recording must never break a classification call — failures are
swallowed."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .config import Config, load_config
from .store import _atomic_write


def record_usage(model: str, usage: dict | None, config: Config | None = None) -> None:
    try:
        config = config or load_config()
        usage_dir = config.store_dir / "llm_usage"
        usage_dir.mkdir(parents=True, exist_ok=True)
        path = usage_dir / f"{date.today().isoformat()}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entry = data.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        entry["calls"] += 1
        entry["prompt_tokens"] += int((usage or {}).get("prompt_tokens") or 0)
        entry["completion_tokens"] += int((usage or {}).get("completion_tokens") or 0)
        _atomic_write(path, json.dumps(data, indent=2) + "\n")
    except Exception:
        pass  # bookkeeping must never take down the pipeline


def usage_by_day(config: Config, days: int = 14) -> list[dict]:
    """[{day, calls, prompt_tokens, completion_tokens, total, models}] for the
    last N days, oldest first; days without calls included with zeros."""
    usage_dir = config.store_dir / "llm_usage"
    out = []
    today = date.today()
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        path = usage_dir / f"{day.isoformat()}.json"
        models = {}
        if path.exists():
            try:
                models = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                models = {}
        calls = sum(m.get("calls", 0) for m in models.values())
        prompt = sum(m.get("prompt_tokens", 0) for m in models.values())
        completion = sum(m.get("completion_tokens", 0) for m in models.values())
        out.append({"day": day.isoformat(), "calls": calls, "prompt_tokens": prompt,
                    "completion_tokens": completion, "total": prompt + completion,
                    "models": models})
    return out
