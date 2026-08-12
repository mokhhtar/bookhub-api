"""
scripts/refresh_quote_pages.py — republish committed book pages whose
"Notable Quotes" were produced by a resolver we have since corrected.

    python scripts/refresh_quote_pages.py --dry-run     # list, touch nothing
    python scripts/refresh_quote_pages.py --limit 1     # canary FIRST
    python scripts/refresh_quote_pages.py               # the rest

WHY. The quotes resolver has been wrong three times, each in a way no reader
could have spotted, because every version shipped real quotations verbatim
from the page it cites — just not the book's page:

  v7  `redirects=1` followed blindly. "Kidnapped" redirects to "Crime", so
      Stevenson's novel carried five quotations about crime as a topic;
      eight more redirect to their AUTHOR.
  v10 The top-level bullet on a TRANSLATED work's page is the original
      language, and the English translation sits one level below it. The
      Little Prince, Les Misérables and Candide shipped in French.
  v11 A title resolves to whatever page bears it — a theme, an adaptation, a
      musical. Jane Eyre carried lyrics from "Jane Eyre: The Musical";
      Persuasion carried a modern political quotation about persuasion.

The resolver is fixed and the cache heals itself on read. Committed pages do
neither — [[Storage Layers]] layer 4 is permanent and never self-repairs — so
this script exists to push the fix into the files.

HOW IT FINDS THEM. By the payload's own version, not by reading the quotes.
That is the whole lesson of the three incidents above: the text is always
well-formed and plausible, so no inspection of it can say whose it is. Any
page whose stored `quotes.v` is below tools/summary.py's _WQ_PAYLOAD_VERSION
was written by a resolver we have since corrected, which is the same
invariant github_publisher._page_is_stale now uses. The test needs no edit
the next time this happens — only the version bump the fix carries anyway.

Publishing is a SIDE EFFECT of a normal /summary call (publish_book runs as a
FastAPI BackgroundTask), so this is a paced crawl of the live API. It creates
nothing itself and needs no credentials. The deployed process must already be
serving the current _WQ_PAYLOAD_VERSION, or every call rewrites the page with
the same stale payload; the run aborts and says so rather than reporting a
success it did not achieve.
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

from tools.summary import _WQ_PAYLOAD_VERSION  # noqa: E402  the resolver's own stamp

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
            out["quotes_v"] = int(quotes.get("v", 0) or 0)
    return out


def find_affected() -> list[dict]:
    """Pages whose baked-in quotes predate the current resolver."""
    affected = []
    for name in sorted(os.listdir(BOOKS)):
        if not name.endswith(".md"):
            continue
        fm = _front_matter(os.path.join(BOOKS, name))
        if not (fm.get("wq_page") and fm.get("title")):
            continue
        if fm.get("quotes_v", 0) >= _WQ_PAYLOAD_VERSION:
            continue
        fm["file"] = name
        affected.append(fm)
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
    if quotes.get("v", 0) < _WQ_PAYLOAD_VERSION:
        return "STALE v%s — is the deploy live?" % quotes.get("v")
    return "requoted from " + (quotes.get("source_url", "").rsplit("/", 1)[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list the affected pages, change nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N pages (use 1 as a canary)")
    args = ap.parse_args()

    affected = find_affected()
    print(f"{len(affected)} committed page(s) hold quotes below "
          f"v{_WQ_PAYLOAD_VERSION}:\n")
    for p in affected:
        print(f"  {p['file'][:-3]:<40} v{p['quotes_v']}  from '{p['wq_page']}'")
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
                print(f"\nAborting: the deployed process is not serving "
                      f"_WQ_PAYLOAD_VERSION {_WQ_PAYLOAD_VERSION} yet, so every "
                      f"call would rewrite the page with the same stale payload. "
                      f"Nothing further was called.")
                return 1
            if i < len(todo):
                time.sleep(PACE_SECONDS)

    print("\nDone. The pages are rewritten by a BackgroundTask, so give the "
          "commits a minute, then `git pull` in the site repo and read the diff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
