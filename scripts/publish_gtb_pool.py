"""
scripts/publish_gtb_pool.py — publishes a static page for every book in the
"Guess the Book" pool, so the game's reveal link is instant.

    python scripts/publish_gtb_pool.py --limit 1     # canary FIRST
    python scripts/publish_gtb_pool.py               # the rest

WHY. The reveal link after a solved puzzle points at /summary/<slug>/ when a
static page exists and at the dynamic summarizer otherwise. Today only 2 of
48 pool books have a page, so 46 puzzles end by sending a player to Render —
which sleeps after 15 minutes and takes 30-60s to wake. That is the worst
possible moment to make someone wait: they just won and want to read.

Publishing is a SIDE EFFECT of a normal /summary call (publish_book runs as a
FastAPI BackgroundTask), so this script is just a paced warm-up crawl of the
live API. It creates nothing by itself and needs no special credentials.

READ THIS BEFORE RUNNING — the indexing hazard:
Every pool book is a Project Gutenberg book by construction, and the v3 rule
in github_publisher grants Gutenberg pages `noindex:false` the moment they
are created. Publishing 48 of them with that rule active would put 48 new
pages into Google in one afternoon, which is the opposite of the current
pre-launch stance. `DEFER_INDEXING=true` must therefore be live on Render
BEFORE this runs.

So the script verifies rather than trusts: after each publish it fetches the
committed file from GitHub and aborts the whole batch the moment one comes
back indexable. Run --limit 1 first and read the result; that single canary
is what tells you the env var actually reached the deployed process.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slug import book_slug  # noqa: E402
from make_gtb_puzzles import litheca_url  # noqa: E402

# The published site, for the title+author fallback lookup.
SITE_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "bookhub"))
from tools.gtb_pool import GTB_POOL  # noqa: E402

API = os.environ.get("LITHECA_API", "https://bookhub-api-hnv7.onrender.com")
RAW = "https://raw.githubusercontent.com/mokhhtar/bookhub/main/_books"
UA = {"User-Agent": "Litheca/1.0 (mokhhtar@github.com)"}
# Publishing happens in a BackgroundTask after the response is returned, and
# GitHub's raw CDN lags its own API by a few seconds.
PUBLISH_SETTLE_SECONDS = 25


def wake_api() -> bool:
    """Render sleeps; the first call after that pays 30-60s. Wake it once here
    rather than letting the first book eat a timeout."""
    print("Waking the API…", end=" ", flush=True)
    try:
        r = httpx.get(f"{API}/health", headers=UA, timeout=90.0)
        print(f"{r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"failed: {e}")
        return False


def summarize(title: str, author: str) -> tuple[bool, str]:
    try:
        r = httpx.post(f"{API}/summary", headers=UA, timeout=180.0,
                       json={"title": title, "author": author,
                             "depth": "medium", "language": "en"})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        data = r.json()
        return True, data.get("slug") or book_slug(title)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _state_of(markdown: str) -> str:
    if re.search(r"^noindex:\s*false\s*$", markdown[:4000], re.MULTILINE):
        return "indexed"
    return "noindex"


def _fetch_md(slug: str) -> str | None:
    """Page source, "" for a real 404, None when the fetch itself failed.

    That third case matters. Collapsing a timeout into "absent" made the
    checker report two published books as missing on one run and two
    different ones on the next — noise that would eventually get the whole
    report ignored, or worse, get a page published twice."""
    for attempt in (1, 2):
        try:
            r = httpx.get(f"{RAW}/{slug}.md", headers=UA, timeout=30.0)
            if r.status_code == 404:
                return ""
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(1.5)
    return None


def slug_index_state(slug: str) -> tuple[bool, str]:
    """State of one known slug — used right after a publish, where the API has
    already told us the exact slug it wrote."""
    md = _fetch_md(slug)
    if md is None:
        return False, "unknown"
    if md == "":
        return False, "absent"
    return True, _state_of(md)


def page_index_state(entry) -> tuple[bool, str]:
    """→ (page exists, one of 'indexed' / 'noindex' / 'absent').

    Tries the slug the pool's title implies, then falls back to searching the
    published pages by title words + author surname. The publisher names a
    page after whatever Google Books resolved, so a slug guess misses often:
    The Jungle Book published as jungle-book, Metamorphosis as
    the-metamorphosis, Adventures of Huckleberry Finn with a leading "The".
    Reporting three published books as missing is how a checker teaches you to
    ignore it.
    """
    md = _fetch_md(book_slug(entry.title))
    if md:
        return True, _state_of(md)
    guessed_ok = md is not None   # "" means a genuine 404, None means we don't know

    url = litheca_url(entry, SITE_ROOT)
    if url.startswith("/summary/?b="):
        return (False, "absent") if guessed_ok else (False, "unknown")
    md = _fetch_md(url.strip("/").split("/")[-1])
    if md:
        return True, _state_of(md)
    return (False, "absent") if md is not None else (False, "unknown")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="stop after N books (use 1 as a canary)")
    ap.add_argument("--pause", type=float, default=12.0, help="seconds between books")
    ap.add_argument("--check-only", action="store_true",
                    help="report each pool book's page state and change nothing")
    args = ap.parse_args()

    if args.check_only:
        indexed = missing = fine = 0
        for entry in GTB_POOL:
            exists, state = page_index_state(entry)
            mark = {"indexed": "INDEXED", "noindex": "ok", "absent": "no page",
                    "unknown": "check failed"}[state]
            print(f"  {entry.title:<44} {mark}")
            indexed += state == "indexed"
            missing += state == "absent"
            fine += state == "noindex"
        unknown = len(GTB_POOL) - fine - indexed - missing
        print(f"\n{fine} published+noindex · {indexed} INDEXED · {missing} not published"
              + (f" · {unknown} could not be checked" if unknown else ""))
        return 0

    if not wake_api():
        print("API unreachable — aborting.")
        return 2

    # "unknown" is deliberately NOT in this list: re-publishing a book whose
    # page we merely failed to fetch is how duplicates get made.
    todo = [e for e in GTB_POOL if page_index_state(e)[1] == "absent"]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} book(s) to publish.\n")

    published = failed = 0
    for i, entry in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {entry.title} — {entry.author}", flush=True)
        ok, detail = summarize(entry.title, entry.author)
        if not ok:
            print(f"    summary failed: {detail}")
            failed += 1
            continue

        time.sleep(PUBLISH_SETTLE_SECONDS)
        exists, state = slug_index_state(detail)
        if not exists:
            print(f"    no page at _books/{detail}.md yet (publish may still be settling)")
            failed += 1
        elif state == "indexed":
            # The one outcome worth stopping everything for.
            print(f"\n  ABORT: _books/{detail}.md came out with noindex:false.")
            print("  DEFER_INDEXING is not active on the deployed API. Set it on")
            print("  Render, wait for the redeploy, then re-run. Fix this page by hand.")
            return 1
        else:
            print(f"    published, not indexed ✓  → /summary/{detail}/")
            published += 1
        time.sleep(args.pause)

    print(f"\n{published} published, {failed} failed.")
    if published:
        print("Re-run make_gtb_puzzles.py --overwrite for affected dates so the "
              "committed reveal links point at the new static pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
