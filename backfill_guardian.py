"""
backfill_guardian.py — ONE-OFF recovery of Guardian Nigeria articles
missed between 7 July 2026 (start of collection) and 21 July 2026
(when the BOM fix brought Guardian online).

Reuses the daily collector's machinery for identical extraction,
date hierarchy, and politeness. Recovered rows are marked
ingest_mode='backfill_sitemap' so the corpus stays honest about
how each article arrived.

Run once via the backfill_guardian workflow, then delete both files.
"""

import time
import requests

from collector import (
    fetch, clean_url, parse_date, extract_page_published,
    parse_sitemap, db_get_existing, db_insert_article, db_log_run,
    FETCH_DELAY,
)
import trafilatura

SITEMAP_INDEX = "https://guardian.ng/sitemap.xml"
WINDOW_START = "2026-07-07"     # collection began
WINDOW_END   = "2026-07-22"     # day after the BOM fix landed
MAX_ARTICLES = 2000             # generous safety ceiling


def within_window(lastmod):
    return bool(lastmod) and WINDOW_START <= lastmod[:10] <= WINDOW_END


def discover():
    """All Guardian article URLs whose sitemap entry falls in the window."""
    print(f"Reading sitemap index: {SITEMAP_INDEX}")
    kind, items = parse_sitemap(fetch(SITEMAP_INDEX).content)

    candidates = []
    if kind == "sitemapindex":
        # Follow every sub-sitemap whose lastmod touches the window
        subs = [(u, lm) for u, lm in items if lm is None or lm[:10] >= WINDOW_START]
        print(f"  {len(items)} sub-sitemaps listed; {len(subs)} touch the window")
        for sub_url, _ in subs:
            try:
                _, urls = parse_sitemap(fetch(sub_url).content)
                hits = [(u, lm) for u, lm in urls if within_window(lm)]
                print(f"  {sub_url.rsplit('/',1)[-1]}: {len(hits)} articles in window")
                candidates.extend(hits)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! sub-sitemap failed: {sub_url} ({exc})")
            time.sleep(2)
    else:
        candidates = [(u, lm) for u, lm in items if within_window(lm)]

    urls = {}
    for loc, lastmod in candidates:
        u = clean_url(loc)
        if u and not u.endswith((".xml", ".jpg", ".png", ".webp")):
            urls[u] = lastmod
    return urls


def main():
    print("GUARDIAN BACKFILL — recovering the lost window "
          f"{WINDOW_START} → {WINDOW_END}\n")

    urls = discover()
    print(f"\nDiscovered {len(urls)} window articles in sitemaps")

    all_urls = list(urls.keys())[:MAX_ARTICLES]
    existing = db_get_existing(all_urls)
    todo = [u for u in all_urls if u not in existing]
    print(f"Already safely stored: {len(existing)}")
    print(f"To recover now:        {len(todo)}\n")

    inserted, errors = 0, 0
    reasons = []
    for i, url in enumerate(todo, 1):
        try:
            page = fetch(url)
            ex = trafilatura.bare_extraction(
                page.text, url=url, with_metadata=True, favor_precision=True,
                date_extraction_params={"original_date": True},
            )
            body = (ex.text if ex else "") or ""
            page_date = extract_page_published(page.text)
            tra_date = parse_date(getattr(ex, "date", None)) if ex else None

            if page_date:
                published, date_source = page_date, "page_stamp"
            elif tra_date:
                published, date_source = tra_date, "page_extracted"
            else:
                published, date_source = parse_date(urls.get(url)), "sitemap_lastmod"

            db_insert_article({
                "source_id": "guardian",
                "url": url,
                "headline": getattr(ex, "title", None) if ex else None,
                "byline": getattr(ex, "author", None) if ex else None,
                "published_at": published,
                "date_inferred": date_source == "sitemap_lastmod",
                "date_source": date_source,
                "section": None,
                "body_text": body,
                "word_count": len(body.split()),
                "language": "en",
                "ingest_mode": "backfill_sitemap",
            })
            inserted += 1
            if i % 25 == 0:
                print(f"  {i}/{len(todo)} … {inserted} recovered so far")
        except requests.HTTPError as exc:
            errors += 1
            try:
                reasons.append(exc.response.text[:150] if exc.response is not None else str(exc))
            except Exception:  # noqa: BLE001
                reasons.append(str(exc))
        except requests.RequestException as exc:
            errors += 1
            reasons.append(f"network: {exc}")
        time.sleep(FETCH_DELAY)

    note = "guardian backfill; "
    if reasons:
        uniq = []
        for r in reasons:
            if r not in uniq:
                uniq.append(r)
        note += "errors: " + " | ".join(uniq[:2]) + "; "

    db_log_run("guardian", len(urls), len(todo), inserted, errors, note)
    print(f"\nBACKFILL DONE: {inserted} articles recovered, {errors} errors.")
    if errors:
        print("Sample reasons:", " | ".join(reasons[:2]))


if __name__ == "__main__":
    main()
