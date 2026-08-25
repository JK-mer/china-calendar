"""Committee-agenda TOP extraction (#22).

Accepted sittings from the Tagesordnungen RSS carry their agenda PDF as the
source url; this sweep step reads each not-yet-processed PDF, lets the
extractor model select the profile-relevant TOPs, and appends them as note
lines on the sitting event (calsync carries the note into the calendar
description). Selection only — the sitting's date came from the feed, and
the TOP lines quote the PDF's original wording.

Idempotency is per PDF: a `top_extract` history entry with the PDF basename
marks it processed (also when nothing was relevant, so quiet agendas are not
re-read daily). Ergänzungs-PDFs attach to the same event later and get their
own pass. Every failure (fetch, parse, model) skips that PDF and is counted
in the sweep report — never breaking the sweep.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

from .config import Config
from .fetch import Fetcher
from .llm import select_tops
from .store import Store

SOURCE_PREFIX = "pc-bundestag-ausschuss-to-"
MAX_AGENDA_CHARS = 15_000
ACTOR = "auto:top-extract"


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _processed(event, basename: str) -> bool:
    return any(h.field == "top_extract" and h.to == basename for h in event.history)


def run_top_extraction(store: Store, config: Config, fetcher: Fetcher,
                       today: date | None = None) -> dict:
    today = today or date.today()
    report = {"top_extract": True, "checked": 0, "annotated": 0, "errors": []}
    for event in list(store.iter_events()):
        if not event.uid.startswith(SOURCE_PREFIX):
            continue
        if event.start_date() < today:
            continue  # the calendar looks ahead; past agendas are moot
        for source in event.sources:
            url = source.url or ""
            if not url.lower().endswith(".pdf"):
                continue
            basename = url.rsplit("/", 1)[-1]
            if _processed(event, basename):
                continue
            report["checked"] += 1
            try:
                text = _pdf_text(fetcher.fetch_raw(url).content)[:MAX_AGENDA_CHARS]
                tops = select_tops(config.llm, _profile(config), text)
            except Exception as exc:  # fetch/parse/model — sweep must survive
                report["errors"].append(f"{event.uid} {basename}: {exc}")
                continue
            if tops:
                lines = [f"{t} [{basename}]" for t in tops
                         if t not in (event.note or "")]
                if lines:
                    new_note = "\n".join(filter(None, [event.note, *lines]))
                    store.amend(event.uid, {"note": new_note}, actor=ACTOR,
                                reason=f"relevant TOPs from {basename}")
                    report["annotated"] += 1
            # mark processed either way — quiet agendas are not re-read daily
            event = store.note_history(event.uid, "top_extract", basename,
                                       actor=ACTOR, reason="agenda PDF processed")
    return report


def _profile(config: Config) -> str:
    if config.profile_path.exists():
        return config.profile_path.read_text(encoding="utf-8")
    return "German foreign and foreign-economic policy; China and Asia-Pacific."
