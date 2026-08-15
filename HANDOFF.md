# gigbot handoff

Personal job-feed watcher. Polls RSS/JSON job boards, filters by role and
stack, ranks by geographic eligibility, pushes matches to Telegram.

Single process, SQLite for state, no external services beyond the feeds and
the Telegram API.

---

## Status

**Working and tested locally.** Deployed: not yet.

| Component | State |
|---|---|
| Feed ingestion (RSS + JSON) | working |
| Rate limiting + 429 backoff | working, exercised against live 429s |
| XML sanitize-and-retry | working, not needed in practice (WWR parses strict) |
| Filtering (4-stage) | working, rebuilt and measured against the corpus |
| Geo tiering | working, rebuilt and measured against the corpus |
| Telegram delivery | **blocked by ISP locally — see below** |
| Dashboard (`dashboard.py`) | working, rendered and checked in both themes |
| Dashboard over HTTP on Fly | wired and tested locally, **never deployed** |
| Fly deployment | config written, never deployed |
| RemoteOK / Remotive JSON URLs | **verified, both 200** |
| Reddit feeds | **mostly 403 Blocked** |

---

## Layout

```
main.py           ~660 lines. Everything: fetch, filter, classify, send.
config.py         ~215 lines. Feed list + keyword vocabularies. Edit this.
backtest.py       ~140 lines. Tune filters against the stored corpus.
verify_feeds.py    ~60 lines. Check feeds resolve and see match rates.
dashboard.py      ~900 lines. The corpus as one self-contained HTML page,
                        mostly template. Read-only. Also serves itself.
Dockerfile              uv-based build.
fly.toml                Single machine + volume at /data + the dashboard.
seen.db                 SQLite. Gitignored. This is all the state there is.
```

`main.py` sections in order: db → helpers → matching → telegram → core.
No classes, no framework. `run_once()` is the whole pipeline.

---

## Pipeline

```
for each feed:
    http_get()          rate limited per host, 429 backoff, 3 attempts
    fetch_feed()        RSS via feedparser, or JSON adapter
                        on strict-parse failure: sanitize_xml() and retry
    for each entry:
        INSERT into seen        <- always, before any filtering
        skip if bootstrap / too old
        match_reason()          <- 4-stage filter
        classify_geo()          <- pk_local | pk_eligible | unknown | geo_blocked
        append to pending

sort pending by (TIER_ORDER, newest first)
send top MAX_NOTIFY_PER_CYCLE, summarise the rest
prune rows older than RETAIN_DAYS
```

Two-phase by necessity: you cannot prioritize a stream you have already sent.

---

## Decisions worth not re-litigating

**Insert before filter.** Every entry is stored with full title and body even
if it fails the filter. The corpus is the point — it is what `backtest.py`
runs against, and eventually what a skill-frequency analysis would run
against. Cheap to drop later, impossible to recover.

**Bootstrap on empty DB.** First run records everything as seen and notifies
nothing, otherwise deploy day is ~400 messages. Consequence: a lost volume
fails *silently*. If notifications stop after a redeploy, check the volume
before checking anything else.

**Dedupe is deliberately weak.** `title_hash` strips `[Hiring]`-style
bracketed tags and nothing more. Normalizing harder collapses "Backend
Engineer at Acme" into "Backend Engineer at Globex" and silently drops a real
lead. A duplicate message costs two seconds; a missed job does not.

**Word-boundary matching, never substrings.** This is the big one, and it is
now actually implemented — every vocabulary compiles through `_build()` into
one regex with non-alphanumeric lookarounds.

Measured against the 241-row corpus, the old substring version was producing:

| term | fired on | of which real |
|---|---|---|
| `rs.` (PK_LOCAL) | 81 rows | 0 — matched "enginee**rs.**", "yea**rs.**" |
| `multan` (PK_LOCAL) | 4 rows | 0 — matched "si**multan**eously" |
| `apac` (PK_ELIGIBLE) | 14 rows | 1 — matched "**apac**he" |
| `aws` (STACK) | 81 rows | 36 — matched "l**aws**", "dr**aws**" |

83 of 241 rows were tagged `pk_local`. The corpus does not contain a single
posting that mentions Pakistan. All of it was `rs.`.

**Rule: never add a term under three characters or ending in punctuation.**
`_build()` does not rescue you from this. `go` is a real English word — it
matched "we **go** through discovery" and put a Paralegal listing in the
results. Golang is caught by `golang`, `go developer`, `goroutine` instead.

**Title gate before stack matching.** Matching stack terms against the body
alone let "Paralegal, Litigation" through because the boilerplate mentioned
Python. The title decides the role; the body only confirms the stack.

**EXCLUDE is split by scope.** CMS names and freelancer markers match the
**title only**; hard disqualifiers match title + body. This was measured:
4 of 5 CMS exclusions were body-only false positives killing real leads
(Lemon.io's "Senior AI Engineer", A.Team's "$90-170/hr Senior Developer").
A CMS in the title means it is a WordPress job. A CMS in paragraph nine of a
staffing agency's skill list means nothing. A US citizenship requirement, by
contrast, disqualifies you wherever it is written — that stays title+body.

**Single instance only.** The Fly volume binds to one machine. `scale count 2`
gives two independent databases and duplicate notifications.

---

## The 4-stage filter

`match_reason(title, body) -> (bool, why)`. The `why` string names the
deciding stage, which is what makes `backtest.py --why` and `--stages` useful.

1. `TITLE_BLOCK` — non-engineering + wrong seniority. Title only.
2. `TITLE_ROLE` — title must read as an engineering role. Title only.
3. `EXCLUDE` — `EXCLUDE_TITLE` on the title, `EXCLUDE` on title + body.
4. `STACK` — at least one technology term in title + body.

Current corpus performance (241 rows, `backtest.py --stages`):

```
match:  66      TITLE_BLOCK  75
reject: 175     TITLE_ROLE   66
                EXCLUDE      24
                STACK        10
```

Geo tiers are independent and only affect *ordering*, never inclusion —
except `geo_blocked` when `DROP_GEO_BLOCKED = True` (currently `False`).

---

## Resolved this session

1. **`.env` was never loaded.** Nothing in the project read it — `main.py`
   went straight to `os.environ`. The token had been sitting in `.env` the
   whole time, so every local run took the "creds missing, would have sent"
   branch and looked like intended behaviour. `load_dotenv()` now reads it;
   real environment variables still win, so Fly is unaffected.

2. **RemoteOK and Remotive both resolve.** 200 OK, 100 and 18 entries. The
   adapter handles both shapes. Note RemoteOK's `/api` is no longer a
   dev-only board — it now carries "Gardener Handyman Driver" and "Room
   Attendant", so a *low* match rate there is correct, not a bug.

3. **Corpus reclassified.** `backtest.py --reclassify` rewrote 90 of 241
   stored `geo_tier` values. `pk_local` went 83 → 0, which is right.

4. **Seniority question, answered with data.** `staff` and `principal` were
   blocking 14 postings, 13 of them real senior IC roles (Coinbase, Gusto,
   Datadog, Twilio, Faire). Both are now out of `TITLE_BLOCK_SENIORITY`.
   `manager`/`director`/`head of` stay: they block 26, of which 20 are pure
   non-engineering (Internal Audit, Clearing Operations, Threat Assessment)
   and 6 are Engineering Manager — a management ladder, deliberately out.

5. **`emea` removed from `PK_ELIGIBLE`.** It means Europe/Middle East/Africa;
   Pakistan is South Asia and outside it. It was promoting 4 postings on a
   region that does not include you. The explicit Gulf terms stay.

6. **Unicode crash fixed.** A feed title containing an emoji killed
   `backtest.py` mid-listing on the Windows cp1252 console. `main.py` now
   reconfigures stdout/stderr to UTF-8, which covers all three entry points.

---

## Open risks

1. **Telegram is blocked at your ISP.** Not a code bug. TCP to
   `api.telegram.org` connects in 0.2s and the TLS handshake then times out —
   the signature of SNI filtering. Every other host (WWR, RemoteOK, GitHub)
   handshakes in under 0.2s. Pakistan restricts Telegram.

   ```
   api.telegram.org    TCP ok 0.2s   TLS FAIL: handshake timed out
   weworkremotely.com  TCP ok 0.1s   TLS ok 0.0s
   ```

   Consequences: you cannot verify delivery locally without a VPN, and
   `--replay` will not work from home either. It should work from Fly, whose
   machines are outside that filtering — **that is now the single biggest
   untested assumption in the project.** Verify it the moment you deploy.

   The message payload itself is fine: `&` escapes to `&amp;` correctly, so
   the "can't parse entities" worry never materialised. If it ever does, drop
   `parse_mode` from the payload in `send_telegram()`.

2. **Reddit is mostly 403 Blocked.** Not rate limiting — a 403 is the
   unauthenticated user-agent being refused, and no backoff fixes it.
   `HOST_MIN_INTERVAL` is raised to 5s, which helps the 429s; `r/forhire` did
   return 50 entries on the last run, so it is intermittent rather than
   uniformly dead. If it stays this way the real fix is OAuth via PRAW.
   `http_get()` does not retry a 403, so a blocked feed costs one request.

3. **A failed send is never retried.** Phase 1 skips any `entry_id` already
   in `seen`, so an item whose send failed keeps `notified = 0` but is never
   reconsidered. `backtest.py --replay` picks it up; nothing automatic does.
   The obvious fix — re-queue `notified = 0` — is wrong as the schema stands,
   because bootstrap rows are also `notified = 0` and re-queueing them fires
   the ~400-message flood bootstrap exists to prevent. Doing it properly
   needs a `notify_state` column (pending/sent/skipped) so "never attempted"
   and "attempted and failed" stop sharing a value.

   A circuit breaker limits the damage meanwhile: three consecutive failures
   abandon the rest of the cycle. Without it a dead channel costs ~60s per
   message and a full cycle hangs for 25 minutes.

---

## The dashboard

`dashboard.py` renders the corpus to one self-contained HTML file — no CDN, no
charting library, no network access at all, so it opens over `file://`. Every
verdict on it is recomputed live through `main.match_reason()` and
`main.classify_geo()`, never read from the stored `geo_tier` / `notify_state`.
Edit a vocabulary, re-render, see the effect. It is `backtest.py` with a
browser: the filter funnel is `--stages`, the postings table is `--why`.

What it answers that the terminal did not:

- **Which feeds earn their requests.** `r/forhire` is 135 rows and 5 matches;
  `wwr:backend` is 27 rows and 18. Both cost the same in the poll loop.
- **Which feeds contribute nothing at all.** A feed with zero rows cannot
  appear in a per-source table, so the scorecard diffs the corpus against
  `config.FEEDS` and names the difference. That is the "a dead feed is silent"
  gotcha, finally visible.
- **Which vocabulary terms are dead weight.** Corpus-wide firing counts per
  term, per list, plus the terms that never fire. `frappe` fires on 38
  postings and appears in zero matches — worth a look before the next
  `STACK` edit.

On Fly, `main.py` starts it on a daemon thread when `DASHBOARD_PORT` is set,
and `[http_service]` routes to it.

**There is no auth.** A token gate was built and then deliberately removed —
this is a personal watcher over public job feeds, and the page is a read-only
view of postings already published elsewhere. The cost is real and worth
stating once: `https://<app>.fly.dev/` serves the entire corpus to anyone who
asks, and `*.fly.dev` hostnames are enumerable. An `X-Robots-Tag: noindex`
header keeps it out of search results, which is the only thing left protecting
it. If that stops being acceptable, delete `[http_service]` from `fly.toml` and
reach it with `fly proxy 8080:8080` — nothing in the code needs to change.

What does still guard the *watcher* from the dashboard:

- **Read-only, cached, daemon thread.** It opens SQLite with `mode=ro`, caches
  the rendered body for 120s or until the DB file changes, and cannot keep the
  process alive. A render is ~2s of regex over the whole corpus; without the
  cache, a held refresh key would pin the one shared CPU the watcher runs on.
- A render failure returns 500 and is logged. A viewer must never be able to
  take the poll loop down with it.

---

## Next steps, roughly ordered

1. Deploy to Fly and confirm Telegram delivers from there. Everything else is
   downstream of that one unknown. Confirm the dashboard answers on
   `https://<app>.fly.dev/` in the same pass — it is the fastest check that the
   volume mounted and the corpus is where it should be.
2. `--replay` the backlog once delivery is confirmed.
3. Watch `fly logs` for a day.
4. Decide on Reddit: PRAW/OAuth, or drop the nine feeds and lean on WWR +
   RemoteOK + Remotive.
5. Add a `notify_state` column and retire risk 3.
6. Add a `my_verdict` column (applied / skipped) and fill it manually. Two
   weeks of that gives labelled data to calibrate against.
7. Only then consider: Greenhouse/Lever board polling (highest signal-to-noise
   source available, needs a curated company list), LLM scoring, skill
   trend analysis over the corpus.

Step 7 is where this becomes a portfolio piece rather than a utility. It is
also where scope creep lives — the ugly version is the one that finds the job.

---

## Gotchas

- `backtest.py` reads `DB_PATH` from env, defaults to `./seen.db`. Running it
  from the wrong directory silently creates an empty DB and reports zero — it
  now says so explicitly instead of printing a confusing 0/0.
- `db_connect()` migrates old schemas by `ALTER TABLE`. The `geo_tier` index
  must be created *after* the column — that ordering bug already bit once.
- Overflow items past `MAX_NOTIFY_PER_CYCLE` get `notified = 1` without being
  sent, so they never replay. You get a one-line summary instead. To find
  them, query by `ingested_at` and `geo_tier` rather than by `notified`.
  A backlog you can never clear is worse than one you have to SELECT.
- Feed failures are logged and skipped, never fatal. A dead feed is silent —
  run `verify_feeds.py` periodically. The dashboard's source scorecard now
  names any feed in `FEEDS` with zero rows, which is the cheapest way to
  notice.
- `fly.toml` used to be described here as having no `[http_service]`. It always
  did — `fly launch` wrote one — and it was set to `auto_stop_machines = 'stop'`
  with `min_machines_running = 0`. Nothing listened on 8080, so Fly was free to
  stop an idle machine and silently stop the polling with it. That is now
  `'off'` / `1`, and the dashboard is what listens.
- A full local cycle takes ~7 minutes, almost all of it Reddit backoff.
