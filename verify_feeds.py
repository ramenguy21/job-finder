"""Check every feed in config.FEEDS actually returns parseable entries.

Run this after adding a feed, and periodically: board feeds churn and a dead
feed fails silently in production (it just logs and moves on).

    uv run python verify_feeds.py
"""

import sys

import httpx

import config
import main as gb


def main() -> int:
    failures = 0
    with httpx.Client(headers={"User-Agent": gb.UA}) as client:
        for feed in config.FEEDS:
            source, url = feed[0], feed[1]
            kind = feed[2] if len(feed) > 2 else "rss"
            entries = gb.fetch_feed(client, source, url, kind)

            if not entries:
                print(f"  FAIL  {source:<18} 0 entries")
                failures += 1
                continue

            geo = {}
            match = 0
            for entry in entries:
                title = entry.get("title", "")
                # Same strip_html the ingest path applies. Without it the raw
                # markup counts as body text and the match rate here would not
                # be the one run_once() sees.
                body = gb.strip_html(
                    entry.get("summary") or entry.get("description") or ""
                )
                tier = gb.classify_geo(f"{title} {body}")
                geo[tier] = geo.get(tier, 0) + 1
                if gb.match_reason(title, body)[0]:
                    match += 1
            spread = " ".join(f"{k}={v}" for k, v in sorted(geo.items()))
            print(
                f"  ok    {source:<18} {len(entries):>3} entries, "
                f"{match:>3} match  [{spread}]"
            )

    print(f"\n{failures} failing feed(s) of {len(config.FEEDS)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())