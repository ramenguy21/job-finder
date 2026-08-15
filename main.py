"""RSS job watcher. Polls feeds, filters by keyword, pushes hits to Telegram."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import httpx

import config

# Job titles carry emoji ("🚀 Senior Backend Engineer"). The default Windows
# console codec is cp1252 and raises UnicodeEncodeError on them, which killed
# backtest.py mid-listing. Everything downstream imports this module, so
# reconfiguring here covers backtest.py and verify_feeds.py too.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("gigbot")

def load_dotenv(path: str = ".env") -> None:
    """Read .env into os.environ if it exists. Real env always wins.

    Fly injects secrets as real environment variables, so this is a local-dev
    convenience only. It is here because its absence was a silent trap: the
    token sat in .env, nothing read it, every local run took the "creds
    missing" branch in send_telegram() and looked like intended behaviour.
    Twelve lines of stdlib beats a dependency for this.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "./seen.db")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))

# Reddit 403s the default python user-agent.
UA = "gigbot/0.1 (personal job feed reader)"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NOISE_RE = re.compile(r"[^a-z0-9 ]+")
# [Hiring], [HIRING][Remote], (Remote), (Contract) etc.
BRACKET_RE = re.compile(r"[\[\(][^\]\)]{0,30}[\]\)]")
# Characters XML forbids outright.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# An & that isn't already the start of an entity.
BARE_AMP_RE = re.compile(r"&(?!#?\w{1,8};)")

# host -> monotonic timestamp of last request, for rate limiting.
_last_request: dict[str, float] = {}


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    entry_id    TEXT PRIMARY KEY,
    title_hash  TEXT,
    source      TEXT,
    title       TEXT,
    body        TEXT,
    link        TEXT,
    published   INTEGER,
    ingested_at INTEGER,
    notified    INTEGER DEFAULT 0,
    geo_tier    TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_title_hash ON seen(title_hash);
CREATE INDEX IF NOT EXISTS idx_seen_ingested  ON seen(ingested_at);
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # Migration for databases created before geo tiers existed. CREATE TABLE
    # IF NOT EXISTS is a no-op on an existing table, so the column has to be
    # added explicitly. This must happen before any index on geo_tier.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)")}
    if "geo_tier" not in cols:
        log.info("migrating: adding geo_tier column")
        conn.execute("ALTER TABLE seen ADD COLUMN geo_tier TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_geo_tier ON seen(geo_tier)")
    conn.commit()
    return conn


def prune(conn: sqlite3.Connection) -> None:
    cutoff = int(time.time()) - config.RETAIN_DAYS * 86400
    cur = conn.execute("DELETE FROM seen WHERE ingested_at < ?", (cutoff,))
    if cur.rowcount:
        log.info("pruned %d old rows", cur.rowcount)
    conn.commit()


# ---------------------------------------------------------------------- helpers


def strip_html(raw: str) -> str:
    return WS_RE.sub(" ", TAG_RE.sub(" ", raw or "")).strip()


def title_hash(title: str) -> str:
    """Crude cross-feed dedupe. Same job on RemoteOK and WWR collapses.

    Deliberately conservative: it strips bracketed tags like [Hiring] and
    (Remote), which are pure noise, but nothing else. Over-normalizing would
    collapse "Backend Engineer" at two different companies into one hash and
    silently drop a real lead. A duplicate message is cheap; a missed job
    is not.
    """
    norm = BRACKET_RE.sub(" ", (title or "").lower())
    norm = NOISE_RE.sub("", norm)
    norm = WS_RE.sub(" ", norm).strip()
    return hashlib.sha1(norm.encode()).hexdigest()


def entry_published(entry) -> int:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return int(time.mktime(parsed))
    # JSON adapter path: epoch int, or an ISO-ish date string.
    raw = entry.get("_epoch")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(
                    datetime.strptime(raw[:19], fmt)
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except ValueError:
                continue
    return int(time.time())


# ------------------------------------------------------------------- matching


def _build(terms: list[str]) -> re.Pattern[str]:
    """Compile a vocabulary into one regex with non-alphanumeric lookarounds.

    THIS IS THE LOAD-BEARING FUNCTION. Nothing in config.py may be matched
    with `in`. Raw substring matching is what put `rs.` (from PK_LOCAL) inside
    "engineers." and "years." and tagged 81 of 83 corpus rows as Pakistani.

    \\b is not used deliberately: it is defined against \\w, so "node.js"
    would break at the dot and "c#" would never match at all. The lookarounds
    below only care about letters and digits, so punctuation-bearing terms
    behave.

    A space in a term becomes [\\s\\-_]+, so "full stack" also matches
    "full-stack". Longest-first ordering keeps the alternation from settling
    for "react" when "react.js" was available, which matters for --why.
    """
    parts = []
    for term in sorted(set(terms), key=len, reverse=True):
        escaped = re.escape(term.strip().lower())
        # re.escape renders a literal space as "\ " on older versions and " "
        # on newer ones; normalise both to the separator class.
        escaped = escaped.replace("\\ ", " ").replace(" ", r"[\s\-_]+")
        parts.append(escaped)
    # IGNORECASE is belt-and-braces. Callers are expected to pass _norm()'d
    # text, but every one of these patterns is lowercase, so a caller who
    # forgets would get silent false negatives on "AWS" rather than an error.
    # It also makes the lookarounds case-insensitive, so "LAWS" still fails
    # to match "aws" the way it should.
    return re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


TITLE_BLOCK_RE = _build(config.TITLE_BLOCK)
TITLE_ROLE_RE = _build(config.TITLE_ROLE)
EXCLUDE_TITLE_RE = _build(config.EXCLUDE_TITLE)
EXCLUDE_RE = _build(config.EXCLUDE)
STACK_RE = _build(config.STACK)
PK_LOCAL_RE = _build(config.PK_LOCAL)
PK_ELIGIBLE_RE = _build(config.PK_ELIGIBLE)
GEO_BLOCKED_RE = _build(config.GEO_BLOCKED)


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace so multi-word terms match reliably.

    Feed titles carry newlines and doubled spaces often enough that
    "Senior  Backend\\nEngineer" would otherwise miss "backend engineer".
    """
    return WS_RE.sub(" ", (text or "").lower()).strip()


def match_reason(title: str, body: str) -> tuple[bool, str]:
    """Four-stage filter. Returns (keep, why), where why names the stage.

    The stage name is the point: it is what makes `backtest.py --why` able to
    show you which rule made a decision, which is how false positives get
    caught. A bare True/False cannot be tuned against a corpus.

    Stages 1 and 2 see the title only. That split is not cosmetic - matching
    stack terms against the body let "Paralegal, Litigation" through because
    the firm's boilerplate mentioned Python.
    """
    t = _norm(title)
    both = f"{t} {_norm(body)}"

    hit = TITLE_BLOCK_RE.search(t)
    if hit:
        return False, f"TITLE_BLOCK: {hit.group(0)}"

    role = TITLE_ROLE_RE.search(t)
    if not role:
        return False, "TITLE_ROLE: title is not an engineering role"

    hit = EXCLUDE_TITLE_RE.search(t)
    if hit:
        return False, f"EXCLUDE: {hit.group(0)} (title)"

    hit = EXCLUDE_RE.search(both)
    if hit:
        return False, f"EXCLUDE: {hit.group(0)}"

    stack = STACK_RE.search(both)
    if not stack:
        return False, "STACK: no technology term"

    return True, f"STACK: {stack.group(0)} (role: {role.group(0)})"


def classify_geo(text: str) -> str:
    """Assign a geographic tier. Order of checks matters.

    pk_local wins outright: a Pakistani employer's posting won't also be
    US-only. After that, explicit eligibility phrases ("anywhere in the
    world") are checked before exclusions, because boards like WWR use them
    as structured region markers and they're more reliable than prose. A
    posting containing both is genuinely ambiguous and lands in pk_eligible,
    which is the failure direction we want: shown, not silently dropped.
    """
    low = _norm(text)
    if PK_LOCAL_RE.search(low):
        return "pk_local"
    if PK_ELIGIBLE_RE.search(low):
        return "pk_eligible"
    if GEO_BLOCKED_RE.search(low):
        return "geo_blocked"
    return "unknown"


TIER_LABEL = {
    "pk_local": "PK local",
    "pk_eligible": "PK eligible",
    "unknown": "geo unclear",
    "geo_blocked": "geo restricted",
}


# --------------------------------------------------------------------- telegram


def send_telegram(client: httpx.Client, text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        log.warning("telegram creds missing, would have sent:\n%s", text)
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(3):
        try:
            resp = client.post(url, json=payload, timeout=20)
        except httpx.HTTPError as exc:
            log.warning("telegram transport error: %s", exc)
            time.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 5)
            log.info("telegram rate limited, sleeping %ss", wait)
            time.sleep(wait + 1)
            continue
        log.error("telegram %s: %s", resp.status_code, resp.text[:300])
        return False
    return False


def format_message(
    source: str, title: str, body: str, link: str, published: int, tier: str
) -> str:
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(published, timezone.utc)
    hours = int(age.total_seconds() // 3600)
    when = f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"

    snippet = body[:350] + ("..." if len(body) > 350 else "")
    return (
        f"<b>{html.escape(title[:200])}</b>\n"
        f"<i>{html.escape(TIER_LABEL.get(tier, tier))} · "
        f"{html.escape(source)} · {when}</i>\n\n"
        f"{html.escape(snippet)}\n\n"
        f"{html.escape(link)}"
    )


# ------------------------------------------------------------------------- core


def sanitize_xml(raw: str) -> str:
    """Repair the two ways feeds are usually malformed.

    Bare ampersands ("Q&A", "R&D") produce 'not well-formed (invalid token)'.
    Stray control characters produce the same. Neither is fixable by
    feedparser itself, which uses a strict XML parser on Python 3.

    This does NOT fix structural problems like 'mismatched tag' - nothing
    short of a real HTML parser will, and for those the answer is a JSON
    endpoint instead.
    """
    raw = CONTROL_RE.sub("", raw)
    raw = BARE_AMP_RE.sub("&amp;", raw)
    return raw


def http_get(client: httpx.Client, url: str, source: str) -> bytes | None:
    """Fetch with per-host rate limiting and 429 backoff."""
    host = urlparse(url).netloc
    min_gap = config.HOST_MIN_INTERVAL.get(host, config.DEFAULT_MIN_INTERVAL)

    for attempt in range(3):
        elapsed = time.monotonic() - _last_request.get(host, 0.0)
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)

        try:
            resp = client.get(url, timeout=30, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("[%s] transport error: %s", source, exc)
            time.sleep(2 * (attempt + 1))
            continue
        finally:
            _last_request[host] = time.monotonic()

        if resp.status_code == 200:
            return resp.content

        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", 0) or 0)
            if not wait:
                wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
            log.info("[%s] 429, backing off %.0fs", source, wait)
            time.sleep(wait)
            continue

        log.error("[%s] HTTP %s", source, resp.status_code)
        return None

    log.error("[%s] gave up after retries", source)
    return None


def normalize_location(loc: str) -> str:
    """Turn a board's structured location field into a canonical phrase.

    JSON boards give a clean value like "Worldwide" or "United States", but
    the geo lists are written for prose ("us only", "anywhere in the world").
    Bare "anywhere" can't go in PK_ELIGIBLE - it would false-positive on
    "anywhere in the US" - so map the structured value here instead, where
    there's no ambiguity about what the field means.
    """
    low = (loc or "").strip().lower()
    if not low:
        return ""
    open_markers = ("anywhere", "worldwide", "world wide", "global", "remote")
    if any(m in low for m in open_markers) and "only" not in low:
        return f"{loc} anywhere in the world"
    # "United States", "USA", "Europe" as a required location = restricted.
    closed = {
        "united states": "us only", "usa": "usa only", "us": "us only",
        "north america": "us only", "europe": "eu only",
        "united kingdom": "uk only", "uk": "uk only", "canada": "canada only",
        "emea": "emea", "latam": "latam only", "latin america": "latam only",
    }
    for needle, canonical in closed.items():
        if low == needle or low.startswith(needle + ","):
            return f"{loc} {canonical}"
    return loc


def parse_json_jobs(payload: bytes, source: str) -> list:
    """Adapter for boards that publish JSON instead of usable RSS.

    Handles RemoteOK (bare list, first element is legal boilerplate) and
    Remotive ({"jobs": [...]}). Both use different key names, so map onto
    the same shape feedparser gives us.
    """
    try:
        data = json.loads(payload)
    except ValueError as exc:
        log.error("[%s] JSON parse failed: %s", source, exc)
        return []

    if isinstance(data, dict):
        rows = data.get("jobs") or data.get("results") or []
    else:
        rows = data

    entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # RemoteOK's first element is a legal notice with no position/url.
        url = row.get("url") or row.get("apply_url") or ""
        title = row.get("position") or row.get("title") or ""
        if not title or not url:
            continue

        company = row.get("company") or row.get("company_name") or ""
        body = row.get("description") or row.get("summary") or ""
        tags = row.get("tags") or []
        location = normalize_location(
            row.get("location") or row.get("candidate_required_location") or ""
        )

        entries.append(
            {
                "id": str(row.get("id") or url),
                "title": f"{company}: {title}" if company else title,
                # Fold tags and location into the body so the keyword and geo
                # filters can see them. This is where "Worldwide" lives.
                "summary": f"{body} {' '.join(map(str, tags))} {location}",
                "link": url,
                "published_parsed": None,
                "_epoch": (
                    row.get("epoch")
                    or row.get("publication_date")
                    or row.get("date")
                    or row.get("created_at")
                ),
            }
        )
    return entries


def fetch_feed(client: httpx.Client, source: str, url: str, kind: str) -> list:
    payload = http_get(client, url, source)
    if payload is None:
        return []

    if kind == "json":
        return parse_json_jobs(payload, source)

    parsed = feedparser.parse(payload)
    if parsed.entries:
        return parsed.entries

    # Strict parse failed. Try repairing and reparsing before giving up.
    text = payload.decode("utf-8", errors="replace")
    parsed = feedparser.parse(sanitize_xml(text))
    if parsed.entries:
        log.info("[%s] recovered %d entries after sanitizing", source, len(parsed.entries))
        return parsed.entries

    log.error("[%s] unparseable: %s", source, parsed.get("bozo_exception"))
    return []


def run_once(conn: sqlite3.Connection, client: httpx.Client) -> None:
    """One full pipeline pass: ingest everything, then notify the best of it.

    KNOWN GAP - a send that fails is not retried. Phase 1 skips any entry_id
    already in `seen`, so an item whose send failed keeps notified=0 but is
    never reconsidered on a later cycle. It survives in the corpus and
    `backtest.py --replay` will pick it up, but nothing automatic will.

    The obvious fix - re-queue rows with notified=0 - is wrong as the schema
    stands, because bootstrap rows are also notified=0 and re-queueing them
    would fire the ~400-message flood that bootstrap exists to prevent.
    Doing it properly needs a third state (a notify_state column:
    pending/sent/skipped) so "never attempted" and "attempted and failed" stop
    sharing a value. Until then the circuit breaker below at least keeps a
    dead channel from burning 25 minutes a cycle.
    """
    bootstrap = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    if bootstrap:
        log.info("empty database, bootstrapping without notifications")

    age_cutoff = int(time.time()) - config.MAX_AGE_DAYS * 86400
    stats = {"new": 0, "matched": 0, "dupe": 0, "dropped": 0, "sent": 0,
             "failed": 0}
    pending = []

    # -- phase 1: ingest everything, collect candidates --------------------
    for feed in config.FEEDS:
        source, url = feed[0], feed[1]
        kind = feed[2] if len(feed) > 2 else "rss"
        entries = fetch_feed(client, source, url, kind)
        log.info("[%s] %d entries", source, len(entries))

        for entry in entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id:
                continue

            row = conn.execute(
                "SELECT 1 FROM seen WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row:
                continue

            title = entry.get("title", "")
            body = strip_html(
                entry.get("summary") or entry.get("description") or ""
            )
            link = entry.get("link", "")
            published = entry_published(entry)
            thash = title_hash(title)
            tier = classify_geo(f"{title} {body}")

            stats["new"] += 1

            # Insert first, always. The corpus is worth keeping even for
            # entries that never get notified.
            conn.execute(
                "INSERT OR IGNORE INTO seen "
                "(entry_id, title_hash, source, title, body, link, published, "
                " ingested_at, notified, geo_tier) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (entry_id, thash, source, title, body, link, published,
                 int(time.time()), tier),
            )

            if bootstrap:
                continue
            if published < age_cutoff:
                continue
            keep, why = match_reason(title, body)
            if not keep:
                log.debug("[%s] skip %r - %s", source, title[:60], why)
                continue

            stats["matched"] += 1

            if tier == "geo_blocked" and config.DROP_GEO_BLOCKED:
                stats["dropped"] += 1
                continue

            cross = conn.execute(
                "SELECT 1 FROM seen WHERE title_hash = ? AND notified = 1 LIMIT 1",
                (thash,),
            ).fetchone()
            if cross:
                stats["dupe"] += 1
                continue

            pending.append(
                {
                    "entry_id": entry_id, "title_hash": thash, "source": source,
                    "title": title, "body": body, "link": link,
                    "published": published, "tier": tier,
                }
            )

        conn.commit()

    # -- phase 2: sort by tier, then newest first, then send ---------------
    rank = {tier: i for i, tier in enumerate(config.TIER_ORDER)}
    pending.sort(key=lambda p: (rank.get(p["tier"], 99), -p["published"]))

    overflow = pending[config.MAX_NOTIFY_PER_CYCLE:]
    pending = pending[: config.MAX_NOTIFY_PER_CYCLE]

    sent_hashes: set[str] = set()
    consecutive_failures = 0
    for item in pending:
        # Two feeds in the same cycle can carry the same job; the DB check in
        # phase 1 can't catch that because nothing was marked notified yet.
        if item["title_hash"] in sent_hashes:
            stats["dupe"] += 1
            continue

        # Circuit breaker. A blocked or down Telegram costs ~60s per message
        # (three retries behind a 20s timeout), so a full cycle would hang for
        # 25 minutes before finishing. Telegram is SNI-filtered by some ISPs -
        # Pakistan among them - so this is a standing condition when running
        # locally, not a rare outage. Three failures in a row means the
        # channel is down, not that one message was bad.
        if consecutive_failures >= 3:
            log.error(
                "telegram unreachable after %d attempts, abandoning %d "
                "remaining sends this cycle",
                consecutive_failures, len(pending) - stats["sent"],
            )
            break

        msg = format_message(
            item["source"], item["title"], item["body"],
            item["link"], item["published"], item["tier"],
        )
        if send_telegram(client, msg):
            consecutive_failures = 0
            conn.execute(
                "UPDATE seen SET notified = 1 WHERE entry_id = ?",
                (item["entry_id"],),
            )
            sent_hashes.add(item["title_hash"])
            stats["sent"] += 1
            time.sleep(0.5)  # stay well under Telegram's rate limit
        else:
            # Stays notified=0. See the note in the docstring of run_once
            # about why that is not yet a durable retry.
            consecutive_failures += 1
            stats["failed"] += 1

    if overflow and consecutive_failures < 3:
        by_tier: dict[str, int] = {}
        for item in overflow:
            by_tier[item["tier"]] = by_tier.get(item["tier"], 0) + 1
        breakdown = ", ".join(
            f"{n} {TIER_LABEL.get(t, t)}" for t, n in sorted(by_tier.items())
        )
        send_telegram(
            client,
            f"<i>+{len(overflow)} more matches held back this cycle "
            f"({html.escape(breakdown)}). Query the DB or loosen EXCLUDE.</i>",
        )
        # Held-back items stay notified=0, so they're queryable but won't be
        # re-sent next cycle. That's deliberate: a backlog you never clear is
        # worse than a backlog you can SELECT.
        for item in overflow:
            conn.execute(
                "UPDATE seen SET notified = 1 WHERE entry_id = ?",
                (item["entry_id"],),
            )

    conn.commit()
    prune(conn)
    log.info(
        "cycle done: %d new, %d matched, %d dupes, %d geo-dropped, "
        "%d sent, %d failed, %d held",
        stats["new"], stats["matched"], stats["dupe"], stats["dropped"],
        stats["sent"], stats["failed"], len(overflow),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    args = ap.parse_args()

    conn = db_connect()
    with httpx.Client(headers={"User-Agent": UA}) as client:
        if args.once:
            run_once(conn, client)
            return
        while True:
            try:
                run_once(conn, client)
            except Exception:
                log.exception("cycle failed, continuing")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()