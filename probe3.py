"""
probe3.py — one-off diagnostic for The Nation.
Its sitemap has been returning the same 60 URLs for 4 days,
suggesting the current entry point is a stale or wrong section.

This probes every plausible sitemap door and reports on:
  - what each URL returns (size, content type)
  - if it's a sitemap index: what sub-sitemaps it lists
  - the freshest lastmod date it can see
so we can point the collector at the actively-updating door.
"""

import time
import requests
import xml.etree.ElementTree as ET


BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# All the sitemap entrypoints The Nation might expose
CANDIDATES = [
    "https://thenationonlineng.net/sitemap.xml",
    "https://thenationonlineng.net/sitemap_index.xml",
    "https://thenationonlineng.net/wp-sitemap.xml",
    "https://thenationonlineng.net/news-sitemap.xml",
    "https://thenationonlineng.net/post-sitemap.xml",
    "https://thenationonlineng.net/wp-sitemap-posts-post-1.xml",
    "https://thenationonlineng.net/robots.txt",  # often lists the real sitemap
]


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def clean_xml(content):
    """Strip BOM / leading whitespace before the < — same as the collector does."""
    if isinstance(content, bytes):
        idx = content.find(b"<")
        if idx > 0:
            content = content[idx:]
    return content


def inspect(url):
    print(f"\n{'-'*66}\n{url}")
    try:
        r = requests.get(url, headers=BROWSER, timeout=45)
    except requests.RequestException as e:
        print(f"  network error: {type(e).__name__}: {e}")
        return

    print(f"  HTTP {r.status_code}  ({len(r.content)//1024}KB)  "
          f"content-type: {r.headers.get('content-type','?')}")

    if r.status_code != 200 or not r.content:
        return

    # robots.txt: show any Sitemap: lines
    if url.endswith("robots.txt"):
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap"):
                print(f"  {line}")
        return

    try:
        root = ET.fromstring(clean_xml(r.content))
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        print(f"  first 200 chars: {r.text[:200]!r}")
        return

    kind = _tag(root)
    print(f"  parsed OK; root = <{kind}>")

    # Collect entries with their lastmod
    entries = []
    for child in root:
        loc = lastmod = None
        for f in child:
            if _tag(f) == "loc":
                loc = (f.text or "").strip()
            elif _tag(f) == "lastmod":
                lastmod = (f.text or "").strip()
        if loc:
            entries.append((loc, lastmod))

    print(f"  entries: {len(entries)}")

    if kind == "sitemapindex":
        # Show the top 6 by lastmod so we can spot which sub-sitemap is fresh
        with_mod = [e for e in entries if e[1]]
        with_mod.sort(key=lambda x: x[1] or "", reverse=True)
        print("  most recently modified sub-sitemaps:")
        for loc, lm in with_mod[:6]:
            print(f"    {lm}   {loc}")
        without = [e for e in entries if not e[1]]
        if without:
            print(f"  (also {len(without)} sub-sitemaps without lastmod)")
    elif kind == "urlset":
        # Show freshest and oldest lastmod dates in this urlset
        dates = sorted([e[1] for e in entries if e[1]])
        if dates:
            print(f"  lastmod range: {dates[0]}  →  {dates[-1]}")
        # Show a few sample URLs so we can see if they're article URLs at all
        print("  sample URLs:")
        for loc, lm in entries[:3]:
            print(f"    {lm or '(no lastmod)'}   {loc}")


def main():
    print("PROBE 3 — mapping The Nation's sitemap landscape\n"
          "(looking for where fresh articles actually live)")
    for url in CANDIDATES:
        inspect(url)
        time.sleep(2)
    print("\nDone. Paste this whole output back into the chat.")


if __name__ == "__main__":
    main()
