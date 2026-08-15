"""Test the 4-stage filter and geo rules against postings already in the DB.

The bootstrap run recorded everything without notifying. That corpus is the
best tuning data you have - use it instead of waiting for new postings.

    uv run python backtest.py              # what would match
    uv run python backtest.py --rejected   # what the filter dropped
    uv run python backtest.py --why        # which STAGE decided, per entry
    uv run python backtest.py --stages     # rejection counts per stage
    uv run python backtest.py --reclassify # rewrite stored geo_tier values
    uv run python backtest.py --replay     # actually send the matches

--why prints the stage name, not just a term. That is the whole point: a
false positive is diagnosed by seeing which rule let it through.
"""

from __future__ import annotations

import argparse
import os
import sqlite3

import config
import main as gb


def run() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rejected", action="store_true", help="show non-matches")
    ap.add_argument("--why", action="store_true", help="show the deciding stage")
    ap.add_argument("--stages", action="store_true",
                    help="summarise which stage rejected what")
    ap.add_argument("--reclassify", action="store_true",
                    help="recompute and store geo_tier for every row")
    ap.add_argument("--replay", action="store_true",
                    help="send matches to Telegram and mark them notified")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    conn = sqlite3.connect(os.environ.get("DB_PATH", "./seen.db"))
    rows = conn.execute(
        "SELECT entry_id, source, title, body, link, published, geo_tier, notified "
        "FROM seen ORDER BY published DESC"
    ).fetchall()

    if not rows:
        print(f"empty corpus at {os.environ.get('DB_PATH', './seen.db')} - "
              "wrong directory? backtest.py creates the DB if it is missing.")
        return

    # The stored geo_tier values were written by the substring classifier and
    # are wrong wherever `rs.` fired. Always score against a fresh
    # classification; --reclassify writes the corrected value back.
    if args.reclassify:
        changed = 0
        for entry_id, _s, title, body, _l, _p, old, _n in rows:
            new = gb.classify_geo(f"{title or ''} {body or ''}")
            if new != old:
                conn.execute(
                    "UPDATE seen SET geo_tier = ? WHERE entry_id = ?",
                    (new, entry_id),
                )
                changed += 1
        conn.commit()
        print(f"reclassified {changed} of {len(rows)} rows\n")

    matched, rejected = [], []
    for entry_id, source, title, body, link, published, tier, notified in rows:
        ok, why = gb.match_reason(title or "", body or "")
        tier = gb.classify_geo(f"{title or ''} {body or ''}")
        rec = (entry_id, source, title, link, published, tier, why, notified)
        (matched if ok else rejected).append(rec)

    print(f"corpus: {len(rows)} postings")
    print(f"  match:  {len(matched)}")
    print(f"  reject: {len(rejected)}\n")

    by_tier: dict[str, int] = {}
    for rec in matched:
        by_tier[rec[5]] = by_tier.get(rec[5], 0) + 1
    print("matches by geo tier:")
    for tier in config.TIER_ORDER:
        print(f"  {tier:<14} {by_tier.get(tier, 0)}")
    print()

    if args.stages:
        stages: dict[str, int] = {}
        for rec in rejected:
            stage = rec[6].split(":")[0]
            stages[stage] = stages.get(stage, 0) + 1
        print("rejected by stage:")
        for stage, n in sorted(stages.items(), key=lambda kv: -kv[1]):
            print(f"  {stage:<14} {n}")
        print()

    show = rejected if args.rejected else matched
    rank = {t: i for i, t in enumerate(config.TIER_ORDER)}
    show.sort(key=lambda r: (rank.get(r[5], 99), -(r[4] or 0)))

    label = "REJECTED" if args.rejected else "MATCHED"
    print(f"--- {label} (top {args.limit}) ---")
    for entry_id, source, title, link, published, tier, why, notified in show[: args.limit]:
        flag = " " if notified else "*"
        print(f"{flag} [{tier:<12}] {(title or '')[:72]}")
        if args.why:
            print(f"             {why}   ({source})")

    if not args.replay:
        print("\n('*' = never notified. Use --replay to send the matches.)")
        return

    import httpx

    print(f"\nreplaying {len(matched)} matches...")
    sent = 0
    with httpx.Client(headers={"User-Agent": gb.UA}) as client:
        for entry_id, source, title, link, published, tier, why, notified in show:
            if notified:
                continue
            row = conn.execute(
                "SELECT body FROM seen WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            msg = gb.format_message(
                source, title or "", row[0] or "", link or "", published or 0, tier
            )
            if gb.send_telegram(client, msg):
                conn.execute(
                    "UPDATE seen SET notified = 1 WHERE entry_id = ?", (entry_id,)
                )
                sent += 1
                if sent % 10 == 0:
                    conn.commit()
    conn.commit()
    print(f"sent {sent}")


if __name__ == "__main__":
    run()