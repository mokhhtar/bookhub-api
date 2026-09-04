#!/usr/bin/env python3
"""Walk the published book pages and re-view the ones missing data.

WHY THIS EXISTS. A published page is only rewritten when someone VIEWS the
book — publish_book runs as a background task off a /summary read. That was
fine while a page could only be written by a fresh summary, but the resolver
work of 2026-09-03/04 means many committed pages are now repairable: measured
then, 164 of 200 carried no chapters, 134 no characters, 116 neither. Those
are exactly the pages nobody visits — a book with no characters and no
chapters is the thin page that never earned traffic — so left alone they
would stay broken precisely because they are broken.

WHAT IT DOES. Reads the page's own front matter for the request that built
it (title, author, google_id) and re-issues that request. Matching the
original request matters: /summary's cache key is built from the request
parameters, so title+author+google_id lands on the ENTRY THE READER CREATED
and heals it, instead of missing and spending a Gemini call regenerating a
summary that was already fine. The heal fills chapters/characters, and the
publish task then rewrites the page — the whole chain this walks.

BATCH SIZE IS NOT ARBITRARY. /summary's publish path is capped at
SUMMARY_PUBLISH_DAILY (8) per client IP per day, counted on every view and
not just on every write, and a job's requests all come from one runner IP.
Asking for more than that would spend the extra requests healing caches
whose pages then cannot be written — work that helps nobody, since these
books have no readers to trigger the write later. So the default batch is
that cap, and a full pass over the backlog takes about three weeks. Raise
SUMMARY_PUBLISH_DAILY on Render if that is too slow; do not raise it here.

The window rotates by day-of-year rather than always taking the first N, so
a page that cannot be repaired (no wiki, no Wikidata characters — a real and
permanent outcome for plenty of books) is retried occasionally instead of
sitting at the front of the queue blocking everything behind it forever.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("BOOKHUB_API", "https://bookhub-api-hnv7.onrender.com")
REPO = os.environ.get("BOOKHUB_SITE_REPO", "mokhhtar/bookhub")
BRANCH = os.environ.get("BOOKHUB_SITE_BRANCH", "main")
UA = {"User-Agent": "BookHub-page-refresh/1.0"}

_FM = {name: re.compile(rf"^{name}:\s*(.*)$", re.MULTILINE)
       for name in ("title", "author", "google_id", "chapters", "characters")}


def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def list_pages():
    """Every _books/*.md slug on the site, sorted for a stable window."""
    url = f"https://api.github.com/repos/{REPO}/contents/_books?ref={BRANCH}"
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")          # optional: lifts 60/hr to 5000
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    data = json.loads(_get(url, headers=headers))
    return sorted(e["name"][:-3] for e in data
                  if e.get("type") == "file" and e["name"].endswith(".md"))


def page_fields(slug):
    """(title, author, google_id, needs_refresh) from a page's front matter."""
    raw = _get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/_books/"
               f"{urllib.parse.quote(slug)}.md")
    out = {}
    for name, rx in _FM.items():
        m = rx.search(raw)
        out[name] = m.group(1).strip() if m else ""

    def _empty(field):
        v = out.get(field, "")
        return v in ("", "[]", "null")

    title = out["title"].strip('"')
    author = out["author"].strip('"')
    gid = out["google_id"].strip('"')
    return title, author, gid, (_empty("chapters") or _empty("characters"))


def refresh(title, author, gid, timeout=300):
    """Re-issue the request that built this page. Returns a short report."""
    payload = {"title": title, "author": author, "depth": "quick", "language": "en"}
    if gid:
        payload["google_id"] = gid
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{API}/summary", data=body, headers={
        **UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", errors="replace"))
    return (len(d.get("chapters") or []), len(d.get("characters") or []),
            bool(d.get("found")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=8,
                    help="pages per run; see the module docstring before raising")
    ap.add_argument("--offset", type=int, default=None,
                    help="override the rotating window start")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slugs = list_pages()
    print(f"{len(slugs)} published book pages")

    needy = []
    for s in slugs:
        try:
            title, author, gid, needs = page_fields(s)
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! could not read {s}: {type(e).__name__}")
            continue
        if needs and title:
            needy.append((s, title, author, gid))
    print(f"{len(needy)} are missing chapters and/or characters")
    if not needy:
        return 0

    if args.offset is not None:
        start = args.offset % len(needy)
    else:
        doy = datetime.now(timezone.utc).timetuple().tm_yday
        start = (doy * args.batch) % len(needy)
    window = [needy[(start + i) % len(needy)] for i in range(min(args.batch, len(needy)))]
    print(f"refreshing {len(window)} starting at {start}"
          f"{' (dry run)' if args.dry_run else ''}\n")

    for slug, title, author, gid in window:
        if args.dry_run:
            print(f"  would refresh {slug:44} {title[:34]!r}")
            continue
        try:
            ch, ca, found = refresh(title, author, gid)
            print(f"  {slug:44} chapters={ch:3} characters={ca:3}"
                  f"{'' if found else '  (not found)'}")
        except Exception as e:                                   # noqa: BLE001
            print(f"  {slug:44} FAILED {type(e).__name__}: {e}")
        time.sleep(2)      # be gentle with the free-tier instance
    return 0


if __name__ == "__main__":
    sys.exit(main())
