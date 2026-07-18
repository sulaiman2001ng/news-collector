"""
probe.py — one-off diagnostic for the two blocked newspapers.

Runs from GitHub Actions and tries EVERY plausible route into
Guardian Nigeria and The Nation:

  1. The RSS feed         (what's failing now)
  2. The homepage
  3. A real article page  (maybe only the feed is guarded)
  4. The sitemap files    (papers keep these open for Google)
  5. Bing News RSS        (discovery via Microsoft's servers — never blocked)
  6. Google News RSS      (discovery via Google's servers — never blocked)

Each route is tried two ways: as a normal browser request, and with
the Cloudflare challenge-solver. The output table tells us exactly
which door is open, and we build the fix on that door.
"""

import requests, feedparser, re, sys, time

try:
    import cloudscraper
    SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False})
except Exception as e:  # noqa: BLE001
    SCRAPER = None
    print(f"(cloudscraper unavailable: {e})")

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PAPERS = {
    "guardian": {
        "site": "guardian.ng",
        "tests": [
            ("feed",          "https://guardian.ng/feed/"),
            ("homepage",      "https://guardian.ng/"),
            ("sitemap-index", "https://guardian.ng/sitemap.xml"),
            ("wp-sitemap",    "https://guardian.ng/wp-sitemap.xml"),
            ("news-sitemap",  "https://guardian.ng/news-sitemap.xml"),
        ],
    },
    "thenation": {
        "site": "thenationonlineng.net",
        "tests": [
            ("feed",          "https://thenationonlineng.net/feed/"),
            ("homepage",      "https://thenationonlineng.net/"),
            ("sitemap-index", "https://thenationonlineng.net/sitemap.xml"),
            ("wp-sitemap",    "https://thenationonlineng.net/wp-sitemap.xml"),
            ("news-sitemap",  "https://thenationonlineng.net/news-sitemap.xml"),
        ],
    },
}


def attempt(label, url, kind):
    """Try one URL one way; return a short status string."""
    try:
        if kind == "plain":
            r = requests.get(url, headers=BROWSER, timeout=45)
        else:
            if SCRAPER is None:
                return "n/a"
            r = SCRAPER.get(url, timeout=45)
        size = len(r.content)
        note = ""
        body = r.text[:400].lower()
        if r.status_code == 200 and ("just a moment" in body or "cf-challenge" in body):
            note = " (challenge page!)"
        return f"{r.status_code} ({size//1024}KB){note}"
    except requests.RequestException as e:
        return type(e).__name__
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}"


def probe_paper(pid, cfg):
    print(f"\n{'='*62}\n  {pid.upper()}  ({cfg['site']})\n{'='*62}")
    print(f"  {'route':16}{'plain request':>22}{'cloudscraper':>22}")
    print("  " + "-" * 58)

    article_url = None
    for label, url in cfg["tests"]:
        a = attempt(label, url, "plain")
        time.sleep(2)
        b = attempt(label, url, "cs")
        print(f"  {label:16}{a:>22}{b:>22}")
        time.sleep(2)

    # ── Bing News discovery ──
    bing = f"https://www.bing.com/news/search?q=site%3A{cfg['site']}&format=rss"
    try:
        r = requests.get(bing, headers=BROWSER, timeout=45)
        f = feedparser.parse(r.content)
        links = [e.link for e in f.entries if cfg["site"] in getattr(e, "link", "")]
        print(f"  bing-news-rss   {r.status_code:>6} → {len(f.entries)} items, "
              f"{len(links)} direct links")
        if links:
            article_url = links[0]
    except Exception as e:  # noqa: BLE001
        print(f"  bing-news-rss   failed: {type(e).__name__}")
    time.sleep(2)

    # ── Google News discovery ──
    goog = (f"https://news.google.com/rss/search?q=site:{cfg['site']}"
            f"&hl=en-NG&gl=NG&ceid=NG:en")
    try:
        r = requests.get(goog, headers=BROWSER, timeout=45)
        f = feedparser.parse(r.content)
        print(f"  google-news-rss {r.status_code:>6} → {len(f.entries)} items "
              f"(links are Google-encoded)")
    except Exception as e:  # noqa: BLE001
        print(f"  google-news-rss failed: {type(e).__name__}")
    time.sleep(2)

    # ── If Bing gave us a real article URL, test fetching THAT page ──
    if article_url:
        print(f"\n  testing a real article page from Bing discovery:")
        print(f"    {article_url[:70]}")
        a = attempt("article", article_url, "plain")
        time.sleep(2)
        b = attempt("article", article_url, "cs")
        print(f"  {'article-page':16}{a:>22}{b:>22}")
    else:
        print("\n  (no direct article link found via Bing to test)")


def main():
    print("PROBE — which doors are open?\n(200 = open, 403 = blocked, "
          "'challenge page' = pretend-open)")
    for pid, cfg in PAPERS.items():
        probe_paper(pid, cfg)
    print(f"\n{'='*62}\nDone. Paste this whole output back into the chat.")


if __name__ == "__main__":
    main()
