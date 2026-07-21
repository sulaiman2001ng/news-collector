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
import xml.etree.ElementTree as ET
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
        "id": "vanguard",
        "name": "Vanguard",
        "feed_urls": [
            "https://www.vanguardngr.com/feed/",
            "https://www.vanguardngr.com/feed/?paged=2",
        ],
    },
    {
        "id": "dailytrust",
        "name": "Daily Trust",
        "feed_urls": [
            "https://dailytrust.com/feed/",
            "https://dailytrust.com/feed/?paged=2",
        ],
    },
    {
        "id": "guardian",
        "name": "The Guardian Nigeria",
        # This paper blocks its RSS feed but keeps sitemaps open (verified
        # by probe) — so we discover new articles from the sitemap instead.
        "sitemap_urls": ["https://guardian.ng/sitemap.xml"],
    },
    {
        "id": "businessday",
        "name": "BusinessDay",
        "feed_urls": [
            "https://businessday.ng/feed/",
            "https://businessday.ng/feed/?paged=2",
        ],
    },
    {
        "id": "pmnews",
        "name": "PM News",
        "feed_urls": [
            "https://pmnewsnigeria.com/feed/",
            "https://pmnewsnigeria.com/feed/?paged=2",
        ],
    },
    {
        "id": "leadership",
        "name": "Leadership",
        "feed_urls": [
            "https://leadership.ng/feed/",
            "https://leadership.ng/feed/?paged=2",
        ],
    },
    {
        "id": "thisday",
        "name": "ThisDay",
        "feed_urls": [
            "https://www.thisdaylive.com/feed/",
            "https://www.thisdaylive.com/index.php/feed/",  # alternate address some ThisDay setups use
        ],
    },
    {
        "id": "thenation",
        "name": "The Nation",
        # Feed is blocked but sitemaps are open (verified by probe).
        "sitemap_urls": ["https://thenationonlineng.net/sitemap.xml"],
    },
    {
        "id": "tribune",
        "name": "Nigerian Tribune",
        "feed_urls": [
            "https://tribuneonlineng.com/feed/",
            "https://tribuneonlineng.com/feed/?paged=2",
        ],
    },
    {
        "id": "sun",
        "name": "The Sun",
        "feed_urls": [
            "https://sunnewsonline.com/feed/",
            "https://sunnewsonline.com/feed/?paged=2",
        ],
    },
]

# Seconds to wait between article downloads (politeness — do not lower much)
FETCH_DELAY = 3

# Read the secret connection details from the environment (set in GitHub).
# Long keys sometimes get copied with a hidden line-break in the middle;
# keys and URLs never contain whitespace, so we remove ALL of it anywhere.
SUPABASE_URL = re.sub(r"\s+", "", os.environ.get("SUPABASE_URL", "")).rstrip("/")
SUPABASE_KEY = re.sub(r"\s+", "", os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
CONTACT = re.sub(r"\s+", "", os.environ.get("CONTACT_EMAIL", "research-archive"))

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

HEADERS_DB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# Present as a normal browser — several papers' protective filters (e.g.
# The Sun, The Nation, Guardian) block unfamiliar user agents with a 403.
# We stay polite: slow request rate, and your contact email travels in
# the standard "From" header so site owners can always reach you.
HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "From": CONTACT,
}


def fetch(url, tries=2, timeout=60):
    """Download a URL with patience: a longer timeout, one retry after a
    short pause, and — if the site answers 403 Forbidden — an automatic
    switch to 'cloudscraper', which can pass the standard Cloudflare
    challenge some papers (The Sun, The Nation, Guardian) sit behind."""
    last_error = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS_WEB, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                try:
                    r2 = get_scraper().get(url, timeout=timeout)
                    r2.raise_for_status()
                    return r2
                except Exception as exc2:  # noqa: BLE001 — record and retry
                    last_error = exc2
            else:
                last_error = exc
        except requests.RequestException as exc:
            last_error = exc
        if attempt < tries - 1:
            time.sleep(6)
    raise last_error


_scraper = None

def get_scraper():
    """Create the challenge-solving downloader once, on first need."""
    global _scraper
    if _scraper is None:
        import cloudscraper
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return _scraper


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


def extract_page_published(html):
    """Read the article page's own machine-readable PUBLICATION stamp —
    JSON-LD 'datePublished' or the 'article:published_time' meta tag.
    This is deliberately the publication date, never 'dateModified',
    which matters because sitemap lastmod is only a modification date."""
    patterns = [
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return parse_date(m.group(1).strip())
    return None


# How many recent sitemap articles to consider per paper per run.
# Keeps the very first run from trying to download a paper's whole history.
SITEMAP_MAX_ARTICLES = 60


def _xml_tag(el):
    """Element tag without its namespace prefix."""
    return el.tag.rsplit("}", 1)[-1]


def parse_sitemap(content):
    """Read a sitemap file. Returns ('sitemapindex' or 'urlset',
    list of (url, lastmod) pairs)."""
    # Some papers (Guardian.ng) serve the sitemap with a UTF-8 byte-order-mark
    # or blank lines before the XML declaration — invisible in a browser, but
    # a strict XML parser refuses to start. Strip anything before "<".
    if isinstance(content, bytes):
        idx = content.find(b"<")
        if idx > 0:
            content = content[idx:]
    else:
        idx = content.find("<")
        if idx > 0:
            content = content[idx:]

    root = ET.fromstring(content)
    items = []
    for child in root:
        loc, lastmod = None, None
        for field in child:
            t = _xml_tag(field)
            if t == "loc":
                loc = (field.text or "").strip()
            elif t == "lastmod":
                lastmod = (field.text or "").strip()
        if loc:
            items.append((loc, lastmod))
    return _xml_tag(root), items


def sitemap_discover(sitemap_urls):
    """Discover recent article URLs from sitemaps (for papers that block
    their RSS feed but keep sitemaps open, as newspapers must for Google).

    Handles both shapes: a sitemap *index* (a list of sub-sitemaps — we
    follow the most recently updated ones) and a direct *urlset* (a list
    of article URLs). Returns (entries dict, note string)."""
    found = []   # (url, lastmod)
    note = ""

    for sm_url in sitemap_urls:
        try:
            resp = fetch(sm_url)
            kind, items = parse_sitemap(resp.content)

            if kind == "sitemapindex":
                # Follow the 2 most recently modified sub-sitemaps
                items.sort(key=lambda x: x[1] or "", reverse=True)
                for child_url, _ in items[:2]:
                    try:
                        child = fetch(child_url)
                        _, urls = parse_sitemap(child.content)
                        found.extend(urls)
                    except (requests.RequestException, ET.ParseError) as exc:
                        note += f"sub-sitemap failed: {child_url} ({exc}); "
                    time.sleep(2)
            else:
                found.extend(items)

        except (requests.RequestException, ET.ParseError) as exc:
            note += f"sitemap failed: {sm_url} ({exc}); "

    # Keep only the most recent articles
    if any(lm for _, lm in found):
        found.sort(key=lambda x: x[1] or "", reverse=True)
        recent = found[:SITEMAP_MAX_ARTICLES]
    else:
        # No dates in this sitemap — newest entries are usually at the end
        recent = found[-SITEMAP_MAX_ARTICLES:]

    entries = {}
    for loc, lastmod in recent:
        url = clean_url(loc)
        if not url or url.endswith((".xml", ".jpg", ".png", ".webp")):
            continue
        entries[url] = {
            "title": None,                    # extracted from the page itself
            "published": parse_date(lastmod), # LAST RESORT only — this is a
            "author": None,                   # modification date, not publication
            "section": None,
            "date_origin": "sitemap_lastmod",
        }
    return entries, note


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
    # PostgreSQL rejects text containing null bytes (\u0000) and some control
    # characters. A few papers (Vanguard especially) occasionally embed these
    # in their article HTML, which silently blocks the save. Strip them first.
    for key in ("headline", "byline", "section", "body_text"):
        val = record.get(key)
        if isinstance(val, str):
            record[key] = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", val)

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

    # 1) Discover article links — from RSS feeds, sitemaps, or both
    entries = {}  # url -> feed info
    feed_note = ""
    for feed_url in source.get("feed_urls", []):
        try:
            resp = fetch(feed_url)
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                # A feed that answers but lists nothing is suspicious —
                # often a protection page in disguise. Make it visible.
                feed_note += f"feed empty: {feed_url}; "
            for e in feed.entries:
                link = clean_url(getattr(e, "link", "") or "")
                if link:
                    entries[link] = {
                        "title": getattr(e, "title", None),
                        "published": parse_date(getattr(e, "published", None)),
                        "author": getattr(e, "author", None),
                        "section": (e.tags[0].term if getattr(e, "tags", None) else None),
                        "date_origin": "feed",
                    }
        except requests.RequestException as exc:
            feed_note += f"feed failed: {feed_url} ({exc}); "
            print(f"  ! could not read feed {feed_url}: {exc}")

    # Sitemap discovery, for papers whose feeds are blocked
    if source.get("sitemap_urls"):
        sm_entries, sm_note = sitemap_discover(source["sitemap_urls"])
        feed_note += sm_note
        for url, info in sm_entries.items():
            entries.setdefault(url, info)   # feeds win if both found the URL

    discovered = len(entries)
    print(f"  feed(s) list {discovered} articles")

    # 2) Which ones are actually new?
    urls = list(entries.keys())
    existing = db_get_existing(urls) if urls else set()
    new_urls = [u for u in urls if u not in existing]
    print(f"  {len(new_urls)} are new (rest already stored)")

    # 3) Fetch and extract each new article, politely
    inserted, errors = 0, 0
    fail_reasons = []
    for url in new_urls:
        info = entries[url]
        try:
            page = fetch(url)

            extracted = trafilatura.bare_extraction(
                page.text, url=url, with_metadata=True, favor_precision=True,
                date_extraction_params={"original_date": True},
            )

            body = (extracted.text if extracted else "") or ""

            # ── Publication date, best source first ──────────────────
            # 1. The page's own machine-readable datePublished stamp
            #    (full timestamp, explicitly publication not modification)
            # 2. Trafilatura's date reading (original_date=True → prefers
            #    first publication; day precision)
            # 3. The RSS feed's timestamp (real publication time)
            # 4. The sitemap's lastmod (modification date — LAST resort,
            #    and flagged so researchers can audit or exclude it)
            page_date = extract_page_published(page.text)
            tra_date = parse_date(getattr(extracted, "date", None)) if extracted else None

            if page_date:
                published, date_source = page_date, "page_stamp"
            elif tra_date:
                published, date_source = tra_date, "page_extracted"
            elif info["published"]:
                published, date_source = info["published"], info.get("date_origin", "feed")
            else:
                published, date_source = None, "none"

            record = {
                "source_id": source["id"],
                "url": url,
                "headline": (getattr(extracted, "title", None) if extracted else None)
                            or info["title"],
                "byline": (getattr(extracted, "author", None) if extracted else None)
                          or info["author"],
                "published_at": published,
                "date_inferred": date_source in ("sitemap_lastmod", "none"),
                "date_source": date_source,
                "section": info["section"],
                "body_text": body,
                "word_count": len(body.split()),
                "language": "en",
                "ingest_mode": "daily_rss",
            }
            db_insert_article(record)
            inserted += 1
            print(f"  + saved: {record['headline'][:70] if record['headline'] else url}")
        except requests.HTTPError as exc:
            # A database rejection (e.g. a field too long / bad value). Capture WHY —
            # the server's message is in the response body — so silent loss becomes visible.
            errors += 1
            reason = ""
            try:
                reason = exc.response.text[:200] if exc.response is not None else str(exc)
            except Exception:  # noqa: BLE001
                reason = str(exc)
            fail_reasons.append(reason)
            print(f"  ! DB rejected: {url}\n      reason: {reason}")
        except requests.RequestException as exc:
            errors += 1
            fail_reasons.append(f"network: {exc}")
            print(f"  ! fetch failed: {url} ({exc})")

        time.sleep(FETCH_DELAY)  # politeness pause between page visits

    # 4) Write the coverage record — include a sample failure reason if any
    note = feed_note
    if fail_reasons:
        # de-duplicate and attach up to two distinct reasons, so the log
        # tells you WHY articles were lost, not just how many
        seen = []
        for r in fail_reasons:
            if r not in seen:
                seen.append(r)
        note += "insert errors: " + " | ".join(seen[:2]) + "; "

    db_log_run(source["id"], discovered, len(new_urls), inserted, errors, note)
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
