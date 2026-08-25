"""Which recurring formats should have a date in a window, and don't (#66).

Some events are never announced far enough ahead to scrape — the CCFEA, an
EU-China summit, an 8th round of Regierungskonsultationen. For those,
remembering to look IS the mechanism, and `gaps()` cannot help: it counts
events per month, so a month holding three elections looks healthy while the
Party Congress is missing from it.

The registry is `glossary.yaml`, which already describes what we watch and is
deliberately detached from the event store. One file, two consumers: the
dashboard renders the prose, this module reads the optional `expect:` blocks.
`render_glossary` reads a fixed key tuple, so `expect:` stays invisible there.

This module NEVER writes. An expectation is not a date — it says where to
look, and the caller still has to research and write through the provenance
gate. A registry that seeded its own projections would be a fabrication
engine with a registry for a fig leaf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

GLOSSARY = Path(__file__).parent / "glossary.yaml"

# One occurrence per listed month; everything else is one per qualifying year
# somewhere inside the listed months. The distinction is load-bearing: the
# G20 is annual but drifts across Nov/Dec, and listing both months under a
# per-month rule would invent a second summit every year.
PER_MONTH_RULE = "every_n_months"
RULES = {"annual", PER_MONTH_RULE, "n_yearly", "irregular"}


@dataclass
class Occurrence:
    period: str                    # "2026-10" for per-month rules, "2026" otherwise
    verdict: str                   # covered | missing
    events: list[dict] = field(default_factory=list)


@dataclass
class Expectation:
    name: str
    cluster: str
    rule: str
    occurrences: list[Occurrence]
    verdict: str                   # covered | missing | partial | watch
    confirms: str | None = None
    lead: str | None = None
    look: str | None = None
    note: str | None = None
    last_seen: dict | None = None          # irregular items: most recent match, any date
    stale_projections: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {
            "format": self.name,
            "cluster": self.cluster,
            "rule": self.rule,
            "verdict": self.verdict,
            "occurrences": [
                {"period": o.period, "verdict": o.verdict, "events": o.events}
                for o in self.occurrences
            ],
        }
        for key in ("confirms", "lead", "look", "note", "last_seen"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.stale_projections:
            out["stale_projections"] = self.stale_projections
        return out


def load_registry(path: Path | None = None) -> list[dict]:
    """Registry items that carry an `expect:` block, with their cluster."""
    groups = yaml.safe_load((path or GLOSSARY).read_text(encoding="utf-8"))
    items = []
    for group in groups:
        for item in group.get("items", []):
            if isinstance(item.get("expect"), dict):
                items.append({"cluster": group["cluster"], **item})
    return items


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        month = 1 if month == 12 else month + 1
        if month == 1:
            year += 1
    return out


def expected_periods(expect: dict, start: date, end: date) -> list[str]:
    """Expand a cadence rule into the periods it should produce in a window.

    Per-month rules yield "YYYY-MM"; annual and n-yearly rules yield "YYYY",
    because the month within the candidate range is exactly what we do not
    claim to know.
    """
    rule = expect.get("rule")
    months = set(expect.get("months") or [])
    if rule == "irregular":
        return []
    if rule == PER_MONTH_RULE:
        return [f"{y:04d}-{m:02d}" for y, m in _months_between(start, end)
                if not months or m in months]
    if rule in ("annual", "n_yearly"):
        every = int(expect.get("every_years", 1)) if rule == "n_yearly" else 1
        anchor = int(expect.get("anchor_year", start.year))
        periods = []
        for year in range(start.year, end.year + 1):
            if every > 1 and (year - anchor) % every:
                continue
            # Only count the year if a candidate month actually falls inside
            # the window — a December format is not "missing" from a window
            # that ends in March.
            if months and not any(
                (year, m) in _months_between(start, end) for m in months
            ):
                continue
            periods.append(f"{year:04d}")
        return periods
    return []


def _titles(event: Any) -> str:
    return " ".join(
        str(getattr(event, attr, "") or "")
        for attr in ("title_en", "title_de", "title_zh")
    ).lower()


def _matches(event: Any, needles: list[str]) -> bool:
    blob = _titles(event)
    return any(n.lower() in blob for n in needles)


def _period_of(event: Any, rule: str) -> str:
    iso = event.start_date().isoformat()
    return iso[:7] if rule == PER_MONTH_RULE else iso[:4]


def _brief(event: Any) -> dict:
    return {
        "uid": event.uid,
        "start": str(event.start)[:10],
        "status": getattr(event.status, "value", str(event.status)),
        "title": event.title_en or event.title_de or "",
    }


def evaluate(store: Any, from_date: date, to_date: date,
             today: date | None = None,
             registry: list[dict] | None = None) -> list[Expectation]:
    """Compare the registry against the store over a window."""
    today = today or date.today()
    items = registry if registry is not None else load_registry()
    # Coverage is judged over the whole period, not the caller's window edges.
    # A window ending 9 November still asks "does November have a plenary?",
    # and the answer must consider the 23rd — otherwise every window boundary
    # manufactures a miss, and a registry that cries wolf gets ignored.
    # Reach back a year further than the window: a projection that expired
    # LAST month is the exact silent-loss case `stale_projections` exists for,
    # and querying only from the window start excluded it from `hits` — so the
    # one engine that notices these could never see the freshest ones.
    candidates = store.search(
        from_=date(from_date.year - 1, from_date.month, 1),
        to=date(to_date.year, 12, 31),
    )
    results = []

    for item in items:
        expect = item["expect"]
        rule = expect.get("rule", "irregular")
        needles = expect.get("match") or [item["name"]]
        hits = [e for e in candidates if _matches(e, needles)]

        # A projection whose window closed with nothing promoting it is the
        # silent-loss case: expire_stale_triage only covers pending triage
        # items, so nothing else in the system notices these.
        stale = [
            _brief(e) for e in hits
            if getattr(e.status, "value", str(e.status)) == "projected"
            and e.end_date() < today
        ]

        occurrences: list[Occurrence] = []
        for period in expected_periods(expect, from_date, to_date):
            matched = [_brief(e) for e in hits if _period_of(e, rule) == period]
            occurrences.append(Occurrence(
                period=period,
                verdict="covered" if matched else "missing",
                events=matched,
            ))

        if rule == "irregular":
            verdict = "watch"
        elif rule not in RULES:
            # A typo (`rule: anual`) expands to no occurrences, and "no
            # occurrences" used to mean "covered" — so one misspelling silenced
            # a format forever, in the one tool whose entire job is noticing
            # absence. Unknown rules degrade to a standing watch instead:
            # visible, never a false all-clear.
            verdict = "watch"
        elif not occurrences:
            verdict = "covered"
        elif all(o.verdict == "covered" for o in occurrences):
            verdict = "covered"
        elif any(o.verdict == "covered" for o in occurrences):
            verdict = "partial"
        else:
            verdict = "missing"

        last_seen = None
        if verdict == "watch":
            seen = sorted(
                (e for e in store.iter_events() if _matches(e, needles)),
                key=lambda e: e.start_date(),
            )
            if seen:
                last_seen = _brief(seen[-1])

        results.append(Expectation(
            name=item["name"], cluster=item["cluster"], rule=rule,
            occurrences=occurrences, verdict=verdict,
            confirms=expect.get("confirms"), lead=expect.get("lead"),
            look=expect.get("look"),
            note=(expect.get("note") if rule in RULES else
                  f"CONFIG ERROR: unknown rule {rule!r} — this format is not being "
                  f"checked; fix the expect: block in glossary.yaml"),
            last_seen=last_seen, stale_projections=stale,
        ))
    return results


def summarise(expectations: list[Expectation]) -> dict:
    """Counts plus the two lists a caller actually acts on."""
    missing = [
        {"format": e.name, "cluster": e.cluster, "periods":
            [o.period for o in e.occurrences if o.verdict == "missing"],
         "confirms": e.confirms, "look": e.look}
        for e in expectations if e.verdict in ("missing", "partial")
    ]
    watch = [
        {"format": e.name, "cluster": e.cluster, "look": e.look,
         "last_seen": e.last_seen, "note": e.note}
        for e in expectations if e.verdict == "watch"
    ]
    stale = [s | {"format": e.name} for e in expectations for s in e.stale_projections]
    return {
        "checked": len(expectations),
        "covered": sum(1 for e in expectations if e.verdict == "covered"),
        "missing": missing,
        "standing_watch": watch,
        "stale_projections": stale,
    }
