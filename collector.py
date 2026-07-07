"""
NaijaPress Collector — daily news collection for Nigerian newspapers.

What this script does, in plain terms:
  1. Checks each newspaper's RSS feed for links to new articles.
  2. Skips anything already in your database (no duplicates).
  3. Visits each new article page politely (one page every few seconds),
     and extracts the headline, author, publish date, section and full text.
  4. Saves everything into your Supabase database.
  5. Writes a "crawl log" entry so you always know how much was collected
     on each run — your coverage record for research integrity.

It is designed to run automatically on a schedule via GitHub Actions,
but you can also run it manually:  python collector.py
"""

import os
import sys
import time
import re
from datetime import datetime, timezone

import requests
import feedparser
import trafilatura
from dateutil import parser as dateparser

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION — add more newspapers here later (one block each).
# feed_urls can list several feeds per paper (e.g. category feeds).
# ─────────────────────────────────────────────────────────────────────
SOURCES = [
    {
        "id": "punch",
        "name": "The Punch",
        "feed_urls": [
            "https://punchng.com/feed/",
            "https://punchng.com/feed/?paged=2",  # one page back, as a safety net
        ],
    },
    {
        "id": "premiumtimes",
        "name": "Premium Times",
        "feed_urls": [
            "https://www.premiumtimesng.com/feed",
            "https://www.premiumtimesng.com/feed?paged=2",
        ],
    },
]

# Seconds to wait between article downloads (politeness — do not lower much)
FETCH_DELAY = 4

# Read the secret connection details from the environment (set in GitHub).
# .strip() removes any accidental spaces or line-breaks picked up when
# the secrets were copy-pasted — a very common, invisible setup snag.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
CONTACT = os.environ.get("CONTACT_EMAIL", "research-archive").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

HEADERS_DB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# An honest User-Agent identifying the crawler, as good practice requires.
HEADERS_WEB = {
    "User-Agent": f"NaijaPressResearchArchive/1.0 (+{CONTACT}; academic research archive)"
}


# ─────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────
def clean_url(url: str) -> str:
    """Remove tracking junk (?utm_...) and fragments so the same article
    always has exactly one address — this is how duplicates are prevented."""
    url = url.split("#")[0]
    url = re.sub(r"\?(utm_[^&]+&?)+$", "", url)
    return url.rstrip("/").strip()


def parse_date(value):
    """Turn any date text into a proper timestamp; return None if impossible."""
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
        if dt and dt.tzinfo is None:
            # Nigerian papers publish in WAT (UTC+1)
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def db_get_existing(urls):
    """Ask the database which of these URLs it already has."""
    existing = set()
    # Query in chunks to keep request URLs a reasonable length
    for i in range(0, len(urls), 40):
        chunk = urls[i : i + 40]
        quoted = ",".join(f'"{u}"' for u in chunk)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/articles",
            headers=HEADERS_DB,
            params={"select": "url", "url": f"in.({quoted})"},
            timeout=30,
        )
        r.raise_for_status()
        existing.update(row["url"] for row in r.json())
    return existing


def db_insert_article(record):
    """Save one article. If it slipped in twice, the database quietly ignores it."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/articles?on_conflict=url",
        headers={**HEADERS_DB, "Prefer": "resolution=ignore-duplicates"},
        json=record,
        timeout=30,
    )
    r.raise_for_status()


def db_log_run(source_id, discovered, new, inserted, errors, note=""):
    """Record what happened on this run — the coverage matrix for research."""
    requests.post(
        f"{SUPABASE_URL}/rest/v1/crawl_log",
        headers=HEADERS_DB,
        json={
            "source_id": source_id,
            "discovered": discovered,
            "new_articles": new,
            "inserted": inserted,
            "errors": errors,
            "notes": note,
        },
        timeout=30,
    ).raise_for_status()


# ─────────────────────────────────────────────────────────────────────
# The main collection routine
# ─────────────────────────────────────────────────────────────────────
def collect_source(source):
    print(f"\n=== {source['name']} ===")

    # 1) Discover article links from the feeds
    entries = {}  # url -> feed info
    feed_note = ""
    for feed_url in source["feed_urls"]:
        try:
            resp = requests.get(feed_url, headers=HEADERS_WEB, timeout=30)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for e in feed.entries:
                link = clean_url(getattr(e, "link", "") or "")
                if link:
                    entries[link] = {
                        "title": getattr(e, "title", None),
                        "published": parse_date(getattr(e, "published", None)),
                        "author": getattr(e, "author", None),
                        "section": (e.tags[0].term if getattr(e, "tags", None) else None),
                    }
        except requests.RequestException as exc:
            feed_note += f"feed failed: {feed_url} ({exc}); "
            print(f"  ! could not read feed {feed_url}: {exc}")

    discovered = len(entries)
    print(f"  feed(s) list {discovered} articles")

    # 2) Which ones are actually new?
    urls = list(entries.keys())
    existing = db_get_existing(urls) if urls else set()
    new_urls = [u for u in urls if u not in existing]
    print(f"  {len(new_urls)} are new (rest already stored)")

    # 3) Fetch and extract each new article, politely
    inserted, errors = 0, 0
    for url in new_urls:
        info = entries[url]
        try:
            page = requests.get(url, headers=HEADERS_WEB, timeout=30)
            page.raise_for_status()

            extracted = trafilatura.bare_extraction(
                page.text, url=url, with_metadata=True, favor_precision=True
            )

            body = (extracted.text if extracted else "") or ""
            meta_date = parse_date(getattr(extracted, "date", None)) if extracted else None

            record = {
                "source_id": source["id"],
                "url": url,
                "headline": (getattr(extracted, "title", None) if extracted else None)
                            or info["title"],
                "byline": (getattr(extracted, "author", None) if extracted else None)
                          or info["author"],
                # Prefer the date printed on the article page; fall back to the feed's.
                "published_at": meta_date or info["published"],
                "date_inferred": not bool(meta_date or info["published"]),
                "section": info["section"],
                "body_text": body,
                "word_count": len(body.split()),
                "language": "en",
                "ingest_mode": "daily_rss",
            }
            db_insert_article(record)
            inserted += 1
            print(f"  + saved: {record['headline'][:70] if record['headline'] else url}")
        except requests.RequestException as exc:
            errors += 1
            print(f"  ! failed: {url} ({exc})")

        time.sleep(FETCH_DELAY)  # politeness pause between page visits

    # 4) Write the coverage record
    db_log_run(source["id"], discovered, len(new_urls), inserted, errors, feed_note)
    print(f"  done: {inserted} saved, {errors} errors")
    return inserted, errors


def main():
    print(f"NaijaPress Collector — run started {datetime.now(timezone.utc).isoformat()}")
    total_saved, total_errors = 0, 0
    for source in SOURCES:
        saved, errs = collect_source(source)
        total_saved += saved
        total_errors += errs
    print(f"\nRun finished: {total_saved} articles saved, {total_errors} errors.")
    # Exit code 0 even with some errors — individual page failures are normal;
    # the crawl_log records them for review.


if __name__ == "__main__":
    main()
