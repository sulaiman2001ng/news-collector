"""
probe2.py — tests the remaining candidate routes to the two blocked
RSS feeds, so we choose with evidence rather than guesswork:

  1. curl_cffi   — impersonates a real Chrome browser at the network-
                   signature (TLS) level, deeper than cloudscraper.
  2. rss2json    — a feed-proxy service: THEIR servers fetch the feed.
  3. Open RSS    — a free nonprofit feed proxy, same idea.

Run manually via the probe2 workflow, then paste the output back.
"""

import time
import requests
import feedparser

FEEDS = {
    "guardian":  "https://guardian.ng/feed/",
    "thenation": "https://thenationonlineng.net/feed/",
}


def show(label, status, extra=""):
    print(f"  {label:22}{status:>10}  {extra}")


def test_curl_cffi(feed_url):
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return ("no-lib", "curl_cffi not installed")
    try:
        r = creq.get(feed_url, impersonate="chrome", timeout=45)
        if r.status_code == 200:
            f = feedparser.parse(r.content)
            return (str(r.status_code), f"{len(f.entries)} feed items parsed")
        return (str(r.status_code), f"{len(r.content)//1024}KB body")
    except Exception as e:  # noqa: BLE001
        return ("error", type(e).__name__)


def test_rss2json(feed_url):
    try:
        r = requests.get(
            "https://api.rss2json.com/v1/api.json",
            params={"rss_url": feed_url}, timeout=45)
        j = r.json()
        if j.get("status") == "ok":
            return (str(r.status_code), f"{len(j.get('items', []))} items via rss2json")
        return (str(r.status_code), f"service says: {str(j.get('message'))[:60]}")
    except Exception as e:  # noqa: BLE001
        return ("error", type(e).__name__)


def test_openrss(feed_url):
    # Open RSS syntax: openrss.org/<site-and-path without scheme>
    bare = feed_url.replace("https://", "").replace("http://", "").rstrip("/")
    try:
        r = requests.get(f"https://openrss.org/{bare}", timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            f = feedparser.parse(r.content)
            return (str(r.status_code), f"{len(f.entries)} items via Open RSS")
        return (str(r.status_code), f"{len(r.content)//1024}KB body")
    except Exception as e:  # noqa: BLE001
        return ("error", type(e).__name__)


def main():
    print("PROBE 2 — alternative routes to the blocked feeds\n")
    for pid, feed in FEEDS.items():
        print(f"{'='*58}\n  {pid.upper()}  ({feed})\n{'='*58}")
        s, x = test_curl_cffi(feed);  show("curl_cffi (TLS)", s, x); time.sleep(3)
        s, x = test_rss2json(feed);   show("rss2json proxy", s, x);  time.sleep(3)
        s, x = test_openrss(feed);    show("Open RSS proxy", s, x);  time.sleep(3)
        print()
    print("Done. Paste this whole output back into the chat.")


if __name__ == "__main__":
    main()
