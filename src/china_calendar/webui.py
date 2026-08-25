"""Web dashboard — the glanceable surface (issue #6).

Third thin adapter over the same core as CLI and MCP server (invariant I2):
every write goes through gate.triage_decide / store.remove / store.restore /
store.amend_and_requeue / verify.*. Five views: Overview (triage + preview +
watch), Calendar (month grid), Events (filterable store, bulk actions,
per-event verify, detail tile with inline amend — #56), Journal
(what changed, and rejected items with a human override — overrides become
ledger decisions and feed the few-shot loop), Glossary (recurring formats).
UI language is English (#35); German terms of art and auto-pulled content
stay in their original language. Route names (/kalender, /glossar, von/bis)
are stable identifiers, not display text.

Server-rendered HTML; the only JavaScript is a select-all checkbox helper.
The /api/* routes are the exception: summary, queue, upcoming and a POST
triage, so the home dashboard can show and decide the queue without
scraping this markup (#31, #32). They are adapters like everything else
here — every write still goes through gate.triage_decide.
Access control is the bind address (127.0.0.1 / docker0), per MCP Stack
conventions. Every stored string is HTML-escaped — feed titles are untrusted
— links are scheme-checked, and form POSTs must be same-origin,
because "only reachable from the LAN" is not the same as "only reachable by
you": any page in a LAN browser can post here.
"""

from __future__ import annotations

import html
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from .calsync import wants_alarm
from .clusters import CLUSTER_LABELS, cluster_of
from .config import load_config
from .gate import pending_triage, triage_decide
from .models import Event
from .store import Store, StoreError
from .sweep import ZERO_RUNS_FLAG

CONFIG = load_config()
STORE = Store(CONFIG)

STAMP_CLASS = {
    "confirmed": "st-confirmed",
    "scheduled": "st-scheduled",
    "rumored": "st-rumored",
    "projected": "st-projected",
    "unverified": "st-unverified",
}

CSS = """
:root {
  --paper: #e9eceb; --card: #f8f9f8; --ink: #1b1f23; --ink-soft: #55605e;
  --rule: #c8cfcd; --seal: #b3261e;
  --c-confirmed: #1e6b4a; --c-scheduled: #23527a; --c-rumored: #9a6a00;
  --c-projected: #6b5b7a; --c-unverified: #b3261e;
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--paper); color: var(--ink);
  font: 15px/1.5 "Source Sans 3", "Segoe UI", system-ui, sans-serif;
  max-width: 1100px; margin: 0 auto; padding: 2rem 1.25rem 4rem;
}
.mono { font-family: ui-monospace, "Cascadia Code", "JetBrains Mono", monospace; }
header { border-bottom: 3px double var(--ink); padding-bottom: .75rem; margin-bottom: 1.4rem; }
header h1 { font-size: 1.35rem; letter-spacing: .12em; text-transform: uppercase; font-weight: 700; }
.registry { display: flex; gap: 1.5rem; flex-wrap: wrap; margin-top: .4rem;
  font-family: ui-monospace, monospace; font-size: .8rem; color: var(--ink-soft); }
.registry strong { color: var(--ink); }
nav { display: flex; gap: 1.25rem; margin-bottom: 2rem; font-size: .85rem;
  letter-spacing: .06em; text-transform: uppercase; }
nav a { color: var(--ink); text-decoration: none; border-bottom: 2px solid transparent; padding-bottom: .15rem; }
nav a.active { border-bottom-color: var(--seal); font-weight: 700; }
nav a:hover { border-bottom-color: var(--ink); }
section { margin-bottom: 2.5rem; }
.eyebrow { display: flex; align-items: baseline; gap: .75rem; border-bottom: 1px solid var(--ink);
  padding-bottom: .3rem; margin-bottom: .9rem; }
.eyebrow .de { font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: .85rem; }
.eyebrow .en { color: var(--ink-soft); font-size: .8rem; }
.eyebrow .count { margin-left: auto; font-family: ui-monospace, monospace; font-size: .8rem; }
.stamp { display: inline-block; font-family: ui-monospace, monospace; font-size: .68rem;
  letter-spacing: .09em; text-transform: uppercase; padding: .1rem .45rem;
  border: 1.5px solid; border-radius: 2px; }
.st-confirmed { color: var(--c-confirmed); border-color: var(--c-confirmed); }
.st-scheduled { color: var(--c-scheduled); border-color: var(--c-scheduled); }
.st-rumored { color: var(--c-rumored); border-color: var(--c-rumored); }
.st-projected { color: var(--c-projected); border-color: var(--c-projected); }
.st-unverified { color: var(--c-unverified); border-color: var(--c-unverified); border-style: dashed; }
.row { display: grid; grid-template-columns: 1.4rem 13.5rem 7.5rem 1fr; gap: .8rem;
  padding: .5rem .25rem; border-bottom: 1px solid var(--rule); align-items: baseline; }
.row.nosel { grid-template-columns: 13.5rem 7.5rem 1fr; }
/* A grid item is blockified, so a bare .stamp stretches across its whole
   7.5rem column — a confirmed row was the only one showing it, because every
   other status wraps the stamp in the .verify <details> and shrinks to text
   inside that. Shrink-to-fit both, so the column looks the same either way. */
.row > .stamp, .row > .verify { justify-self: start; }
.row .date { font-family: ui-monospace, monospace; font-size: .82rem; white-space: nowrap; }
.row .title { font-weight: 600; }
.row .meta { color: var(--ink-soft); font-size: .8rem; }
.row.removed .title { text-decoration: line-through; color: var(--ink-soft); }
.cluster-h { font-size: .78rem; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-soft); margin: 1.1rem 0 .2rem; }
.tri { background: var(--card); border: 1px solid var(--rule); border-left: 4px solid var(--ink);
  padding: .8rem 1rem; margin-bottom: .8rem; }
.tri .head { display: flex; gap: 1rem; align-items: baseline; flex-wrap: wrap; }
.tri .desc { color: var(--ink-soft); font-size: .85rem; margin: .35rem 0 .4rem; max-width: 70ch; }
.tri .hint { font-size: .78rem; color: var(--c-rumored); margin-bottom: .3rem; }
.bulkbar { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
  background: var(--card); border: 1px solid var(--rule); padding: .6rem .8rem; margin: .8rem 0; }
.bulkbar input[type=text] { flex: 1 1 12rem; border: 1px solid var(--rule); background: #fff;
  padding: .35rem .5rem; font: inherit; font-size: .85rem; }
.bulkbar button { padding-inline: .55rem; }
.filters { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;
  font-size: .85rem; }
.filters input[type=text], .filters select { border: 1px solid var(--rule); background: #fff;
  padding: .3rem .5rem; font: inherit; font-size: .85rem; }
button { font: inherit; font-size: .82rem; letter-spacing: .04em; padding: .35rem .9rem;
  border: 1.5px solid var(--ink); background: transparent; color: var(--ink); cursor: pointer; }
button:hover { background: var(--ink); color: var(--paper); }
button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible {
  outline: 2px solid var(--c-scheduled); outline-offset: 2px; }
button.accept { border-color: var(--c-confirmed); color: var(--c-confirmed); }
button.accept:hover { background: var(--c-confirmed); color: #fff; }
button.reject, button.danger { border-color: var(--seal); color: var(--seal); }
button.reject:hover, button.danger:hover { background: var(--seal); color: #fff; }
button.small { padding: .15rem .55rem; font-size: .75rem; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th { text-align: left; font-size: .72rem; letter-spacing: .09em; text-transform: uppercase;
  color: var(--ink-soft); border-bottom: 1px solid var(--ink); padding: .25rem .5rem .25rem 0; }
td { border-bottom: 1px solid var(--rule); padding: .4rem .5rem .4rem 0; vertical-align: baseline; }
.flag { color: var(--seal); font-weight: 600; }
.empty { color: var(--ink-soft); font-style: italic; padding: .4rem 0; }
.folded { color: var(--ink-soft); font-size: .78rem; margin: -.4rem 0 .8rem; }
footer { margin-top: 3rem; border-top: 1px solid var(--ink); padding-top: .6rem;
  font-size: .8rem; color: var(--ink-soft); display: flex; gap: 1.5rem; flex-wrap: wrap; }
a { color: var(--c-scheduled); }
.notice { background: #fff; border-left: 4px solid var(--c-confirmed); padding: .6rem 1rem;
  margin-bottom: 1.5rem; font-size: .88rem; }
.journal-reason { color: var(--ink-soft); }
.calwrap { overflow-x: auto; }
table.cal { table-layout: fixed; min-width: 720px; }
.cal th { text-align: center; padding: .25rem 0; }
.cal td { vertical-align: top; height: 6.5rem; padding: .3rem .35rem; border: 1px solid var(--rule);
  background: var(--card); }
.cal td.out { background: transparent; }
.cal td.out .dnum { opacity: .4; }
.cal td.today { outline: 2px solid var(--seal); outline-offset: -2px; }
.dnum { font-family: ui-monospace, monospace; font-size: .72rem; color: var(--ink-soft); }
.chip { display: block; font-size: .72rem; line-height: 1.3; margin-top: .2rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-left: 3px solid;
  padding-left: .3rem; text-decoration: none; }
.chip.cont { opacity: .5; }
.dot { display: inline-block; width: .5rem; height: .5rem; border-radius: 50%; margin-right: .25rem; }
.k-german_institutional { background: #1b1f23; } .k-eu { background: #2f4b8f; }
.k-business_formats { background: #8a6d1f; } .k-china { background: #a03030; }
.k-elections { background: #0f6f7a; } .k-other { background: #7a8480; }
.legend { display: flex; gap: 1.1rem; flex-wrap: wrap; margin-top: .6rem;
  font-size: .78rem; color: var(--ink-soft); align-items: center; }
.pager { display: flex; gap: 1rem; justify-content: center; align-items: baseline;
  font-family: ui-monospace, monospace; font-size: .8rem; margin: .9rem 0; }
.verify { font-size: .78rem; position: relative; display: inline-block; }
.verify summary { cursor: pointer; list-style: none; }
.verify summary::-webkit-details-marker { display: none; }
.verify summary:hover .stamp, .verify[open] summary .stamp {
  background: var(--ink); color: var(--paper); border-color: var(--ink); }
.verifybox { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center;
  background: var(--card); border: 1px solid var(--rule); padding: .5rem .6rem;
  position: absolute; z-index: 20; top: calc(100% + .3rem); left: 0;
  width: min(36rem, 85vw); box-shadow: 0 4px 16px rgba(0,0,0,.18); }
.verifybox input[type=text], .verifybox input[type=url] { border: 1px solid var(--rule);
  background: #fff; padding: .25rem .45rem; font: inherit; font-size: .78rem; }
.verifybox input[name=url] { flex: 1 1 13rem; }
.verifybox input[name=evidence] { flex: 2 1 16rem; }
.detail { position: relative; display: inline-block; }
.detail summary { cursor: pointer; list-style: none; }
.detail summary::-webkit-details-marker { display: none; }
.detail summary:hover .title, .detail[open] summary .title { text-decoration: underline; }
.detailbox { background: var(--card); border: 1px solid var(--rule); padding: .7rem .8rem;
  position: absolute; z-index: 20; top: calc(100% + .3rem); left: 0;
  width: min(44rem, 90vw); max-height: 26rem; overflow-y: auto;
  box-shadow: 0 4px 16px rgba(0,0,0,.18); font-size: .82rem; font-weight: 400;
  text-decoration: none; }
.detailbox .dhead { font-family: ui-monospace, monospace; font-size: .74rem;
  color: var(--ink-soft); margin-bottom: .35rem; }
.detailbox .dnote { max-width: 72ch; margin-bottom: .4rem; white-space: pre-line; }
.detailbox .dsec { font-size: .7rem; letter-spacing: .09em; text-transform: uppercase;
  color: var(--ink-soft); border-bottom: 1px solid var(--rule); margin: .55rem 0 .25rem; }
.detailbox .dsrc, .detailbox .dhist { font-size: .78rem; margin-bottom: .3rem;
  overflow-wrap: anywhere; }
.detailbox .dhist { font-family: ui-monospace, monospace; font-size: .72rem; }
.amendform { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; margin-top: .35rem; }
.amendform input[type=text], .amendform textarea { border: 1px solid var(--rule);
  background: #fff; padding: .25rem .45rem; font: inherit; font-size: .78rem; }
.amendform label { font-size: .74rem; color: var(--ink-soft); }
.amendform textarea { flex: 1 1 100%; }
.amendform input[name=reason] { flex: 2 1 14rem; }
.amendform .hint { flex: 1 1 100%; font-size: .72rem; color: var(--ink-soft); }
.gloss { border-bottom: 1px solid var(--rule); padding: .55rem .25rem; }
.gloss .gname { font-weight: 600; }
.gloss .gmeta { font-family: ui-monospace, monospace; font-size: .74rem;
  color: var(--ink-soft); margin: .1rem 0 .15rem; }
.gloss .gblurb { font-size: .88rem; max-width: 78ch; }
@media (max-width: 720px) { .row, .row.nosel { grid-template-columns: 1fr; gap: .1rem; } }
"""

# Inline SVG favicon: a red seal with 中, matching the --seal accent. Data
# URI, so no static-file route is needed.
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='18' "
           "fill='%23b3261e'/%3E%3Ctext x='50' y='74' font-size='64' "
           "text-anchor='middle' fill='white' font-family='serif'%3E%E4%B8%AD"
           "%3C/text%3E%3C/svg%3E")

SELECT_ALL_JS = """
<script>
function toggleAll(box, name) {
  document.querySelectorAll('input[name="' + name + '"]').forEach(cb => cb.checked = box.checked);
}
// verify + detail popups (#43/#56): Esc or a click outside closes any open one
document.addEventListener('click', e => {
  document.querySelectorAll('details.verify[open], details.detail[open]').forEach(d => {
    if (!d.contains(e.target)) d.removeAttribute('open');
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape')
    document.querySelectorAll('details.verify[open], details.detail[open]')
      .forEach(d => d.removeAttribute('open'));
});
</script>
"""


def esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


# Outbound links leave the dashboard, so they open in a new tab — following
# one mid-triage should not cost the user their place. noopener/noreferrer
# because these URLs come from scraped pages (#51).
NEW_TAB = ' target="_blank" rel="noopener noreferrer"'


def safe_href(url) -> str | None:
    """Escaped href, or None if the scheme is not one we will link to.

    Tier-2 parsers lift hrefs out of scraped pages, so a hostile or
    compromised source site could otherwise plant `javascript:` in a link
    that executes in this origin when clicked.
    """
    if not url:
        return None
    scheme = urlsplit(str(url)).scheme.lower()
    return esc(url) if scheme in ("http", "https") else None


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def fmt_date(iso: str | None) -> str:
    """'2026-09-25' → '25 September 2026'; timed → '25 September 2026, 09:30'."""
    if not iso:
        return "?"
    try:
        day = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    text = f"{day.day} {MONTHS[day.month - 1]} {day.year}"
    if len(iso) > 10:
        text += f", {iso[11:16]}"
    return text


def fmt_compact(iso: str | None) -> str:
    """'2026-09-25' → '25.09.2026'; timed → '25.09.2026, 09:30'. Used in the
    dense rows (Vorschau/Bestand); triage cards keep the written-out form."""
    if not iso:
        return "?"
    try:
        day = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    text = f"{day.day:02d}.{day.month:02d}.{day.year}"
    if len(iso) > 10:
        text += f", {iso[11:16]}"
    return text


def span_of(event: Event) -> str:
    start, end = event.start, event.end
    if not end or end == start:
        return fmt_compact(start)
    if start[:10] == end[:10] and len(start) > 10 and len(end) > 10:
        # same-day timed event: date once, time range ("25.09.2026, 09:30–12:30")
        return f"{fmt_compact(start)}–{end[11:16]}"
    return f"{fmt_compact(start)} → {fmt_compact(end)}"


def stamp(status: str) -> str:
    return f'<span class="stamp {STAMP_CLASS.get(status, "")}">{esc(status)}</span>'


def _verifiable(event: Event) -> bool:
    return not event.removed and event.status.value != "confirmed"


def verify_details(event: Event, back: str, scope: str = "") -> str:
    """Verify popup, triggered by clicking the status stamp itself (#33/#42).
    Rows may live inside a bulk-action form and a nested <form> is invalid
    HTML — the inputs reference a sibling form (see popup_forms) via the
    form= attribute. `back` is where the redirect returns to.

    `scope` distinguishes sections: one event can be listed twice on a page,
    and ids must stay unique or the browser submits the wrong copy's inputs
    (#51).
    """
    fid = f"vf-{esc(scope)}{esc(event.uid)}"
    # prefill from the first URL-bearing source (#43): verification and
    # correction usually start from what is already there. Submitting the
    # values unchanged re-checks that source (core dedupes) rather than
    # appending a duplicate.
    prefill = next((s for s in event.sources if s.url), None)
    url_val = f' value="{esc(prefill.url)}"' if prefill else ""
    ev_val = f' value="{esc(prefill.evidence)}"' if prefill and prefill.evidence else ""
    return (
        f'<details class="verify"><summary title="Click to verify this event">'
        f"{stamp(event.status.value)}</summary>"
        f'<div class="verifybox">'
        f'<input form="{fid}" type="url" name="url" placeholder="Source URL"{url_val}>'
        f'<input form="{fid}" type="text" name="evidence" '
        f'placeholder="Evidence — exact string from the page, original language"{ev_val}>'
        f'<label><input form="{fid}" type="checkbox" name="official" value="1"> '
        f"official announcement</label>"
        f'<button form="{fid}" class="accept small" name="mode" value="fetch">'
        f"Fetch &amp; match</button>"
        f'<button form="{fid}" class="small" name="mode" value="human">'
        f"I checked this myself</button>"
        f'<input form="{fid}" type="hidden" name="uid" value="{esc(event.uid)}">'
        f'<input form="{fid}" type="hidden" name="back" value="{esc(back)}">'
        f"</div></details>"
    )


def popup_forms(events: list[Event], scope: str = "") -> str:
    """The sibling forms the popup inputs reference — rendered OUTSIDE any
    other form (nested forms are invalid HTML). One verify (vf-) and one
    amend (af-) form per (scope, uid): ids must be unique on the page, or
    the handler reads the wrong copy's inputs (#51).
    """
    seen = set()
    forms = []
    for event in events:
        if event.uid in seen:
            continue
        seen.add(event.uid)
        if _verifiable(event):
            forms.append(f'<form id="vf-{esc(scope)}{esc(event.uid)}" '
                         f'method="post" action="/events/verify"></form>')
        if not event.removed:
            forms.append(f'<form id="af-{esc(scope)}{esc(event.uid)}" '
                         f'method="post" action="/events/amend"></form>')
    return "".join(forms)


def _fmt_ts(ts) -> str:
    return fmt_compact(ts) if ts else "—"


def detail_tile(event: Event, back: str, scope: str = "") -> str:
    """Detail popup (#56): the row title is the trigger. Shows what the row
    cannot — the note, every source with its stamps, recent history — and
    carries the inline amend form. Inputs reference a sibling form (see
    popup_forms): rows may live inside a bulk-action form and a nested
    <form> is invalid HTML."""
    fid = f"af-{esc(scope)}{esc(event.uid)}"
    head = (f'<div class="dhead">{esc(event.uid)} · T{event.tier} · '
            f"{esc(event.provenance.value)} · {esc(event.status.value)}</div>")
    note = f'<p class="dnote">{esc(event.note)}</p>' if event.note else ""
    srcs = ['<div class="dsec">Sources</div>']
    for s in event.sources:
        href = safe_href(s.url)
        link = f'<a href="{href}"{NEW_TAB}>{esc(s.url)}</a>' if href else "(no url)"
        stamps = f"retrieved {_fmt_ts(s.retrieved_at)} · verified {_fmt_ts(s.verified_at)}"
        ev = f"<br>&ldquo;{esc(s.evidence)}&rdquo;" if s.evidence else ""
        srcs.append(f'<div class="dsrc">{link} · {stamps}{ev}</div>')
    hist = ['<div class="dsec">History (recent)</div>']
    for h in list(reversed(event.history))[:6]:
        arrow = f"{esc(h.from_)} &rarr; {esc(h.to)}" if h.from_ is not None else esc(h.to)
        reason = f" · {esc(h.reason)}" if h.reason else ""
        hist.append(f'<div class="dhist">{_fmt_ts(h.ts)} · {esc(h.field)}: {arrow} · '
                    f"{esc(h.actor)}{reason}</div>")
    form = ""
    if not event.removed:
        form = (
            f'<div class="dsec">Amend</div><div class="amendform">'
            f'<label>start <input form="{fid}" type="text" name="start" '
            f'value="{esc(event.start)}" placeholder="YYYY-MM-DD"></label>'
            f'<label>end <input form="{fid}" type="text" name="end" '
            f'value="{esc(event.end or "")}" placeholder="YYYY-MM-DD, empty = one day"></label>'
            f'<textarea form="{fid}" name="note" rows="3" '
            f'placeholder="Note">{esc(event.note or "")}</textarea>'
            f'<input form="{fid}" type="text" name="reason" required '
            f'placeholder="Reason — recorded in the event history">'
            f'<button form="{fid}" class="small">Save</button>'
            f'<span class="hint">Moving the date of a confirmed/scheduled event without a new '
            f"verified source drops it to unverified — amend first, verify after.</span>"
            f'<input form="{fid}" type="hidden" name="uid" value="{esc(event.uid)}">'
            f'<input form="{fid}" type="hidden" name="back" value="{esc(back)}">'
            f"</div>")
    return (f'<details class="detail"><summary title="Details &amp; edit">'
            f'<span class="title">{esc(event.title())}</span></summary>'
            f'<div class="detailbox">{head}{note}{"".join(srcs)}{"".join(hist)}{form}</div>'
            f"</details>")


def event_row(event: Event, selectable: bool = False, checkbox_name: str = "uid",
              verify_back: str | None = None, verify_scope: str = "") -> str:
    source = safe_href(event.sources[0].url) if event.sources else None
    link = f' · <a href="{source}"{NEW_TAB}>source</a>' if source else ""
    verified = " · verified" if any(s.verified_at for s in event.sources) else ""
    reminder = " · reminder T-2" if wants_alarm(event) else ""
    # Everything syncs unless explicitly switched off (#70), so the label is
    # only worth showing for the exception — flagging every event as
    # "calendar on" would be noise on all 200+ rows.
    calendar = " · calendar off" if event.sync_authorized is False else ""
    removed_cls = " removed" if event.removed else ""
    removed_note = (f' <span class="flag">removed: {esc(event.removed_reason or "")}</span>'
                    if event.removed else "")
    checkbox = (f'<input type="checkbox" name="{checkbox_name}" value="{esc(event.uid)}" '
                f'aria-label="Select {esc(event.title())}">' if selectable else "")
    sel_cls = "" if selectable else " nosel"
    # the stamp doubles as the verify trigger (#42): clicking it pops the form
    stamp_cell = (verify_details(event, verify_back, verify_scope)
                  if verify_back and _verifiable(event)
                  else stamp(event.status.value))
    # and the title as the detail-tile trigger (#56)
    title_cell = (detail_tile(event, verify_back, verify_scope) if verify_back
                  else f'<span class="title">{esc(event.title())}</span>')
    return (
        f'<div class="row{sel_cls}{removed_cls}">{checkbox}'
        f'<span class="date">{esc(span_of(event))}</span>'
        f"{stamp_cell}"
        f"<div>{title_cell}"
        f'<span class="meta"> · {esc(event.uid)} · T{event.tier}{verified}{reminder}{calendar}{link}</span>'
        f"{removed_note}</div></div>"
    )


def page(active: str, body: str, message: str | None = None) -> str:
    pending_count = len(pending_triage(STORE))
    total = sum(1 for _ in STORE.iter_events())
    states = list(STORE.iter_source_states())
    sick = len(sick_sources(states))
    tabs = [("/", "uebersicht", "Overview"), ("/kalender", "kalender", "Calendar"),
            ("/events", "bestand", "Events"), ("/journal", "journal", "Journal"),
            ("/glossar", "glossar", "Glossary")]
    nav = "".join(
        f'<a href="{href}"{" class=\"active\"" if key == active else ""}>{label}</a>'
        for href, key, label in tabs
    )
    notice = f'<div class="notice">{esc(message)}</div>' if message else ""
    return (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="icon" href="{FAVICON}">'
        f"<title>China Calendar</title><style>{CSS}</style></head><body>"
        f"<header><h1>China Calendar</h1><div class=\"registry\">"
        f"<span>as of <strong>{date.today().isoformat()}</strong></span>"
        f"<span><strong>{total}</strong> events</span>"
        f"<span>triage <strong>{pending_count}</strong></span>"
        f"<span>sources <strong>{len(states) - sick} ok / {sick} failing</strong></span>"
        # the sweep's raw markdown report — reference material, not a view
        f'<span><a href="/digest">digest</a></span>'
        f"</div></header><nav>{nav}</nav>"
        f"{notice}{body}"
        f'<footer><span>Store: {esc(CONFIG.store_dir)} — all writes go through '
        f"the same core as CLI and MCP.</span></footer>"
        f"{SELECT_ALL_JS}</body></html>"
    )


# ---------------------------------------------------------------- Übersicht

def watch_lists(today: date) -> tuple[list[Event], list[Event]]:
    """The two halves of the watchlist: dates that have not stood up yet, and
    projections whose window closed without resolution (#27).

    Shared with /api/summary — the tile counts the same events the page
    shows, and a divergence between the two would be invisible.
    """
    watch, stale_projections = [], []
    for event in STORE.iter_events():
        if event.status.value in ("unverified", "rumored"):
            watch.append(event)
        elif event.status.value == "projected" and event.end_date() < today:
            stale_projections.append(event)
    return watch, stale_projections


def sick_sources(states: list) -> list:
    """Sources that are failing or have gone quiet — same rule the header uses."""
    return [s for s in states
            if s.last_error or s.consecutive_zero_runs >= ZERO_RUNS_FLAG]


def render_overview() -> str:
    today = date.today()
    pending = pending_triage(STORE)
    upcoming = STORE.search(from_=today, to=today + timedelta(days=90))
    watch, stale_projections = watch_lists(today)
    states = list(STORE.iter_source_states())

    parts = ['<section><div class="eyebrow"><span class="de">Triage</span>'
             f'<span class="en">items awaiting your decision</span><span class="count">{len(pending)}</span></div>']
    if not pending:
        parts.append('<p class="empty">Nothing waiting. The next sweep runs tomorrow morning.</p>')
    else:
        parts.append('<form method="post" action="/triage">')
        parts.append('<div class="bulkbar"><label><input type="checkbox" '
                     'onclick="toggleAll(this, \'id\')"> all</label>'
                     '<input type="text" name="reason" placeholder="Reason — goes in the ledger">'
                     '<button class="accept" name="bulk" value="accept">Accept selected</button>'
                     '<button class="reject" name="bulk" value="reject">Reject selected</button>'
                     '<button name="bulk" value="defer">Defer selected</button></div>')
        for item in pending:
            hint = ""
            if item.classifier:
                verdict = "relevant" if item.classifier.get("relevant") else "not relevant"
                hint = (f'<div class="hint">classifier: {verdict} '
                        f'({item.classifier.get("confidence", 0):.2f}) — '
                        f'{esc(item.classifier.get("reason", ""))}</div>')
            item_span = esc(fmt_date(item.start)) + (f" → {esc(fmt_date(item.end))}" if item.end else "")
            item_href = safe_href(item.url)
            link = f' · <a href="{item_href}"{NEW_TAB}>source</a>' if item_href else ""
            # Duplicate suggestion (#68) — a proposal, so it renders as its own
            # action rather than pre-selecting anything.
            dupe = ""
            if item.duplicate_of:
                d = item.duplicate_of
                dupe = (
                    f'<div class="hint dupe">Looks like we already hold this: '
                    f'<a href="/events?q={esc(d.get("uid", ""))}">{esc(d.get("title") or d.get("uid", ""))}</a> '
                    f'({esc(str(d.get("start", "")))}, {esc(str(d.get("status", "")))}) — '
                    f'{esc(str(d.get("why", "")))}. '
                    f'<button class="small" name="single" '
                    f'value="corroborate:{esc(item.content_hash)}">'
                    f'Attach as corroboration</button></div>'
                )
            parts.append(
                f'<div class="tri"><div class="head">'
                f'<input type="checkbox" name="id" value="{esc(item.content_hash)}" '
                f'aria-label="Select {esc(item.title)}">'
                f'<span class="date mono">{item_span}</span>'
                f'<span class="title">{esc(item.title)}</span>'
                f'<span class="meta">{esc(item.source_id)}{link}</span>'
                f'<button class="accept small" name="single" value="accept:{esc(item.content_hash)}">Accept</button>'
                f'<button class="reject small" name="single" value="reject:{esc(item.content_hash)}">Reject</button>'
                f"</div>"
                f'<div class="desc">{esc((item.description or "")[:280])}</div>{hint}{dupe}</div>'
            )
        parts.append("</form>")
    parts.append("</section>")

    parts.append('<section><div class="eyebrow"><span class="de">Preview</span>'
                 f'<span class="en">next 90 days</span><span class="count">{len(upcoming)}</span></div>')
    if not upcoming:
        parts.append('<p class="empty">No events in the window.</p>')
    grouped: dict[str, list[Event]] = {}
    for event in upcoming:
        grouped.setdefault(cluster_of(event), []).append(event)
    for key in CLUSTER_LABELS:
        if key in grouped:
            parts.append(f'<div class="cluster-h">{esc(CLUSTER_LABELS[key])}</div>')
            parts.extend(event_row(e, verify_back="/", verify_scope="p-")
                         for e in grouped[key])
    parts.append("</section>")

    parts.append('<section><div class="eyebrow"><span class="de">Watchlist</span>'
                 '<span class="en">unverified / rumored / unresolved projections</span>'
                 f'<span class="count">{len(watch) + len(stale_projections)}</span></div>')
    if not watch and not stale_projections:
        parts.append('<p class="empty">Nothing on watch — every stored date has held up.</p>')
    for event in watch:
        from .sweep import RECHECK_DEMOTE_AFTER

        rechecks = sum(1 for h in event.history if h.field == "recheck_failed")
        unreachable = sum(1 for h in event.history if h.field == "recheck_unreachable")
        row = event_row(event, verify_back="/", verify_scope="w-")
        if rechecks >= RECHECK_DEMOTE_AFTER:
            row = row.replace("</span></div>",
                              f'</span> <span class="flag">{rechecks} failed re-checks — '
                              f"possible fabrication</span></div>", 1)
        elif unreachable:
            # Nobody has contradicted this date; we just cannot reach the
            # page that carries it. Different problem, different remedy.
            row = row.replace("</span></div>",
                              f'</span> <span class="flag">source unreachable '
                              f"({unreachable}) — needs another source</span></div>", 1)
        parts.append(row)
    for event in stale_projections:
        row = event_row(event, verify_back="/", verify_scope="s-").replace(
            "</span></div>",
            '</span> <span class="flag">projection window closed — resolve: '
            "research &amp; amend, or remove</span></div>", 1)
        parts.append(row)
    parts.append("</section>")

    parts.append('<section><div class="eyebrow"><span class="de">Sources</span>'
                 '<span class="en">parser health</span></div>')
    if states:
        parts.append("<table><tr><th>Source</th><th>Last run</th><th>Items</th><th>Status</th></tr>")
        for s in states:
            if s.last_error:
                status = f'<span class="flag">{esc(s.last_error)}</span>'
            elif s.consecutive_zero_runs >= ZERO_RUNS_FLAG:
                status = f'<span class="flag">{s.consecutive_zero_runs} zero-item runs</span>'
            else:
                status = "ok"
            parts.append(f'<tr><td class="mono">{esc(s.source_id)}</td>'
                         f'<td class="mono">{esc((s.last_run or "—")[:16])}</td>'
                         f"<td>{s.last_item_count}</td><td>{status}</td></tr>")
        parts.append("</table>")
    else:
        parts.append('<p class="empty">No sweep has run yet.</p>')
    parts.append("</section>")
    parts.append(popup_forms(upcoming, "p-")
                 + popup_forms(watch, "w-")
                 + popup_forms(stale_projections, "s-"))
    return "".join(parts)


# --------------------------------------------------------------- pagination

PAGE_SIZE = 50


def _page_of(query: dict, key: str = "page") -> int:
    try:
        return max(1, int(query.get(key, 1)))
    except (TypeError, ValueError):
        return 1


def _paginate(items: list, page: int):
    total_pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(page, total_pages)
    return items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], page, total_pages


def _pager(path: str, query: dict, key: str, page: int, total_pages: int,
           count: int) -> str:
    if total_pages <= 1:
        return ""
    def link(p: int, label: str) -> str:
        params = {k: v for k, v in query.items() if v and k != "m"}
        params[key] = str(p)
        return f'<a href="{path}?{urlencode(params)}">{label}</a>'
    prev_a = link(page - 1, "&larr; back") if page > 1 else "<span></span>"
    next_a = link(page + 1, "next &rarr;") if page < total_pages else "<span></span>"
    return (f'<div class="pager">{prev_a}<span>page {page}/{total_pages} · '
            f"{count} entries</span>{next_a}</div>")


# ----------------------------------------------------------------- Kalender

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Events spanning this many days or more leave the grid and render as a
# "Zeiträume" list above it — a week-long chip repeated on every covered
# day floods the month view (#20, user feedback 2026-08-03).
LONG_SPAN_DAYS = 4


def render_calendar(query: dict) -> str:
    import calendar as calmod

    today = date.today()
    try:
        first = date.fromisoformat((query.get("month") or "")[:7] + "-01")
    except ValueError:
        first = today.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")

    events = STORE.search(from_=first, to=last)
    spans = [e for e in events
             if (e.end_date() - e.start_date()).days + 1 >= LONG_SPAN_DAYS]
    by_day: dict[date, list[tuple[Event, bool]]] = {}
    for event in events:
        if event in spans:
            continue
        start = event.start_date()
        day = max(start, first)
        stop = min(event.end_date(), last)
        while day <= stop:
            by_day.setdefault(day, []).append((event, day == start))
            day += timedelta(days=1)

    title = f"{MONTHS[first.month - 1]} {first.year}"
    parts = ['<section><div class="eyebrow"><span class="de">Calendar</span>'
             f'<span class="en">{len(events)} events</span>'
             f'<span class="count"><a href="/kalender?month={prev_m}">&larr; back</a> · '
             f"<strong>{title}</strong> · "
             f'<a href="/kalender?month={next_m}">next &rarr;</a> · '
             f'<a href="/kalender">today</a></span></div>']
    if spans:
        parts.append('<div class="cluster-h">Multi-day spans '
                     f'({LONG_SPAN_DAYS}+ days — listed, not gridded)</div>')
        parts.extend(event_row(e, verify_back="/kalender") for e in spans)
        parts.append('<div style="margin-bottom:1rem"></div>')
    parts.append('<div class="calwrap"><table class="cal"><tr>'
                 + "".join(f"<th>{d}</th>" for d in WEEKDAYS) + "</tr>")
    for week in calmod.Calendar().monthdatescalendar(first.year, first.month):
        parts.append("<tr>")
        for day in week:
            classes = (["out"] if day.month != first.month else []) \
                + (["today"] if day == today else [])
            cls = f' class="{" ".join(classes)}"' if classes else ""
            cell = [f'<span class="dnum">{day.day}</span>']
            for event, is_start in by_day.get(day, []):
                dot = f'<span class="dot k-{cluster_of(event)}"></span>' if is_start else ""
                cont = "" if is_start else " cont"
                cell.append(
                    f'<a class="chip {STAMP_CLASS.get(event.status.value, "")}{cont}" '
                    f'href="/events?q={quote(event.uid)}" '
                    f'title="{esc(span_of(event))} · {esc(event.status.value)} · {esc(event.title())}">'
                    f"{dot}{esc(event.title())}</a>")
            parts.append(f"<td{cls}>{''.join(cell)}</td>")
        parts.append("</tr>")
    parts.append("</table></div>")
    parts.append('<div class="legend">'
                 + "".join(f'<span><span class="dot k-{key}"></span>{esc(label)}</span>'
                           for key, label in CLUSTER_LABELS.items())
                 + "<span>Status = text colour; pale chips are continuation days; "
                   f"events of {LONG_SPAN_DAYS}+ days sit above the grid.</span></div>")
    parts.append("</section>")
    parts.append(popup_forms(spans))
    return "".join(parts)


# ------------------------------------------------------------------ Bestand

def _fold_cutoff(today: date | None = None) -> date:
    """First day of the current month — everything that ended before it is
    "last month or older" and folds away by default (#60)."""
    return (today or date.today()).replace(day=1)


def filter_events(query: dict) -> tuple[list, int]:
    """The Events view's filter, as one implementation (#73).

    Shared with the CSV export so the file cannot drift from the page — an
    export that quietly disagrees with what you were looking at is worse than
    no export. Returns (events, how many past events were folded away).
    """
    q = query.get("q", "")
    status = query.get("status", "")
    cluster = query.get("cluster", "")
    include_removed = query.get("removed", "") == "1"
    verified_only = query.get("verified", "") == "1"
    show_past = query.get("past", "") == "1"

    def parse_day(key: str) -> date | None:
        try:
            return date.fromisoformat(query.get(key, ""))
        except ValueError:
            return None

    von, bis = parse_day("von"), parse_day("bis")
    events = STORE.search(query=q or None, status=status or None,
                          from_=von, to=bis, verified=verified_only or None,
                          include_removed=include_removed)
    if cluster:
        events = [e for e in events if cluster_of(e) == cluster]

    # The store sorts ascending by start, so without this the view opens on
    # 2023 every time (#60). An explicit `von` is the user asking for a range —
    # it wins over the default fold, as does `past=1`.
    folded = 0
    if not show_past and von is None:
        cutoff = _fold_cutoff()
        kept = [e for e in events if e.end_date() >= cutoff]
        folded = len(events) - len(kept)
        events = kept
    return events, folded


def render_events(query: dict) -> str:
    q = query.get("q", "")
    status = query.get("status", "")
    cluster = query.get("cluster", "")
    include_removed = query.get("removed", "") == "1"
    verified_only = query.get("verified", "") == "1"
    show_past = query.get("past", "") == "1"
    events, folded = filter_events(query)

    def parse_day(key: str) -> date | None:
        try:
            return date.fromisoformat(query.get(key, ""))
        except ValueError:
            return None

    von, bis = parse_day("von"), parse_day("bis")  # for the form fields only

    status_opts = "".join(
        f'<option value="{s}"{" selected" if s == status else ""}>{s or "any status"}</option>'
        for s in ["", "confirmed", "scheduled", "rumored", "projected", "unverified"])
    cluster_opts = "".join(
        f'<option value="{c}"{" selected" if c == cluster else ""}>'
        f'{CLUSTER_LABELS.get(c, "any cluster")}</option>'
        for c in ["", *CLUSTER_LABELS])

    parts = ['<section><div class="eyebrow"><span class="de">Events</span>'
             f'<span class="en">the full store</span><span class="count">{len(events)}</span></div>',
             f'<form method="get" action="/events" class="filters">'
             f'<input type="text" name="q" value="{esc(q)}" '
             f'placeholder="Search title, actor, evidence, note, url">'
             f'<select name="status" aria-label="Status">{status_opts}</select>'
             f'<select name="cluster" aria-label="Cluster">{cluster_opts}</select>'
             f'<label>from <input type="date" name="von" value="{von.isoformat() if von else ""}"></label>'
             f'<label>to <input type="date" name="bis" value="{bis.isoformat() if bis else ""}"></label>'
             f'<label><input type="checkbox" name="verified" value="1"'
             f'{" checked" if verified_only else ""}> verified only</label>'
             f'<label><input type="checkbox" name="removed" value="1"'
             f'{" checked" if include_removed else ""}> include removed</label>'
             f'<label><input type="checkbox" name="past" value="1"'
             f'{" checked" if show_past else ""}> include past</label>'
             f"<button>Filter</button></form>"]

    # Export carries the live filter, so the file matches what is on screen
    # rather than whatever the export's own defaults would have been (#73).
    csv_params = {k: v for k, v in query.items() if v and k not in ("m", "page")}
    csv_href = "/events.csv" + (f"?{urlencode(csv_params)}" if csv_params else "")
    parts.append(
        f'<p class="folded">Exporting {len(events)} event(s) with this filter · '
        f'<a href="{csv_href}">download CSV</a></p>')

    if folded:
        params = {k: v for k, v in query.items() if v and k not in ("m", "page")}
        params["past"] = "1"
        parts.append(
            f'<p class="folded">{folded} event(s) from last month or older '
            f'are folded away · <a href="/events?{urlencode(params)}">show them</a></p>')

    if not events:
        parts.append('<p class="empty">No events match this filter.</p></section>')
        return "".join(parts)

    page_events, page, total_pages = _paginate(events, _page_of(query))
    pager = _pager("/events", query, "page", page, total_pages, len(events))
    parts.append(pager)
    parts.append('<form method="post" action="/events/action">')
    parts.append('<div class="bulkbar"><label><input type="checkbox" '
                 'onclick="toggleAll(this, \'uid\')"> all</label>'
                 '<input type="text" name="reason" placeholder="Reason — recorded in the event history">'
                 '<button class="danger" name="action" value="remove">Remove selected</button>'
                 '<button name="action" value="restore">Restore selected</button>'
                 '<button name="action" value="remind_on">Reminder on</button>'
                 '<button name="action" value="remind_off">Reminder off</button>'
                 '<button name="action" value="cal_on">Calendar on</button>'
                 '<button name="action" value="cal_off">Calendar off</button></div>')
    parts.extend(event_row(e, selectable=True,
                           verify_back=f"/events?q={quote(e.uid)}")
                 for e in page_events)
    parts.append("</form>")
    parts.append(popup_forms(page_events))
    parts.append(pager)
    parts.append("</section>")
    return "".join(parts)


# ------------------------------------------------------------------ Journal

def render_journal(query: dict) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    entries = []
    for event in STORE.iter_events(include_removed=True):
        for h in event.history:
            if h.ts >= cutoff:
                entries.append((h.ts, event, h))
    entries.sort(key=lambda x: x[0], reverse=True)
    page_entries, cp, cp_total = _paginate(entries, _page_of(query, "cp"))
    cpager = _pager("/journal", query, "cp", cp, cp_total, len(entries))

    parts = ['<section><div class="eyebrow"><span class="de">Journal</span>'
             '<span class="en">changes, last 30 days</span>'
             f'<span class="count">{len(entries)}</span></div>']
    if not entries:
        parts.append('<p class="empty">No changes recorded yet.</p>')
    else:
        parts.append(cpager)
        parts.append("<table><tr><th>When</th><th>Event</th><th>Change</th><th>Actor</th></tr>")
        for ts, event, h in page_entries:
            if h.field == "__created__":
                change = f"created as {stamp(str(h.to))}"
            elif h.field == "removed":
                change = "removed" if h.to else "restored"
            elif h.field == "status":
                change = f"status {esc(h.from_)} → {stamp(str(h.to))}"
            elif h.field == "recheck_failed":
                change = '<span class="flag">re-check failed</span>'
            else:
                change = f"{esc(h.field)} {esc(h.from_)} → {esc(h.to)}"
            reason = f' <span class="journal-reason">— {esc(h.reason)}</span>' if h.reason else ""
            parts.append(f'<tr><td class="mono">{esc(ts[:16])}</td>'
                         f"<td>{esc(event.title())}</td><td>{change}{reason}</td>"
                         f'<td class="mono">{esc(h.actor)}</td></tr>')
        parts.append("</table>")
        parts.append(cpager)
    parts.append("</section>")

    # Changes to the machinery itself — a rewritten topic profile has no event
    # to hang history on, so without this it is visible only as a stray .bak
    # file in the store directory.
    system = [e for e in STORE.iter_system_events() if e.get("ts", "") >= cutoff]
    if system:
        parts.append('<section><div class="eyebrow"><span class="de">System</span>'
                     '<span class="en">configuration changes, last 30 days</span>'
                     f'<span class="count">{len(system)}</span></div>')
        parts.append("<table><tr><th>When</th><th>What</th><th>Actor</th></tr>")
        for entry in sorted(system, key=lambda e: e.get("ts", ""), reverse=True):
            detail = (f' <span class="journal-reason">— {esc(entry.get("detail"))}</span>'
                      if entry.get("detail") else "")
            parts.append(f'<tr><td class="mono">{esc(entry.get("ts", "")[:16])}</td>'
                         f'<td><span class="flag">{esc(entry.get("kind"))}</span> '
                         f'{esc(entry.get("summary"))}{detail}</td>'
                         f'<td class="mono">{esc(entry.get("actor"))}</td></tr>')
        parts.append("</table></section>")

    # Rejected items with human override. Overrides are recorded as human
    # ledger decisions, which is exactly what the classifier's few-shot
    # examples are sampled from — overriding IS teaching the gate.
    rejects = []
    for decision in STORE.iter_decisions():
        if decision.decision != "reject":
            continue
        try:
            item = STORE.get_raw(decision.content_hash)
        except StoreError:
            continue
        rejects.append((decision, item))
    rejects.sort(key=lambda x: x[0].ts, reverse=True)
    page_rejects, rp, rp_total = _paginate(rejects, _page_of(query, "rp"))
    rpager = _pager("/journal", query, "rp", rp, rp_total, len(rejects))

    parts.append('<section><div class="eyebrow"><span class="de">Rejected</span>'
                 '<span class="en">rejected items — select to accept anyway</span>'
                 f'<span class="count">{len(rejects)}</span></div>')
    if not rejects:
        parts.append('<p class="empty">Nothing has been rejected yet.</p>')
    else:
        parts.append('<form method="post" action="/override">')
        parts.append('<div class="bulkbar"><label><input type="checkbox" '
                     'onclick="toggleAll(this, \'id\')"> all</label>'
                     '<input type="text" name="reason" placeholder="Why this belongs in the calendar after all">'
                     '<button class="accept" name="go" value="1">Accept selected anyway</button></div>')
        parts.append(rpager)
        parts.append("<table><tr><th></th><th>Date</th><th>Item</th><th>Rejected by</th><th>Reason</th></tr>")
        for decision, item in page_rejects:
            reject_href = safe_href(item.url)
            title_cell = (f'<a href="{reject_href}"{NEW_TAB}>{esc(item.title)}</a>'
                          if reject_href else esc(item.title))
            parts.append(
                f'<tr><td><input type="checkbox" name="id" value="{esc(item.content_hash)}" '
                f'aria-label="Select {esc(item.title)}"></td>'
                f'<td class="mono">{esc(fmt_date(item.start))}</td>'
                f"<td>{title_cell}</td>"
                f'<td class="mono">{esc(decision.actor)}</td>'
                f'<td class="journal-reason">{esc(decision.reason or "")}</td></tr>')
        parts.append("</table></form>")
    parts.append("</section>")

    # AI token usage — the subscription is shared with other tooling, so the
    # dashboard shows what this tool actually consumes (issue #16).
    from .usage import usage_by_day

    days = usage_by_day(CONFIG, days=14)
    peak = max((d["total"] for d in days), default=0) or 1
    total_calls = sum(d["calls"] for d in days)
    total_tokens = sum(d["total"] for d in days)
    parts.append('<section><div class="eyebrow"><span class="de">AI usage</span>'
                 '<span class="en">LLM token usage, last 14 days</span>'
                 f'<span class="count">{total_calls} calls · {total_tokens:,} tokens</span></div>')
    if not total_calls:
        parts.append('<p class="empty">No model calls recorded yet (tracking starts today; '
                     'the classifier only fires on never-seen items).</p>')
    else:
        parts.append("<table><tr><th>Day</th><th>Calls</th><th>Prompt</th>"
                     "<th>Completion</th><th></th></tr>")
        for d in days:
            width = round(d["total"] / peak * 100)
            bar = (f'<div style="height:.55rem;width:{width}%;background:var(--ink);'
                   f'min-width:{2 if d["total"] else 0}px"></div>')
            parts.append(f'<tr><td class="mono">{esc(fmt_compact(d["day"]))}</td>'
                         f'<td>{d["calls"] or ""}</td>'
                         f'<td class="mono">{d["prompt_tokens"]:,}</td>'
                         f'<td class="mono">{d["completion_tokens"]:,}</td>'
                         f'<td style="width:35%">{bar}</td></tr>')
        parts.append("</table>")
    parts.append("</section>")
    return "".join(parts)


# ------------------------------------------------------------------ routes

async def overview_view(request: Request) -> HTMLResponse:
    return HTMLResponse(page("uebersicht", render_overview(),
                             message=request.query_params.get("m")))


async def calendar_view(request: Request) -> HTMLResponse:
    return HTMLResponse(page("kalender", render_calendar(dict(request.query_params)),
                             message=request.query_params.get("m")))


async def events_view(request: Request) -> HTMLResponse:
    query = dict(request.query_params)
    return HTMLResponse(page("bestand", render_events(query),
                             message=query.get("m")))


# Excel executes a cell beginning with any of these as a formula. Titles here
# come from scraped sources, so this is a real path, not a theoretical one.
_FORMULA_LEAD = ("=", "+", "-", "@")


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    # lstrip first: Excel trims leading whitespace before deciding whether a
    # cell is a formula, so "\t=HYPERLINK(...)" evaluates while passing a naive
    # startswith check. Titles here are scraped, i.e. untrusted.
    if text.lstrip(" \t\r\n").startswith(_FORMULA_LEAD):
        return "'" + text
    return text


def events_csv(query: dict) -> str:
    """The Events view as CSV, honouring the same filter the page shows."""
    import csv
    import io

    events, _ = filter_events(query)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["start", "end", "status", "tier", "title", "location",
                     "note", "source_url", "last_verified", "uid"])
    for event in events:
        primary = event.sources[0] if event.sources else None
        verified = max((s.verified_at for s in event.sources if s.verified_at),
                       default=None)
        writer.writerow([_csv_cell(v) for v in (
            event.start, event.end or "", event.status.value, event.tier,
            event.title(), event.location or "", event.note or "",
            (primary.url if primary else "") or "", verified or "", event.uid,
        )])
    return buf.getvalue()


async def events_csv_view(request: Request) -> Response:
    body = events_csv(dict(request.query_params))
    return Response(
        # BOM: without it Excel reads the file as latin-1 and mangles every
        # umlaut and Chinese title — and this store is full of both.
        content="﻿" + body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="china-calendar-{date.today().isoformat()}.csv"'},
    )


async def journal_view(request: Request) -> HTMLResponse:
    return HTMLResponse(page("journal", render_journal(dict(request.query_params)),
                             message=request.query_params.get("m")))


async def _form(request: Request) -> dict[str, list[str]]:
    return parse_qs((await request.body()).decode())


def _redirect(path: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{sep}m={quote(message)}", status_code=303)


def _safe_back(back: str, uid: str) -> str:
    # Backslashes too: browsers normalise "\" to "/" against a special-scheme
    # base, so "/\evil.example" would resolve protocol-relative.
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        return f"/events?q={quote(uid)}"
    return back


def _push_now(uids: list[str]) -> str:
    """Immediate calendar push after an interactive write (issue #15)."""
    from .calsync import push_event

    outcomes = []
    for uid in uids:
        outcomes.append(push_event(STORE, CONFIG, STORE.get(uid)))
    if not outcomes or outcomes[0] == "sync-not-configured":
        return ""
    pushed = sum(1 for o in outcomes if o in ("pushed", "removed-from-calendar"))
    failed = len(outcomes) - pushed
    note = f" · calendar updated ({pushed})"
    if failed:
        note += f", {failed} push(es) failed — nightly sync will retry"
    return note


async def triage_post(request: Request) -> RedirectResponse:
    form = await _form(request)
    reason = (form.get("reason") or [None])[0] or None
    single = (form.get("single") or [None])[0]
    checked = form.get("id") or []
    if single:
        # A per-item button applies to its item PLUS anything checked — that
        # matches what users expect when they tick boxes and then hit a button.
        decision, _, item_id = single.partition(":")
        # Corroborate is item-specific: each item has its own suggested match,
        # so sweeping in checked boxes would attach sources to the wrong events.
        ids = [item_id] if decision == "corroborate" else list(dict.fromkeys([item_id, *checked]))
    else:
        decision = (form.get("bulk") or [""])[0]
        ids = checked
    if not decision or not ids:
        return _redirect("/", "Nothing selected.")
    accepted, failed = [], 0
    for item_id in ids:
        try:
            event = triage_decide(STORE, CONFIG, item_id, decision, reason, actor="human:webui")
        except (StoreError, ValueError):
            failed += 1  # one bad item must not discard the decisions either side
            continue
        if event:
            accepted.append(event.uid)
    message = f"{decision}: {len(ids) - failed} item(s)"
    if failed:
        message += f", {failed} failed"
    if accepted:
        message += " → " + ", ".join(accepted)
        message += _push_now(accepted)
    return _redirect("/", message)


async def override_post(request: Request) -> RedirectResponse:
    form = await _form(request)
    ids = form.get("id") or []
    reason = (form.get("reason") or [None])[0] or "human override of rejection"
    if not ids:
        return _redirect("/journal", "Nothing selected.")
    accepted = []
    for item_id in ids:
        event = triage_decide(STORE, CONFIG, item_id, "accept", reason, actor="human:webui")
        if event:
            accepted.append(event.uid)
    message = f"accepted anyway: {', '.join(accepted) or 'none'}"
    if accepted:
        message += _push_now(accepted)
    return _redirect("/journal", message)


async def events_action_post(request: Request) -> RedirectResponse:
    form = await _form(request)
    action = (form.get("action") or [""])[0]
    uids = form.get("uid") or []
    reason = (form.get("reason") or [None])[0]
    if action not in ("remove", "restore", "remind_on", "remind_off",
                      "cal_on", "cal_off") or not uids:
        return _redirect("/events", "Nothing selected.")
    if action == "remove" and not reason:
        return _redirect("/events", "Removal needs a reason — it goes in the event history.")
    done = 0
    for uid in uids:
        if action == "remove":
            STORE.remove(uid, actor="human:webui", reason=reason)
        elif action == "restore":
            STORE.restore(uid, actor="human:webui", reason=reason or "restored via dashboard")
        elif action.startswith("remind"):
            STORE.amend(uid, {"remind": action == "remind_on"}, actor="human:webui",
                        reason=reason or "reminder toggled via dashboard")
        else:
            # the human authorization the sync rules require for
            # rumored/projected; harmless on other statuses (they auto-sync)
            STORE.amend(uid, {"sync_authorized": action == "cal_on"}, actor="human:webui",
                        reason=reason or "calendar sync toggled via dashboard")
        done += 1
    verb = {"remove": "removed", "restore": "restored",
            "remind_on": "reminder on for", "remind_off": "reminder off for",
            "cal_on": "calendar on for", "cal_off": "calendar off for"}[action]
    return _redirect("/events", f"{verb} {done} event(s)" + _push_now(uids))


async def events_amend_post(request: Request) -> RedirectResponse:
    """Inline amend from the detail tile (#56). Only start/end/note are
    accepted — status, tier and provenance stay server-assigned (I1), and
    the date-move rule lives in the core (store.amend_and_requeue, I2)."""
    form = await _form(request)
    uid = (form.get("uid") or [""])[0]
    back = _safe_back((form.get("back") or [""])[0], uid)
    reason = (form.get("reason") or [""])[0].strip()
    if not uid:
        return _redirect("/events", "Nothing to amend.")
    if not reason:
        return _redirect(back, "Amending needs a reason — it goes in the event history.")
    try:
        event = STORE.get(uid)
    except StoreError as exc:
        return _redirect("/events", f"Amend failed: {exc}")
    start = (form.get("start") or [""])[0].strip()
    end = (form.get("end") or [""])[0].strip() or None
    note = (form.get("note") or [""])[0].strip() or None
    patch = {}
    if start and start != event.start:
        patch["start"] = start
    if end != event.end:
        patch["end"] = end
    if note != (event.note or None):
        patch["note"] = note
    if not patch:
        return _redirect(back, f"{uid}: nothing changed.")
    try:
        event, requeued = STORE.amend_and_requeue(uid, patch, actor="human:webui",
                                                  reason=reason)
    except (StoreError, ValueError) as exc:
        return _redirect(back, f"Amend failed: {exc}")
    message = f"{uid}: amended {', '.join(sorted(patch))} → {event.status.value}"
    if requeued:
        message += (" — date moved without a verified source, so it dropped to "
                    "unverified; verify to promote")
    return _redirect(back, message + _push_now([uid]))


async def events_verify_post(request: Request) -> RedirectResponse:
    """Human verification from Bestand (issue #33) — same core as the MCP
    event_verify tool (I2): verify.source_verify / verify.human_verify assign
    status from provenance; this handler only routes the form."""
    from .fetch import Fetcher
    from .verify import human_verify, source_verify

    form = await _form(request)
    uid = (form.get("uid") or [""])[0]
    mode = (form.get("mode") or [""])[0]
    url = (form.get("url") or [""])[0].strip() or None
    evidence = (form.get("evidence") or [""])[0].strip() or None
    official = (form.get("official") or [""])[0] == "1"
    back = _safe_back((form.get("back") or [""])[0], uid)
    if not uid or mode not in ("fetch", "human"):
        return _redirect("/events", "Nothing to verify.")
    if not evidence:
        return _redirect(back, "Verification needs evidence — the sentence the date comes from.")
    try:
        if mode == "fetch":
            if not url:
                return _redirect(back, "Fetch & match needs a source URL.")
            fetcher = Fetcher(CONFIG, STORE)
            try:
                event, matched, note = source_verify(
                    STORE, fetcher, uid, url, evidence,
                    actor="human:webui", official=official)
            finally:
                fetcher.close()
            message = (f"{uid}: evidence matched → {event.status.value}"
                       if matched else f"{uid}: {note}")
        else:
            event, note = human_verify(STORE, uid, evidence, actor="human:webui",
                                       official=official, url=url)
            message = f"{uid}: verified by your statement → {event.status.value}"
            if note:
                message = f"{message} ({note})"
    except (StoreError, ValueError) as exc:
        return _redirect(back, f"Verification failed: {exc}")
    return _redirect(back, message + _push_now([uid]))


# ------------------------------------------------------------------ Glossar

def render_glossary() -> str:
    import yaml
    from pathlib import Path

    path = Path(__file__).parent / "glossary.yaml"
    groups = yaml.safe_load(path.read_text(encoding="utf-8"))
    parts = ['<div class="notice">What we watch: the recurring formats behind '
             "the dates — deliberately detached from the event store. "
             'Maintained in <span class="mono">glossary.yaml</span>.</div>']
    for group in groups:
        items = group.get("items", [])
        parts.append(f'<section><div class="eyebrow"><span class="de">{esc(group["cluster"])}</span>'
                     f'<span class="en">{esc(group.get("cluster_en", ""))}</span>'
                     f'<span class="count">{len(items)}</span></div>')
        for item in items:
            meta = " · ".join(
                f"{label} {esc(item[key])}"
                for key, label in (("cadence", "cadence:"), ("who", "participants:"),
                                   ("covers", "covers:"), ("blocked", "blocked by:"))
                if item.get(key))
            parts.append(f'<div class="gloss"><div class="gname">{esc(item["name"])}</div>'
                         f'<div class="gmeta">{meta}</div>'
                         f'<p class="gblurb">{esc(item.get("blurb", ""))}</p></div>')
        parts.append("</section>")
    return "".join(parts)


async def glossary_view(request: Request) -> HTMLResponse:
    return HTMLResponse(page("glossar", render_glossary(),
                             message=request.query_params.get("m")))


async def summary_view(request: Request) -> JSONResponse:
    """Counts for the home dashboard tile (custom-dashboard#32).

    Exactly what the page header and Übersicht already show, as JSON —
    everything else here is server-rendered HTML, and a dashboard scraping
    that would break on the next markup change. Counts only, no titles: this
    is the one route not behind the LAN bind's implicit access control in
    spirit, so it says how much is waiting, never what.
    """
    today = date.today()
    states = list(STORE.iter_source_states())
    sick = sick_sources(states)
    watch, stale_projections = watch_lists(today)
    runs = [s.last_run for s in states if s.last_run]
    return JSONResponse({
        "total_events": sum(1 for _ in STORE.iter_events()),
        "pending": len(pending_triage(STORE)),
        "upcoming_90d": len(STORE.search(from_=today, to=today + timedelta(days=90))),
        "watch": len(watch) + len(stale_projections),
        "sources": {
            "total": len(states),
            "ok": len(states) - len(sick),
            "sick": len(sick),
            "sick_ids": [s.source_id for s in sick],
        },
        # ISO 8601, or null before the first sweep has ever run
        "last_sweep": max(runs) if runs else None,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _clamped(query, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(query.get(key, default))))
    except (TypeError, ValueError):
        return default


def _item_json(item) -> dict:
    return {
        "id": item.content_hash,
        "title": item.title,
        "start": item.start,
        "end": item.end,
        "date_text": item.date_text,
        "source_id": item.source_id,
        "url": item.url,
        "description": (item.description or "")[:280],
        "classifier": item.classifier,
    }


def _event_json(event: Event) -> dict:
    source = next((s.url for s in event.sources if s.url), None)
    return {
        "uid": event.uid,
        "title": event.title(),
        "start": event.start,
        "end": event.end,
        "span": span_of(event),
        "status": event.status.value,
        "tier": event.tier,
        "cluster": cluster_of(event),
        "verified": any(s.verified_at for s in event.sources),
        "url": source,
    }


async def queue_view(request: Request) -> JSONResponse:
    """The triage queue as data, for a surface that can act on it (#32)."""
    limit = _clamped(request.query_params, "limit", 20, 1, 100)
    pending = pending_triage(STORE)
    return JSONResponse({
        "total": len(pending),
        "items": [_item_json(i) for i in pending[:limit]],
    })


async def upcoming_view(request: Request) -> JSONResponse:
    """Events in the window, soonest first."""
    days = _clamped(request.query_params, "days", 90, 1, 730)
    limit = _clamped(request.query_params, "limit", 20, 1, 100)
    today = date.today()
    events = STORE.search(from_=today, to=today + timedelta(days=days))
    return JSONResponse({
        "total": len(events),
        "days": days,
        "events": [_event_json(e) for e in events[:limit]],
    })


async def triage_api(request: Request) -> JSONResponse:
    """Apply triage decisions from another surface — the dashboard tile (#32).

    Same core as the buttons on the Übersicht page (I2); the actor says
    where the decision came from, because the ledger is the record of who
    decided what and "human:webui" would be a lie.

    JSON body required, and not merely as a convention: a plain form POST
    from whatever page the browser happens to be on can already reach
    /triage. A route that accepts nothing but application/json cannot be
    hit that way without a CORS preflight the browser will refuse.
    """
    if "application/json" not in (request.headers.get("content-type") or ""):
        return JSONResponse({"error": "content-type must be application/json"}, status_code=415)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)

    decision = str(body.get("decision") or "")
    if decision not in ("accept", "reject", "defer"):
        return JSONResponse({"error": "decision must be accept, reject or defer"},
                            status_code=400)
    ids = [str(i) for i in (body.get("ids") or []) if i]
    if not ids:
        return JSONResponse({"error": "no ids given"}, status_code=400)
    reason = (body.get("reason") or "").strip() or None

    accepted: list[str] = []
    failed: list[dict] = []
    for item_id in ids:
        try:
            event = triage_decide(STORE, CONFIG, item_id, decision, reason,
                                  actor="human:dashboard")
        except (StoreError, ValueError) as e:
            # One bad id must not discard the decisions either side of it.
            failed.append({"id": item_id, "error": str(e)})
            continue
        if event:
            accepted.append(event.uid)
    # An accept pushes to the calendar immediately, exactly as the page's own
    # buttons do (#15) — a decision made here must not wait for the nightly sync.
    calendar = _push_now(accepted).strip(" ·") if accepted else ""
    return JSONResponse({
        "decision": decision,
        "decided": len(ids) - len(failed),
        "accepted": accepted,
        "failed": failed,
        "calendar": calendar or None,
    })


async def digest_view(request: Request) -> PlainTextResponse:
    files = sorted(CONFIG.digest_dir.glob("*.md")) if CONFIG.digest_dir.exists() else []
    if not files:
        return PlainTextResponse("No digest yet — it is written by the daily sweep.")
    return PlainTextResponse(files[-1].read_text(encoding="utf-8"))


class SameOriginFormPost(BaseHTTPMiddleware):
    """CSRF defence for the form routes: no sessions here, so there is no
    cookie to bind a token to and the check is the Origin instead. Missing
    Origin and Referer means a non-browser client, which passes. /api/* is
    exempt — the dashboard tile calls it cross-origin and its JSON-only
    requirement is its own defence.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and not request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer
            if source:
                parts = urlsplit(source)
                if parts.netloc != request.headers.get("host"):
                    return PlainTextResponse(
                        "Cross-origin POST refused.", status_code=403)
        return await call_next(request)


app = Starlette(middleware=[Middleware(SameOriginFormPost)], routes=[
    Route("/", overview_view),
    Route("/kalender", calendar_view),
    Route("/triage", triage_post, methods=["POST"]),
    Route("/events", events_view),
    Route("/events.csv", events_csv_view),
    Route("/events/action", events_action_post, methods=["POST"]),
    Route("/events/verify", events_verify_post, methods=["POST"]),
    Route("/events/amend", events_amend_post, methods=["POST"]),
    Route("/glossar", glossary_view),
    Route("/journal", journal_view),
    Route("/override", override_post, methods=["POST"]),
    Route("/digest", digest_view),
    Route("/api/summary", summary_view),
    Route("/api/queue", queue_view),
    Route("/api/upcoming", upcoming_view),
    Route("/api/triage", triage_api, methods=["POST"]),
])


def main() -> None:
    uvicorn.run(app,
                host=os.environ.get("PC_WEB_HOST", "127.0.0.1"),
                port=int(os.environ.get("PC_WEB_PORT", "8810")),
                log_level="warning")


if __name__ == "__main__":
    main()
