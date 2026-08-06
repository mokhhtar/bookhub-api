"""
scripts/refresh_quote_pages.py — republish the committed book pages whose
"Notable Quotes" came from a Wikiquote REDIRECT.

    python scripts/refresh_quote_pages.py --dry-run     # list, touch nothing
    python scripts/refresh_quote_pages.py --limit 1     # canary FIRST
    python scripts/refresh_quote_pages.py               # the rest

WHY. `redirects=1` on the parse call was followed blindly, so a title that
redirects elsewhere on Wikiquote published THAT page's quotes. "Kidnapped"
redirects to "Crime", so Stevenson's novel shipped five quotations about
crime as a topic; eight more books redirect to their AUTHOR, which is how
"Around the World in Eighty Days" and "The Mysterious Island" ended up
displaying the same five quotes as each other, in French.

The resolver is fixed and the cache heals itself on read. Committed pages do
neither — [[Storage Layers]] layer 4 is permanent and never self-repairs — so
this script exists to push the fix into the files.

HOW IT FINDS THEM. Not by reading the quotes: they are ordinary, well-formed
quotations that happen to belong to another article, and no inspection of the
text could ever say so. That was the whole trap. It asks Wikiquote instead —
`action=query&redirects=1` reports where each stored source_url actually
lands — and keeps the page only when the landing title disagrees with the
book title under the same word-level test the resolver now uses.

Publishing is a SIDE EFFECT of a normal /summary call (publish_book runs as a
FastAPI BackgroundTask), so this is a paced crawl of the live API. It creates
nothing itself and needs no credentials. PUBLISH_CONTENT_VERSION must already
be 7 on the deployed process, or every call will no-op and the report will
say so rather than pretending it worked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.summary import _wq_titles_agree  # noqa: E402  the resolver's own test

API = os.environ.get("LITHECA_API", "https://bookhub-api-hnv7.onrender.com")
SITE = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "bookhub"))
BOOKS = os.path.join(SITE, "_books")
WQ_API = "https://en.wikiquote.org/w/api.php"
UA = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

PACE_SECONDS = 4.0      # Render free tier; the crawl is not in a hurry.
REQUEST_TIMEOUT = 180.0  # a cold instance takes 30-60s to wake.


def _front_matter(path: str) -> dict:
    """title/author/content_version + the stored Wikiquote page, or {}."""
    text = open(path, encoding="utf-8").read()
    out = {}
    for field in ("title", "author"):
        m = re.search(rf'^{field}: "(.*)"\s*$', text, re.M)
        if m:
            out[field] = m.group(1)
    m = re.search(r"^content_version: (\d+)\s*$", text, re.M)
    out["content_version"] = int(m.group(1)) if m else 0
    m = re.search(r"^quotes: (\{.*\})\s*$", text, re.M)
    if m:
        try:
            quotes = json.loads(m.group(1))
        except Exception:
            return out
        url = quotes.get("source_url") or ""
        if "wikiquote" in url:
            out["wq_page"] = urllib.parse.unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
            out["quote_count"] = len(quotes.get("texts") or [])
            out["first_quote"] = (quotes.get("texts") or [""])[0]
    return out


def find_affected() -> list[dict]:
    """Pages whose stored Wikiquote title redirects somewhere that isn't the book."""
    pages = []
    for name in sorted(os.listdir(BOOKS)):
        if not name.endswith(".md"):
            continue
        fm = _front_matter(os.path.join(BOOKS, name))
        if fm.get("wq_page") and fm.get("title"):
            fm["file"] = name
            pages.append(fm)

    # One batched lookup per 20 titles; the API reports every redirect it
    # followed, so a title absent from that list is not a redirect at all.
    landed: dict[str, str] = {}
    with httpx.Client(headers=UA, timeout=30.0) as client:
        titles = [p["wq_page"] for p in pages]
        for i in range(0, len(titles), 20):
            r = client.get(WQ_API, params={
                "action": "query", "redirects": 1, "format": "json",
                "titles": "|".join(titles[i:i + 20]),
            })
            for hop in (r.json().get("query", {}).get("redirects") or []):
                landed[hop["from"]] = hop["to"]
            time.sleep(0.3)

    affected = []
    for p in pages:
        target = landed.get(p["wq_page"])
        if target and not _wq_titles_agree(p["title"], target):
            p["redirects_to"] = target
            affected.append(p)
    return affected


def refresh(page: dict, client: httpx.Client) -> str:
    """POST /summary so publish_book re-runs. Returns a one-word outcome."""
    try:
        r = client.post(f"{API}/summary", json={
            "title": page["title"], "author": page.get("author") or "",
        }, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        return f"error ({type(e).__name__})"
    if r.status_code != 200:
        return f"http {r.status_code}"
    try:
        quotes = r.json().get("quotes")
    except Exception:
        return "unparseable"
    if not quotes:
        return "cleared"                      # the honest outcome for these
    if quotes.get("v", 0) < 7:
        return "STALE v%s — is the deploy live?" % quotes.get("v")
    return "requoted from " + (quotes.get("source_url", "").rsplit("/", 1)[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list the affected pages, change nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N pages (use 1 as a canary)")
    args = ap.parse_args()

    affected = find_affected()
    print(f"{len(affected)} committed page(s) carry quotes from a redirect:\n")
    for p in affected:
        print(f"  {p['file'][:-3]:<40} {p['wq_page']} -> {p['redirects_to']}")
        print(f"      {p['quote_count']} quote(s), first: {p['first_quote'][:70]}")
    if not affected:
        return 0
    if args.dry_run:
        print("\n--dry-run: nothing was called.")
        return 0

    todo = affected[:args.limit] if args.limit else affected
    print(f"\nRefreshing {len(todo)} page(s) via {API}, {PACE_SECONDS}s apart.\n")
    with httpx.Client() as client:
        for i, p in enumerate(todo, 1):
            outcome = refresh(p, client)
            print(f"  [{i}/{len(todo)}] {p['file'][:-3]:<40} {outcome}")
            if outcome.startswith("STALE"):
                print("\nAborting: the deployed process is not running "
                      "PUBLISH_CONTENT_VERSION 7 yet. Nothing further was called.")
                return 1
            if i < len(todo):
                time.sleep(PACE_SECONDS)

    print("\nDone. The pages are rewritten by a BackgroundTask, so give the "
          "commits a minute, then `git pull` in the site repo and read the diff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
