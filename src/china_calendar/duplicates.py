"""Cross-source duplicate suggestion (#68).

A second source reporting an event we already hold is *evidence*, not noise:
PECC independently agreeing on the ASEAN summit date should strengthen that
record. Before this, the only way to dispose of such an item was to reject it
in triage — which taught the classifier, through `_fewshot_examples`, that
APEC leaders' meetings are irrelevant. The opposite of true, on one of the
highest-value formats tracked.

**Suggest only.** This module never merges anything. It proposes a candidate
and a human confirms, because a false automatic merge silently fuses two
genuinely different events and is close to undetectable afterwards. The
asymmetry sets the threshold: a false suggestion costs a glance, a missed one
costs a duplicate — so matching favours recall and leans loose.

Scope is cross-source only. Within one source, `external_id` already handles
identity exactly and needs no guessing.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

# A window rather than an exact match: sources disagree by a day on multi-day
# events all the time (PECC had the ASEAN summit as 10-12 November against our
# 10-12, but COP31 as 9-20 against a record built from a leaders'-segment date).
DAY_SLACK = 3

# Deliberately low: every hit is confirmed by a human, so a missed match costs
# more than an extra glance. It is safe to be this low only because matching
# is gated on shared distinctive words as well — see `find_candidate`.
MIN_SCORE = 0.34

# Two shared words, or one that accounts for the whole of the shorter title
# ("FOCAC" inside "10th FOCAC Ministerial Conference"). A single incidental
# word in common is how "49th ASEAN Summit" matched "COP31 World Leaders
# Summit" in the first live run.
MIN_SHARED = 2

# Words carrying no identifying signal: nearly every event here is a meeting,
# a summit or a session, so sharing one says nothing about identity.
_NOISE = {"the", "of", "and", "on", "for", "to", "in", "at", "a", "an",
          "meeting", "meetings", "session", "annual", "st", "nd", "rd", "th",
          "summit", "conference", "plenary", "ministerial", "ministers",
          "forum", "window", "projected"}


def _normalise(title: str) -> str:
    text = title.lower()
    # Split letter/digit runs so "COP31" and "COP 31" become the same tokens —
    # without this the two spellings of the same conference never match.
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(title: str) -> set[str]:
    return {t for t in _normalise(title).split() if t not in _NOISE and len(t) > 1}


def score(a: str, b: str) -> tuple[float, int, bool]:
    """(containment, shared word count, whether the shorter title is contained).

    Containment over distinctive words only — NOT sequence similarity. Character
    similarity is what matched "APEC Finance Ministerial Meeting" to "ADMM-Plus
    (ASEAN Defence Ministers' Meeting-Plus)" at 0.47 on the first live run, with
    not a single word in common. Letters in common are not evidence; words are.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0, 0, False
    shared = ta & tb
    smaller = min(len(ta), len(tb))
    return len(shared) / smaller, len(shared), len(shared) == smaller


def _tiebreak(a: str, b: str) -> float:
    """Only ever separates two candidates that already qualified."""
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _span(start: str | None, end: str | None) -> tuple[date, date] | None:
    try:
        first = date.fromisoformat(str(start)[:10])
    except (TypeError, ValueError):
        return None
    try:
        last = date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        last = first
    return (first, last) if last >= first else (last, first)


def _overlaps(a: tuple[date, date], b: tuple[date, date], slack: int = DAY_SLACK) -> bool:
    return (a[0] - timedelta(days=slack)) <= b[1] and (b[0] - timedelta(days=slack)) <= a[1]


def find_candidate(store: Any, item: Any) -> dict | None:
    """The best existing event this raw item might duplicate, or None.

    Returns {uid, title, start, score, why} — a suggestion for a human, never
    an instruction.
    """
    item_span = _span(item.start, item.end)
    if item_span is None:
        return None  # an undated item cannot be matched on anything but words

    best: dict | None = None
    best_rank: tuple = ()
    for event in store.iter_events():
        if event.removed:
            continue
        if event.uid and getattr(item, "event_uid", None) == event.uid:
            continue
        event_span = _span(event.start, event.end)
        if event_span is None or not _overlaps(item_span, event_span):
            continue
        value, shared, fully = score(item.title, event.title() or "")
        if value < MIN_SCORE or (shared < MIN_SHARED and not fully):
            continue
        # An event already carrying this exact url IS the match — it is already
        # corroborated by this source. Skipping it (the first implementation)
        # let a worse candidate win: the 49th ASEAN Summit matched its own
        # record at 100%, was skipped for holding the same PECC url, and the
        # suggestion fell through to the COP31 leaders' summit instead.
        attached = any(s.url and item.url and s.url == item.url for s in event.sources)
        rank = (value, _tiebreak(item.title, event.title() or ""))
        if best is None or rank > best_rank:
            best_rank = rank
            best = {
                "uid": event.uid,
                "title": event.title(),
                "start": str(event.start)[:10],
                "status": getattr(event.status, "value", str(event.status)),
                "score": round(value, 3),
                "shared": shared,
                "already_attached": attached,
                "why": ("this source is already attached — the item is a repeat"
                        if attached else
                        f"dates overlap, {shared} distinctive words in common"),
            }
    return best
