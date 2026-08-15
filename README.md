# gigbot

Polls RSS job feeds, filters by keyword, pushes matches to Telegram. One
process, SQLite for state, no external services.

## Telegram setup (3 minutes)

1. Message `@BotFather`, send `/newbot`, follow prompts, copy the token.
2. Send any message to your new bot (it can't message you first).
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[0].message.chat.id`. That's your chat id.

## Local

```bash
uv sync
cp .env.example .env      # fill in token and chat id
set -a && source .env && set +a
uv run python main.py --once
```

The first run against an empty DB **bootstraps**: it records every entry as
seen without notifying. This is deliberate, otherwise you'd get several
hundred messages at once. Run it twice to see real behaviour.

## Deploy to Fly

```bash
fly launch --no-deploy --copy-config   # pick a unique app name
fly volumes create gigbot_data --size 1 --region bom
fly secrets set TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
fly secrets set DASHBOARD_TOKEN=$(openssl rand -hex 24)
fly deploy
fly logs
```

Roughly $2/month for a shared-cpu-1x machine plus a 1GB volume.

The machine serves the corpus dashboard on its public hostname. Open
`https://<app>.fly.dev/?t=<DASHBOARD_TOKEN>` once — that sets a cookie, and the
bare URL works afterwards. Without the secret the server answers 503 rather
than publishing every posting it has collected to a guessable hostname.

Keep `auto_stop_machines = 'off'` in `fly.toml`. This is a poll loop, not a web
app: if Fly stops the machine because nobody is loading the dashboard, it stops
watching the feeds too, and nothing tells you.

Note: a volume is tied to one machine. Keep this at a single instance
(`fly scale count 1`). Two machines would each get their own volume, their own
`seen.db`, and you'd get every notification twice.

## Deploy to Railway instead

Railway autodetects the Dockerfile. Add a volume mounted at `/data`, set the
same two secrets, deploy. Don't use Railway's cron feature here, this process
manages its own schedule.

## Geo priority

Every entry is classified into a tier and notifications are sorted by it:

| tier          | meaning                                                |
| ------------- | ------------------------------------------------------ |
| `pk_local`    | Pakistani employer (Karachi/Lahore/PKR/Rozee signals)  |
| `pk_eligible` | explicitly worldwide, APAC, South Asia, or Gulf        |
| `unknown`     | says "remote", says nothing about where                |
| `geo_blocked` | US-only, EU-only, UK-only, work-authorization required |

Order lives in `config.TIER_ORDER`. `geo_blocked` postings are demoted to the
bottom rather than dropped, so you can see what the filter is catching. Once
you trust it, set `DROP_GEO_BLOCKED = True`.

Check what the classifier is doing to your real corpus:

```sql
SELECT geo_tier, COUNT(*) FROM seen GROUP BY geo_tier;

-- spot-check for false positives before enabling the drop
SELECT title, substr(body,1,120) FROM seen WHERE geo_tier='geo_blocked' LIMIT 20;
```

## Backtesting your filters

The bootstrap run stores everything without notifying, so after one run you
have a real corpus to tune against:

```bash
uv run python backtest.py            # what would match, by geo tier
uv run python backtest.py --rejected --why   # what got dropped and why
uv run python backtest.py --replay   # send the matches you missed
```

Tune `MUST_HAVE` / `EXCLUDE` in `config.py`, re-run, repeat. No waiting for
new postings.

## The dashboard

```bash
uv run python dashboard.py --open     # dashboard.html, opens in a browser
uv run python dashboard.py --serve    # http://localhost:8080 instead
```

The same corpus as `backtest.py`, as one self-contained HTML page: the filter
funnel, per-source match rates, geo tiers, stack-term frequency across matched
postings, and a vocabulary audit showing which `config.py` terms never fire at
all. Filters at the top scope every chart at once.

Every verdict on the page is recomputed from the current `config.py`, not read
from the database — edit a vocabulary, re-render, see the effect. It opens the
DB read-only and never writes to it.

## Verifying feeds

```bash
uv run python verify_feeds.py
```

Prints entry counts and a geo-tier breakdown per feed. Dead feeds fail
silently in production (they log and the cycle continues), so run this when
you add a feed and occasionally after. The Pakistani board feeds in
`config.py` are commented out and **unverified** — check them here before
enabling.

## Tuning

Everything you'll want to change lives in `config.py`: `FEEDS`, `MUST_HAVE`,
`EXCLUDE`. Expect the first week to be noisy. When something irrelevant comes
through, add a term to `EXCLUDE` and redeploy.

To see what's being caught and dropped:

```sql
-- what matched but you never saw (cross-feed dupes)
SELECT source, title FROM seen WHERE notified = 0 ORDER BY ingested_at DESC LIMIT 50;

-- volume by source, to find dead feeds
SELECT source, COUNT(*) FROM seen GROUP BY source ORDER BY 2 DESC;
```

`fly ssh console -C "sqlite3 /data/seen.db"` to poke at it in production.

## Note on the corpus

Every entry is stored with full title and body, including ones that fail the
keyword filter. That's intentional. In a couple of months there's enough text
here to run skill-frequency and co-occurrence analysis over, which is a much
better signal for what to learn next than the notifications themselves.
Dropping the column later is easy; recovering the data isn't.
