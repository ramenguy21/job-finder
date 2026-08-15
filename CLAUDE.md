# CLAUDE.md

Read `HANDOFF.md` first for state and rationale. This file covers conventions.

## What this is

Single-process job-feed watcher. RSS/JSON in, filtered Telegram messages out.
SQLite is the only state. Deliberately small — resist adding structure.

## Commands

```bash
uv sync
uv run python main.py --once          # one cycle, then exit
uv run python main.py                 # sleep loop, POLL_INTERVAL seconds
uv run python backtest.py --why       # tune filters against stored corpus
uv run python backtest.py --rejected --why
uv run python backtest.py --stages    # rejection counts per filter stage
uv run python backtest.py --reclassify   # after editing geo vocabularies
uv run python verify_feeds.py         # check feeds resolve, see match rates
uv run python dashboard.py --open     # render the corpus to dashboard.html
uv run python dashboard.py --serve    # ...over HTTP instead, like Fly does
```

Env: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DB_PATH`, `POLL_INTERVAL`,
`DASHBOARD_PORT`, `DASHBOARD_TOKEN`.
`main.py` loads `.env` at import via `load_dotenv()`; real environment
variables take precedence, so Fly is unaffected. Without a token,
`send_telegram()` logs instead of sending — that is a valid local-dev path,
not a bug.

Telegram is SNI-blocked by some ISPs (Pakistan among them). A TLS handshake
timeout to `api.telegram.org` while other hosts connect fine is the network,
not the code. See `HANDOFF.md`.

## Conventions

- Standard library plus `feedparser` and `httpx`. Do not add dependencies
  without a concrete reason.
- No classes, no framework, no service layer. Functions in `main.py`.
- All tunable vocabulary lives in `config.py`. Logic lives in `main.py`.
  Adding a keyword should never require touching `main.py`.
- Every feed failure is logged and skipped. A cycle must never crash on one
  bad feed.
- Comments explain _why_, especially where a naive approach was tried and
  failed. Several comments document real bugs — do not delete them.

## Hard rules

**Never use substring matching on keyword vocabularies.** Everything goes
through `_build()`, which compiles terms into one regex with non-alphanumeric
lookarounds. An early version matched raw substrings and `rs.` in the Pakistan
list matched "engineers." and "years.", mislabelling every US posting.

**Never add a vocabulary term under three characters or ending in
punctuation.** Same reason. Word boundaries do not rescue you from terms that
are also ordinary English: `go` matched "we go through discovery" and put a
Paralegal listing in the results, which is why `STACK` spells out `golang`,
`go developer` and `goroutine` instead.

**Scope an exclusion to the title unless it disqualifies you outright.**
`EXCLUDE_TITLE` matches the title only, `EXCLUDE` matches title + body.
"wordpress" in a staffing agency's skill list is noise; "us citizen" in
paragraph nine is a hard no.

**Never notify without inserting first.** Entries are written to `seen`
before any filtering. The corpus has value independent of notifications.

**The dashboard is a viewer and opens the DB read-only.** `dashboard.py`
recomputes every verdict from `config.py` rather than reading the stored
`geo_tier` / `notify_state` — editing a vocabulary and reloading shows the
effect. It must never migrate, reclassify or prune; `backtest.py --reclassify`
stays the only writer. It also serves nothing without `DASHBOARD_TOKEN`: the
Fly hostname is public and the page embeds the whole corpus.

**Keep dedupe conservative.** `title_hash` strips bracketed tags only.
Aggressive normalization drops real leads.

## Testing

No test suite. Verification is done with inline fixtures — synthetic feeds,
`httpx.MockTransport` for rate-limit and retry paths, and `backtest.py`
against the real corpus.

If adding a filter or classifier change, check it against the stored corpus
with `backtest.py --why` before trusting it. The `why` string should name the
stage that decided, which is how false positives get caught.

## When changing filters

Match rate is the signal, and `backtest.py --stages` is how you read it.
Current baseline on the 241-row corpus:

```
match:  66      TITLE_BLOCK  75
reject: 175     TITLE_ROLE   66
                EXCLUDE      24
                STACK        10
```

If matches collapse toward zero, `TITLE_BLOCK` is too aggressive. If `STACK`
starts rejecting double digits, it is eating real roles — check the bodies
before loosening, because marketing-heavy postings that name no technology
are genuinely low signal.

Check both directions. A vocabulary change that only ever adds matches has
not been tested; run `--rejected --why` too.
