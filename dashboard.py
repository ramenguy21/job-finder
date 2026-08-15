"""Render the stored corpus as a single self-contained HTML dashboard.

    uv run python dashboard.py            # writes dashboard.html
    uv run python dashboard.py --open     # ...and opens it
    uv run python dashboard.py --out /tmp/d.html --db /data/seen.db
    uv run python dashboard.py --serve    # http://localhost:8080 instead

On Fly the watcher serves this itself - see serve_in_background(), which
main.py starts on a daemon thread when DASHBOARD_PORT is set. There is no auth:
anyone with the hostname can read the corpus. That is a deliberate call for a
personal watcher over public job feeds.

This is `backtest.py` with a browser instead of a terminal. Same idea, same
source of truth: every verdict shown is recomputed live from `config.py` via
`main.match_reason()` and `main.classify_geo()`, never read from the stored
`geo_tier` / `notify_state` columns. Those columns record what the filter
thought *at ingest time*; the point of the dashboard is to see what the filter
thinks *now*, so that editing a vocabulary and re-rendering shows the effect
immediately. The one exception is the delivery column, which is genuinely
historical and is read from the DB.

The database is opened read-only (`mode=ro`). This is a viewer. It must not be
able to migrate, reclassify or prune the corpus by accident - `backtest.py
--reclassify` is the tool that writes, and it should stay the only one.

No new dependencies: stdlib in, one HTML file out. The charts are CSS boxes,
not a charting library, which is also why the output opens over file:// with
no network access at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import main as gb

# ---------------------------------------------------------------- vocabularies

# (label, compiled pattern, term list, scope) where scope is which text the
# filter actually matches this vocabulary against. Getting the scope wrong here
# would quietly overstate hits: EXCLUDE_TITLE fires on 4 corpus rows against the
# title and on 40-odd against title+body, and the second number is the one that
# was killing real leads before the split existed.
VOCABS = [
    ("TITLE_BLOCK", gb.TITLE_BLOCK_RE, config.TITLE_BLOCK, "title"),
    ("TITLE_ROLE", gb.TITLE_ROLE_RE, config.TITLE_ROLE, "title"),
    ("EXCLUDE_TITLE", gb.EXCLUDE_TITLE_RE, config.EXCLUDE_TITLE, "title"),
    ("EXCLUDE", gb.EXCLUDE_RE, config.EXCLUDE, "both"),
    ("STACK", gb.STACK_RE, config.STACK, "both"),
    ("PK_LOCAL", gb.PK_LOCAL_RE, config.PK_LOCAL, "both"),
    ("PK_ELIGIBLE", gb.PK_ELIGIBLE_RE, config.PK_ELIGIBLE, "both"),
    ("GEO_BLOCKED", gb.GEO_BLOCKED_RE, config.GEO_BLOCKED, "both"),
]

# _build() turns a space in a term into [\s\-_]+, so "full stack" comes back
# out of the text as "full-stack" or "full_stack" or "full  stack". Fold all of
# those back to the spelling in config.py, otherwise the term-frequency table
# lists the same vocabulary entry three times.
SEP_RE = re.compile(r"[\s\-_]+")


def canon(surface: str) -> str:
    return SEP_RE.sub(" ", surface.lower()).strip()


def term_hits(pattern: re.Pattern[str], lookup: dict[str, str], text: str) -> set[str]:
    """Which vocabulary terms fire in this text, as config spells them.

    A set, not a count: "how many postings mention Docker" is the useful
    number. Counting every occurrence would just rank whichever board repeats
    its own tech stack most in the boilerplate.
    """
    found = set()
    for surface in pattern.findall(text):
        key = canon(surface)
        found.add(lookup.get(key, key))
    return found


# ---------------------------------------------------------------------- loading


def load_rows(db_path: str) -> list[sqlite3.Row]:
    if not os.path.exists(db_path):
        raise SystemExit(
            f"no database at {os.path.abspath(db_path)} - wrong directory, or "
            "the watcher has never run. dashboard.py will not create one."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # notify_state only exists on databases that have been through the
    # migration in db_connect(). Opening read-only means we cannot add it, and
    # should not: an older corpus is still perfectly readable without it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen)")}
    state = "notify_state" if "notify_state" in cols else "NULL AS notify_state"
    rows = conn.execute(
        f"SELECT entry_id, source, title, body, link, published, ingested_at, "
        f"       notified, {state} FROM seen ORDER BY published DESC"
    ).fetchall()
    conn.close()
    return rows


def analyze(rows: list[sqlite3.Row]) -> tuple[list[dict], dict]:
    """Recompute every verdict against the live config, and audit the vocabularies.

    Returns (per-row records, vocabulary audit). The audit is deliberately
    computed over the whole corpus rather than the filtered slice: the question
    it answers - "is this term dead weight?" - is about the vocabulary, not
    about a source. A term that fires only on RemoteOK still fires.
    """
    lookups = {
        label: {canon(t): t for t in terms} for label, _p, terms, _s in VOCABS
    }
    audit: dict[str, Counter] = {label: Counter() for label, *_ in VOCABS}
    # Co-occurrence with a *match*, not just presence. "aws appears in 300
    # postings" is much less interesting than "aws appears in 40 of the 66 that
    # survived the filter", which is the beginning of the skill-frequency
    # question HANDOFF.md leaves open.
    matched_hits: dict[str, Counter] = {label: Counter() for label, *_ in VOCABS}

    records = []
    for r in rows:
        title = r["title"] or ""
        body = r["body"] or ""
        keep, why = gb.match_reason(title, body)
        tier = gb.classify_geo(f"{title} {body}")
        stage = "MATCH" if keep else why.split(":")[0]

        t_norm = gb._norm(title)
        both_norm = f"{t_norm} {gb._norm(body)}"
        row_stack: list[str] = []
        for label, pattern, _terms, scope in VOCABS:
            text = t_norm if scope == "title" else both_norm
            hits = term_hits(pattern, lookups[label], text)
            audit[label].update(hits)
            if keep:
                matched_hits[label].update(hits)
            if label == "STACK":
                row_stack = sorted(hits)

        records.append(
            {
                "s": r["source"],
                "t": title[:180],
                "l": r["link"] or "",
                "p": r["published"] or 0,
                "i": r["ingested_at"] or 0,
                "g": tier,
                "m": 1 if keep else 0,
                "st": stage,
                "w": why,
                "k": row_stack,
                # Historical, not recomputed: whether this actually went out.
                "d": r["notify_state"] or ("sent" if r["notified"] else None),
            }
        )

    vocab = {}
    for label, _p, terms, scope in VOCABS:
        counts = audit[label]
        vocab[label] = {
            "scope": scope,
            "size": len(set(terms)),
            "dead": sorted(t for t in set(terms) if not counts.get(t)),
            "top": [
                [t, n, matched_hits[label].get(t, 0)]
                for t, n in counts.most_common(24)
            ],
        }
    return records, vocab


# ----------------------------------------------------------------------- render

# Palette: the reference data-viz instance, blue ramp only.
#
# There is exactly one hue in this dashboard plus gray, on purpose. Every
# quantity here is either a magnitude (counts, rates) or an *ordered* scale
# (the four filter stages, the four geo tiers in TIER_ORDER) - neither is an
# identity/categorical job, and a categorical palette would have double-encoded
# bar length as hue. The ordinal ramp below passes the validator in both modes
# (monotone lightness, >=0.06 step gaps, light end clears the surface).
#
# The one two-series chart is emphasis, not categorical: matched in the accent,
# filtered-out in the de-emphasis gray. That pair fails the validator's chroma
# floor by design - gray is meant to read as gray - and passes the checks that
# matter for it: CVD separation 15.9 and >=3:1 against both surfaces.

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gigbot corpus</title>
<style>
:root {
  color-scheme: light dark;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --accent:#2a78d6; --track:rgba(42,120,214,.16); --dim:#898781;
  --r1:#86b6ef; --r2:#5598e7; --r3:#2a78d6; --r4:#1c5cab; --r5:#104281;
  --good:#0ca30c; --crit:#d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --accent:#3987e5; --track:rgba(57,135,229,.22); --dim:#898781;
    --r1:#184f95; --r2:#256abf; --r3:#3987e5; --r4:#6da7ec; --r5:#9ec5f4;
  }
}
:root[data-theme="dark"] {
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --accent:#3987e5; --track:rgba(57,135,229,.22); --dim:#898781;
  --r1:#184f95; --r2:#256abf; --r3:#3987e5; --r4:#6da7ec; --r5:#9ec5f4;
}

* { box-sizing:border-box; }
body {
  margin:0; padding:28px 22px 64px;
  background:var(--plane); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
}
.wrap { max-width:1180px; margin:0 auto; }
h1 { font-size:19px; font-weight:600; margin:0 0 3px; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:12.5px; margin:0 0 22px; }
.sub code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }

/* ---- filter row: one row above everything it scopes ---- */
.filters {
  display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  padding:11px 13px; margin-bottom:20px;
  background:var(--surface); border:1px solid var(--border); border-radius:10px;
}
.filters select, .filters input {
  font:13px system-ui,-apple-system,"Segoe UI",sans-serif;
  color:var(--ink); background:var(--surface);
  border:1px solid var(--axis); border-radius:7px; padding:5px 8px;
}
.filters input { min-width:190px; }
.filters .spacer { flex:1; }
.filters .count { color:var(--muted); font-size:12.5px; white-space:nowrap; }
button.ghost {
  font:13px system-ui,sans-serif; color:var(--ink2); cursor:pointer;
  background:transparent; border:1px solid var(--axis);
  border-radius:7px; padding:5px 10px;
}
button.ghost:hover { background:var(--track); }

/* ---- cards ---- */
.grid { display:grid; gap:14px; grid-template-columns:repeat(12,1fr); }
.card {
  grid-column:span 12; background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:16px 18px 18px; min-width:0;
}
.card.half { grid-column:span 6; }
.card.third { grid-column:span 4; }
@media (max-width:880px) { .card.half, .card.third { grid-column:span 12; } }
.card h2 {
  font-size:13px; font-weight:600; margin:0 0 2px; letter-spacing:.01em;
}
.card .note { color:var(--muted); font-size:12px; margin:0 0 14px; }
.card .note.silent { margin:14px 0 0; padding-top:12px;
                     border-top:1px solid var(--grid); }
.note code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11.5px; }

/* ---- hero + stat tiles ---- */
.hero-row { display:flex; flex-wrap:wrap; gap:26px; align-items:flex-end; }
.hero .val { font-size:52px; font-weight:600; line-height:1; letter-spacing:-.02em; }
.hero .lab { color:var(--muted); font-size:12.5px; margin-top:6px; }
.tiles { display:flex; flex-wrap:wrap; gap:26px; }
.tile .val { font-size:24px; font-weight:600; line-height:1.1; }
.tile .lab { color:var(--muted); font-size:12px; margin-top:3px; }
.tile .val small { font-size:13px; font-weight:500; color:var(--ink2); }

/* ---- horizontal bars ---- */
.hb { display:flex; flex-direction:column; gap:2px; }
.hb-row {
  display:grid; grid-template-columns:var(--labw,148px) 1fr auto;
  gap:10px; align-items:center; padding:2px 0; min-height:24px;
}
.hb-row:hover .hb-track { background:var(--track); }
.hb-lab {
  color:var(--ink2); font-size:12.5px; text-align:right;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.hb-track { height:16px; background:transparent; border-radius:0 4px 4px 0; }
.hb-fill { height:100%; border-radius:0 4px 4px 0; }
/* A zero draws nothing. A 2px minimum would render an empty tier as a sliver
   that reads like "a few", which is the one thing the chart must not do. */
.hb-fill.z { display:none; }
.hb-val {
  font-size:12.5px; font-variant-numeric:tabular-nums; color:var(--ink2);
  min-width:52px;
}
.hb-val em { font-style:normal; color:var(--muted); font-size:11.5px; }

/* ---- column chart ---- */
.cols { display:flex; align-items:flex-end; gap:3px; height:150px;
        position:relative; padding-top:4px; }
.colwrap { flex:1; max-width:24px; display:flex; flex-direction:column;
           justify-content:flex-end; height:100%; cursor:default; min-width:3px; }
.colwrap:hover .seg-b { filter:brightness(1.12); }
.seg-a { background:var(--dim); border-radius:4px 4px 0 0; min-height:2px; }
.seg-b { background:var(--accent); min-height:2px; }
/* the 2px surface gap that separates stacked segments - white does the
   separating, never a stroke around the mark */
.seg-a + .seg-b { margin-top:2px; }
.colwrap .seg-b:only-child { border-radius:4px 4px 0 0; }
.gridlines { position:absolute; inset:0 0 0 0; pointer-events:none; }
.gridlines div { position:absolute; left:0; right:0; height:1px;
                 background:var(--grid); }
.xaxis { display:flex; gap:3px; margin-top:7px; border-top:1px solid var(--axis);
         padding-top:5px; }
.xaxis span { flex:1; max-width:24px; min-width:3px; font-size:10px;
              color:var(--muted); text-align:center; white-space:nowrap;
              overflow:visible; }
.legend { display:flex; gap:16px; margin-top:12px; font-size:12px;
          color:var(--ink2); }
.legend i { width:10px; height:10px; border-radius:3px; display:inline-block;
            margin-right:6px; vertical-align:-1px; }

/* ---- tables ---- */
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th {
  text-align:left; font-weight:500; color:var(--muted); font-size:11.5px;
  padding:0 10px 7px 0; border-bottom:1px solid var(--grid); white-space:nowrap;
}
th.num, td.num { text-align:right; font-variant-numeric:tabular-nums; }
td { padding:7px 10px 7px 0; border-bottom:1px solid var(--grid);
     vertical-align:top; }
tbody tr:hover { background:var(--track); }
td a { color:var(--ink); text-decoration:none; }
td a:hover { text-decoration:underline; }
.meter { width:88px; height:6px; background:var(--track); border-radius:3px;
         display:inline-block; vertical-align:middle; margin-right:8px; }
.meter i { display:block; height:100%; background:var(--accent);
           border-radius:3px; }
.scroll { overflow-x:auto; }

/* ---- chips ---- */
.chip {
  display:inline-block; font-size:11px; padding:1px 7px; border-radius:20px;
  border:1px solid var(--border); color:var(--ink2); white-space:nowrap;
}
.chip i { width:6px; height:6px; border-radius:50%; display:inline-block;
          margin-right:5px; vertical-align:0; }
.terms { display:flex; flex-wrap:wrap; gap:5px; }
.terms .chip { color:var(--muted); font-family:ui-monospace,Menlo,Consolas,monospace;
               font-size:11px; }
.why { color:var(--muted); font-size:11.5px;
       font-family:ui-monospace,Menlo,Consolas,monospace; }

details { margin-top:14px; }
summary { cursor:pointer; color:var(--muted); font-size:12px; }
summary:hover { color:var(--ink2); }
details[open] > summary { margin-bottom:10px; }
.empty { color:var(--muted); font-size:12.5px; padding:18px 0; }

#tip {
  position:fixed; z-index:50; pointer-events:none; opacity:0;
  transition:opacity .08s; background:var(--surface); color:var(--ink);
  border:1px solid var(--border); border-radius:8px; padding:7px 10px;
  font-size:12px; box-shadow:0 6px 20px rgba(0,0,0,.14); max-width:280px;
}
#tip b { font-weight:600; }
#tip .r { color:var(--ink2); }
</style>
</head>
<body>
<div class="wrap">
  <h1>gigbot corpus</h1>
  <p class="sub" id="meta"></p>

  <div class="filters">
    <select id="f-source"></select>
    <select id="f-tier"></select>
    <select id="f-outcome"></select>
    <select id="f-window"></select>
    <input id="f-q" type="search" placeholder="title contains...">
    <button class="ghost" id="f-reset">Reset</button>
    <span class="spacer"></span>
    <span class="count" id="f-count"></span>
  </div>

  <div class="grid">
    <div class="card">
      <div class="hero-row">
        <div class="hero">
          <div class="val" id="hero-val">0</div>
          <div class="lab" id="hero-lab">postings in the corpus</div>
        </div>
        <div class="tiles" id="tiles"></div>
      </div>
    </div>

    <div class="card half">
      <h2>Filter funnel</h2>
      <p class="note">What survives each stage, recomputed against the current
        <code>config.py</code>.</p>
      <div class="hb" id="funnel"></div>
      <details><summary>Table view</summary><div id="funnel-tbl"></div></details>
    </div>

    <div class="card half">
      <h2>Geographic tier</h2>
      <p class="note">Matched postings only, in <code>TIER_ORDER</code> - the
        order they are notified in.</p>
      <div class="hb" id="tiers"></div>
      <details><summary>Table view</summary><div id="tiers-tbl"></div></details>
    </div>

    <div class="card">
      <h2>Volume by publish date</h2>
      <p class="note" id="vol-note"></p>
      <div class="cols" id="vol"><div class="gridlines" id="vol-grid"></div></div>
      <div class="xaxis" id="vol-x"></div>
      <div class="legend">
        <span><i style="background:var(--accent)"></i>Matched</span>
        <span><i style="background:var(--dim)"></i>Filtered out</span>
      </div>
      <details><summary>Table view</summary><div class="scroll" id="vol-tbl"></div></details>
    </div>

    <div class="card half">
      <h2>Source scorecard</h2>
      <p class="note">Match rate is the signal. A source with volume and no
        matches is costing requests for nothing.</p>
      <div class="scroll" id="sources"></div>
      <p class="note silent" id="sources-silent"></p>
    </div>

    <div class="card half">
      <h2>Stack terms in matched postings</h2>
      <p class="note">How often each <code>STACK</code> term appears in what the
        filter kept. The start of the skill-frequency question.</p>
      <div class="hb" id="stack"></div>
    </div>

    <div class="card">
      <h2>Vocabulary audit</h2>
      <p class="note">Corpus-wide - <b>not</b> scoped by the filters above.
        &ldquo;fires&rdquo; counts postings where the term matches within its own
        scope, independent of which stage actually decided the entry.</p>
      <div id="vocab"></div>
    </div>

    <div class="card">
      <h2>Postings</h2>
      <p class="note" id="rows-note"></p>
      <div class="scroll" id="rows"></div>
    </div>
  </div>
</div>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const RAMP = ['--r1','--r2','--r3','--r4','--r5'];
const TIER_LABEL = {pk_eligible:'PK eligible', pk_local:'PK local',
                    unknown:'geo unclear', geo_blocked:'geo restricted'};
const STAGE_LABEL = {
  TITLE_BLOCK:'Blocked by title', TITLE_ROLE:'Title is not a role',
  EXCLUDE:'Excluded', STACK:'No stack term', MATCH:'Matched'};

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n = v => v.toLocaleString('en-US');
const pct = (a,b) => b ? (100*a/b).toFixed(a/b >= 0.1 ? 0 : 1) + '%' : '0%';

function ago(ts) {
  const h = Math.floor((DATA.generated - ts) / 3600);
  if (h < 1) return 'now';
  if (h < 48) return h + 'h';
  const d = Math.floor(h/24);
  return d < 60 ? d + 'd' : Math.floor(d/30) + 'mo';
}
function day(ts) { return new Date(ts*1000).toISOString().slice(0,10); }

/* ---------------------------------------------------------------- filtering */
const state = {source:'', tier:'', outcome:'', win:'', q:''};

function filtered() {
  const cut = state.win ? DATA.generated - state.win*86400 : 0;
  const q = state.q.trim().toLowerCase();
  return DATA.rows.filter(r =>
    (!state.source || r.s === state.source) &&
    (!state.tier || r.g === state.tier) &&
    (!state.outcome ||
      (state.outcome === 'match' ? r.m : state.outcome === 'reject' ? !r.m
        : r.d === 'sent')) &&
    (!cut || r.p >= cut) &&
    (!q || r.t.toLowerCase().includes(q))
  );
}

/* ------------------------------------------------------------------- charts */
function hbars(el, items, opts) {
  opts = opts || {};
  const max = opts.max || Math.max(1, ...items.map(i => i.v));
  el.style.setProperty('--labw', (opts.labw || 148) + 'px');
  el.innerHTML = items.length ? items.map(i => `
    <div class="hb-row" data-tip="${esc(i.tip || (i.label + ': ' + n(i.v)))}">
      <div class="hb-lab" title="${esc(i.label)}">${esc(i.label)}</div>
      <div class="hb-track"><div class="hb-fill${i.v ? '' : ' z'}"
        style="width:${(100*i.v/max).toFixed(2)}%;background:var(${i.c || '--accent'})"></div></div>
      <div class="hb-val">${n(i.v)}${i.sub ? ' <em>'+esc(i.sub)+'</em>' : ''}</div>
    </div>`).join('') : '<p class="empty">Nothing in this slice.</p>';
}

function table(el, cols, rows, empty) {
  if (!rows.length) { el.innerHTML = `<p class="empty">${empty || 'Nothing here.'}</p>`; return; }
  el.innerHTML = '<table><thead><tr>' +
    cols.map(c => `<th class="${c.num?'num':''}">${esc(c.h)}</th>`).join('') +
    '</tr></thead><tbody>' + rows.map(r => '<tr>' +
      r.map((cell,i) => `<td class="${cols[i].num?'num':''}">${cell}</td>`).join('') +
    '</tr>').join('') + '</tbody></table>';
}

/* Funnel: an ordered scale, so an ordinal ramp - light to dark as the set
   narrows, ending on the darkest step for what actually matched. */
function renderFunnel(rows) {
  const total = rows.length;
  const rej = {TITLE_BLOCK:0, TITLE_ROLE:0, EXCLUDE:0, STACK:0};
  let matched = 0;
  rows.forEach(r => r.m ? matched++ : rej[r.st] !== undefined && rej[r.st]++);
  const steps = [
    ['Ingested', total, null],
    ['Past TITLE_BLOCK', total - rej.TITLE_BLOCK, rej.TITLE_BLOCK],
    ['Reads as a role', total - rej.TITLE_BLOCK - rej.TITLE_ROLE, rej.TITLE_ROLE],
    ['Not excluded', total - rej.TITLE_BLOCK - rej.TITLE_ROLE - rej.EXCLUDE, rej.EXCLUDE],
    ['Names a stack', matched, rej.STACK],
  ];
  hbars($('funnel'), steps.map(([label,v,lost],idx) => ({
    label, v, c: RAMP[idx],
    sub: lost === null ? '' : '-' + n(lost),
    tip: `<b>${esc(label)}</b><br><span class="r">${n(v)} of ${n(total)} (${pct(v,total)})` +
         (lost === null ? '' : `<br>${n(lost)} dropped at this stage`) + '</span>',
  })), {max: total || 1, labw: 132});

  table($('funnel-tbl'),
    [{h:'Stage'},{h:'Surviving',num:true},{h:'Dropped here',num:true},{h:'Of corpus',num:true}],
    steps.map(([label,v,lost]) => [esc(label), n(v), lost===null?'-':n(lost), pct(v,total)]));
}

function renderTiers(rows) {
  const m = rows.filter(r => r.m);
  const counts = {};
  m.forEach(r => counts[r.g] = (counts[r.g]||0) + 1);
  const items = DATA.tier_order.map((t,idx) => ({
    label: TIER_LABEL[t] || t,
    v: counts[t] || 0,
    // Prominence tracks desirability: best tier gets the strongest step.
    c: RAMP[Math.max(0, RAMP.length - 1 - idx)],
    sub: pct(counts[t]||0, m.length),
    tip: `<b>${esc(TIER_LABEL[t]||t)}</b><br><span class="r">${n(counts[t]||0)} of ${n(m.length)} matched</span>`,
  }));
  hbars($('tiers'), items, {labw: 108});
  table($('tiers-tbl'), [{h:'Tier'},{h:'Matched',num:true},{h:'Share',num:true}],
    items.map(i => [esc(i.label), n(i.v), i.sub]));
}

/* Emphasis, not categorical: matched in the accent, the rest in the
   de-emphasis gray. Matched is a subset of the total, so the segments stack. */
function renderVolume(rows) {
  const by = new Map();
  rows.forEach(r => {
    if (!r.p) return;
    const d = day(r.p);
    const e = by.get(d) || {t:0, m:0};
    e.t++; if (r.m) e.m++;
    by.set(d, e);
  });
  const days = [...by.keys()].sort().slice(-45);
  const max = Math.max(1, ...days.map(d => by.get(d).t));
  $('vol-note').textContent = days.length
    ? `The ${days.length} most recent day${days.length>1?'s':''} that carry postings `
      + `(${days[0]} to ${days[days.length-1]}). Column height is the day's total; `
      + `the filled portion matched.`
    : 'No dated postings in this slice.';

  $('vol').innerHTML = '<div class="gridlines">' +
    [0,25,50,75].map(p => `<div style="top:${p}%"></div>`).join('') + '</div>' +
    days.map(d => {
      const e = by.get(d), mh = 100*e.m/max, rh = 100*(e.t-e.m)/max;
      return `<div class="colwrap" data-tip="<b>${d}</b><br><span class='r'>${n(e.t)} posting${e.t>1?'s':''}, ${n(e.m)} matched</span>">
        ${e.t>e.m ? `<div class="seg-a" style="height:${rh.toFixed(2)}%"></div>` : ''}
        ${e.m ? `<div class="seg-b" style="height:${mh.toFixed(2)}%"></div>` : ''}
      </div>`;
    }).join('');
  // Label roughly every sixth day; a label per column collides below ~40px.
  const step = Math.max(1, Math.ceil(days.length/7));
  $('vol-x').innerHTML = days.map((d,i) =>
    `<span>${i % step === 0 ? d.slice(5) : ''}</span>`).join('');

  table($('vol-tbl'), [{h:'Day'},{h:'Postings',num:true},{h:'Matched',num:true},{h:'Rate',num:true}],
    days.slice().reverse().map(d => {
      const e = by.get(d);
      return [d, n(e.t), n(e.m), pct(e.m, e.t)];
    }));
}

function renderSources(rows) {
  const by = new Map();
  rows.forEach(r => {
    const e = by.get(r.s) || {t:0, m:0, sent:0, newest:0, stages:{}};
    e.t++; if (r.m) e.m++; if (r.d === 'sent') e.sent++;
    if (r.p > e.newest) e.newest = r.p;
    if (!r.m) e.stages[r.st] = (e.stages[r.st]||0) + 1;
    by.set(r.s, e);
  });
  const list = [...by.entries()].sort((a,b) => b[1].m - a[1].m || b[1].t - a[1].t);
  table($('sources'),
    [{h:'Source'},{h:'Rows',num:true},{h:'Matched',num:true},{h:'Match rate'},
     {h:'Top rejection'},{h:'Newest',num:true}],
    list.map(([src,e]) => {
      const rate = e.t ? e.m/e.t : 0;
      const top = Object.entries(e.stages).sort((a,b) => b[1]-a[1])[0];
      return [
        esc(src), n(e.t), n(e.m),
        `<span class="meter"><i style="width:${(100*rate).toFixed(1)}%"></i></span>` +
          `<span class="num">${pct(e.m, e.t)}</span>`,
        top ? `<span class="why">${esc(STAGE_LABEL[top[0]]||top[0])} &times;${top[1]}</span>` : '-',
        e.newest ? ago(e.newest) : '-',
      ];
    }), 'No sources in this slice.');

  // A feed that returns nothing does not appear in the table at all - it has
  // no rows to appear with. That is the "a dead feed is silent" failure in
  // HANDOFF.md, and the only place it can surface is here, by diffing the
  // corpus against config.FEEDS. Always measured against the whole corpus, not
  // the filtered slice, or picking one source would report the other nine dead.
  const seen = new Set(DATA.rows.map(r => r.s));
  const silent = DATA.feeds.filter(f => !seen.has(f));
  $('sources-silent').innerHTML = silent.length
    ? `<b>${silent.length} of ${DATA.feeds.length} configured feeds have never `
      + `contributed a row:</b> ` + silent.map(f => `<code>${esc(f)}</code>`).join(', ')
      + `. Either newly added, or failing silently - <code>verify_feeds.py</code> says which.`
    : `All ${DATA.feeds.length} configured feeds have contributed at least one row.`;
}

function renderStack(rows) {
  const c = new Map();
  rows.filter(r => r.m).forEach(r => r.k.forEach(t => c.set(t, (c.get(t)||0)+1)));
  const total = rows.filter(r => r.m).length;
  const top = [...c.entries()].sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0])).slice(0,18);
  hbars($('stack'), top.map(([t,v]) => ({
    label: t, v, sub: pct(v, total),
    tip: `<b>${esc(t)}</b><br><span class="r">in ${n(v)} of ${n(total)} matched postings</span>`,
  })), {labw: 128});
}

function renderRows(rows) {
  const LIMIT = 200;
  const sorted = rows.slice().sort((a,b) => b.p - a.p);
  const shown = sorted.slice(0, LIMIT);
  $('rows-note').textContent = sorted.length > LIMIT
    ? `Newest ${LIMIT} of ${n(sorted.length)}. Narrow the filters to see the rest.`
    : `${n(sorted.length)} posting${sorted.length===1?'':'s'}, newest first.`;
  table($('rows'),
    [{h:'Outcome'},{h:'Tier'},{h:'Source'},{h:'Age',num:true},{h:'Title'},{h:'Deciding rule'}],
    shown.map(r => {
      const idx = r.m ? 4 : ['TITLE_BLOCK','TITLE_ROLE','EXCLUDE','STACK'].indexOf(r.st);
      return [
        `<span class="chip"><i style="background:var(${RAMP[idx<0?0:idx]})"></i>${esc(r.m ? 'match' : 'reject')}</span>`,
        `<span class="why">${esc(TIER_LABEL[r.g] || r.g)}</span>`,
        esc(r.s),
        r.p ? ago(r.p) : '-',
        r.l ? `<a href="${esc(r.l)}" target="_blank" rel="noopener">${esc(r.t) || '(untitled)'}</a>`
            : esc(r.t) || '(untitled)',
        `<span class="why">${esc(r.w)}</span>`,
      ];
    }), 'Nothing matches these filters.');
}

function renderKpis(rows) {
  const matched = rows.filter(r => r.m).length;
  const sent = rows.filter(r => r.d === 'sent').length;
  const eligible = rows.filter(r => r.m && r.g === 'pk_eligible').length;
  const blocked = rows.filter(r => r.m && r.g === 'geo_blocked').length;
  const sources = new Set(rows.map(r => r.s)).size;
  $('hero-val').textContent = n(rows.length);
  $('hero-lab').textContent = rows.length === DATA.rows.length
    ? 'postings in the corpus' : 'postings in this slice';
  $('tiles').innerHTML = [
    ['Match rate', `${pct(matched, rows.length)} <small>${n(matched)}</small>`],
    ['PK eligible', n(eligible)],
    ['Geo restricted', n(blocked)],
    ['Delivered', n(sent)],
    ['Sources', n(sources)],
  ].map(([lab,val]) =>
    `<div class="tile"><div class="val">${val}</div><div class="lab">${lab}</div></div>`
  ).join('');
  $('f-count').textContent = `${n(rows.length)} of ${n(DATA.rows.length)} rows`;
}

/* Static: about the vocabulary, not about the slice. */
function renderVocab() {
  const html = Object.entries(DATA.vocab).map(([label,v]) => {
    const rows = v.top.map(([t,c,m]) => [
      `<code>${esc(t)}</code>`, n(c), n(m)]);
    return `<details ${label === 'STACK' ? 'open' : ''}>
      <summary><b>${label}</b> &middot; ${v.size} terms &middot; scope: ${v.scope}
        &middot; ${v.dead.length} never fire</summary>
      <div class="grid" style="gap:18px">
        <div class="card half" style="border:none;padding:0;background:transparent">
          ${rows.length ? '<table><thead><tr><th>Term</th><th class="num">Fires on</th><th class="num">In matched</th></tr></thead><tbody>' +
            rows.map(r => `<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td class="num">${r[2]}</td></tr>`).join('') +
            '</tbody></table>' : '<p class="empty">No term in this list fires anywhere in the corpus.</p>'}
        </div>
        <div class="card half" style="border:none;padding:0;background:transparent">
          <p class="note">Never fires in ${n(DATA.rows.length)} postings. Dead weight,
            or the source that would trigger it is not in <code>FEEDS</code>.</p>
          <div class="terms">${v.dead.length
            ? v.dead.map(t => `<span class="chip">${esc(t)}</span>`).join('')
            : '<span class="empty">Every term fires.</span>'}</div>
        </div>
      </div>
    </details>`;
  }).join('');
  $('vocab').innerHTML = html;
}

/* ------------------------------------------------------------------ wiring */
function render() {
  const rows = filtered();
  renderKpis(rows); renderFunnel(rows); renderTiers(rows);
  renderVolume(rows); renderSources(rows); renderStack(rows); renderRows(rows);
}

function fill(el, opts) {
  el.innerHTML = opts.map(([v,l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
}

function init() {
  $('meta').innerHTML = `<code>${esc(DATA.db)}</code> &middot; ${n(DATA.rows.length)} postings &middot; ` +
    `generated ${new Date(DATA.generated*1000).toLocaleString()} &middot; ` +
    `verdicts recomputed from the current config`;

  const sources = [...new Set(DATA.rows.map(r => r.s))].sort();
  fill($('f-source'), [['', 'All sources'], ...sources.map(s => [s,s])]);
  fill($('f-tier'), [['', 'All tiers'],
    ...DATA.tier_order.map(t => [t, TIER_LABEL[t] || t])]);
  fill($('f-outcome'), [['', 'All outcomes'], ['match','Matched only'],
    ['reject','Rejected only'], ['sent','Delivered only']]);
  fill($('f-window'), [['', 'All time'], ['7','Published last 7 days'],
    ['30','Last 30 days'], ['90','Last 90 days']]);

  const bind = (id, key, num) => $(id).addEventListener('input', e => {
    state[key] = num ? (e.target.value ? +e.target.value : '') : e.target.value;
    render();
  });
  bind('f-source','source'); bind('f-tier','tier');
  bind('f-outcome','outcome'); bind('f-window','win', true); bind('f-q','q');
  $('f-reset').addEventListener('click', () => {
    Object.keys(state).forEach(k => state[k] = '');
    ['f-source','f-tier','f-outcome','f-window','f-q'].forEach(id => $(id).value = '');
    render();
  });

  // One shared tooltip. Hover enhances, it never gates: every value on this
  // page is also in a label, a table view, or the postings table.
  const tip = $('tip');
  document.addEventListener('mouseover', e => {
    const el = e.target.closest('[data-tip]');
    if (!el) { tip.style.opacity = 0; return; }
    tip.innerHTML = el.dataset.tip;
    tip.style.opacity = 1;
  });
  document.addEventListener('mousemove', e => {
    if (tip.style.opacity === '0') return;
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(e.clientX + pad, innerWidth - w - 8) + 'px';
    tip.style.top = Math.max(8, e.clientY - h - pad) + 'px';
  });

  renderVocab();
  render();
}
init();
</script>
</body>
</html>
"""


def render(records: list[dict], vocab: dict, db_path: str) -> str:
    payload = {
        "generated": int(time.time()),
        "db": os.path.abspath(db_path),
        "tier_order": config.TIER_ORDER,
        # Source labels as configured, so the dashboard can name the feeds that
        # produced nothing. The corpus alone cannot: a feed with no rows leaves
        # no trace in it.
        "feeds": [f[0] for f in config.FEEDS],
        "rows": records,
        "vocab": vocab,
    }
    # "</script>" inside a JSON string ends the block early in an HTML parser,
    # and job bodies are full of raw HTML. Escaping the slash is the standard
    # fix and stays valid JSON.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob)


# ---------------------------------------------------------------------- serving

log = logging.getLogger("gigbot.dashboard")

# A render is a full pass over every row with eight regexes, which is ~2s on a
# shared-cpu-1x and grows with the corpus. Nothing in the corpus changes between
# poll cycles, so serve a cached body and rebuild only when the database file
# has actually been written or the cache has gone stale. Without this, holding
# the refresh key would pin the single shared CPU the watcher runs on.
CACHE_TTL = 120.0
_cache: dict[str, object] = {"body": None, "at": 0.0, "mtime": 0.0}
_cache_lock = threading.Lock()


def rendered_page(db_path: str) -> bytes:
    with _cache_lock:
        try:
            mtime = os.path.getmtime(db_path)
        except OSError:
            mtime = 0.0
        fresh = (
            _cache["body"] is not None
            and mtime == _cache["mtime"]
            and time.time() - float(_cache["at"]) < CACHE_TTL
        )
        if not fresh:
            records, vocab = analyze(load_rows(db_path))
            _cache["body"] = render(records, vocab, db_path).encode("utf-8")
            _cache["at"] = time.time()
            _cache["mtime"] = mtime
        return _cache["body"]  # type: ignore[return-value]


class Handler(BaseHTTPRequestHandler):
    """Two routes, no auth. Anyone who reaches the port sees the corpus.

    There was a token gate here and it was removed deliberately: this is a
    personal watcher over public job feeds, and the page is a read-only view of
    postings that were already published somewhere else. The tradeoff is that
    the Fly hostname is all it takes, and *.fly.dev names are enumerable - if
    that stops being acceptable, put it behind `fly proxy` and drop
    [http_service] rather than reinventing a login here.
    """

    server_version = "gigbot"
    protocol_version = "HTTP/1.1"
    db_path = "./seen.db"

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler writes to stderr unformatted, which interleaves
        # badly with the watcher's log lines in `fly logs`.
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing gates the page, so this header is the only thing keeping the
        # corpus out of search results. Cheap, and worth keeping.
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, val in extra or []:
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        path, _, _query = self.path.partition("?")

        # Kept separate from / so a health probe never pays for a render.
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path not in ("/", "/index.html"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        try:
            body = rendered_page(self.db_path)
        except Exception:
            # A viewer must never be able to take the watcher down with it.
            log.exception("dashboard render failed")
            self._send(500, b"render failed, see logs",
                       "text/plain; charset=utf-8")
            return
        self._send(200, body)

    do_HEAD = do_GET


def serve_in_background(port: int, db_path: str) -> ThreadingHTTPServer:
    """Start the dashboard on a daemon thread and return immediately.

    A thread rather than a second process because the Fly volume binds to one
    machine and the corpus is one SQLite file - a second process would mean a
    second reader competing for the same 256MB and the same shared CPU, for a
    page that is read a few times a day. Daemon so that it can never keep the
    watcher alive after the poll loop exits.

    SQLite is opened read-only, per request, on the serving thread. sqlite3
    connections are not shareable across threads, and the read-only handle is
    what makes a concurrent write from the watcher safe to read under.
    """
    Handler.db_path = db_path
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="dashboard").start()
    log.info("dashboard listening on :%d", port)
    return httpd


def run() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "./seen.db"))
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--open", action="store_true", dest="open_it",
                    help="open the result in a browser")
    ap.add_argument("--serve", action="store_true",
                    help="serve over HTTP instead of writing a file")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("DASHBOARD_PORT", "8080")))
    args = ap.parse_args()

    if args.serve:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        serve_in_background(args.port, args.db)
        url = f"http://localhost:{args.port}/"
        print(f"serving {url}  (ctrl-c to stop)")
        if args.open_it:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    rows = load_rows(args.db)
    if not rows:
        raise SystemExit(f"{os.path.abspath(args.db)} has no rows yet.")

    records, vocab = analyze(rows)
    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(records, vocab, args.db))

    matched = sum(r["m"] for r in records)
    print(f"{len(records)} postings, {matched} match, {len(records) - matched} reject")
    print(f"wrote {out}")
    if args.open_it:
        webbrowser.open(f"file:///{out.replace(os.sep, '/')}")


if __name__ == "__main__":
    run()
