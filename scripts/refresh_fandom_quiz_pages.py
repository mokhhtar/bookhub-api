"""
scripts/refresh_fandom_quiz_pages.py — republish the committed book pages
whose quiz was built from a fan wiki instead of the book.

    python scripts/refresh_fandom_quiz_pages.py --dry-run   # list, touch nothing
    python scripts/refresh_fandom_quiz_pages.py --limit 1   # canary FIRST
    python scripts/refresh_fandom_quiz_pages.py             # the rest

WHY. Of the published pages carrying a quiz, eight are grounded in a Fandom
chapter recap rather than the book's own text, and they are wrong in two
different ways at once:

  * Three of them (Les Miserables, Peter Pan, The Count of Monte Cristo) have
    a real Gutenberg text and never needed the wiki. They were denied it by a
    poisoned free_ebook cache entry — the sub-cache the self-heal reads from
    was as stale as the payload it was healing, so the heal wrote the wrong
    value back and reported success. Fixed by the `force` parameter on
    _cached_free_ebook; these pages then simply need to be rebuilt.
  * The other five have no text at all and keep a Fandom quiz. Theirs was
    generated before tools/fandom.py learned to drop a wiki page that declares
    it also covers a screen adaptation — V for Vendetta's quiz asked about an
    event that happens only in the 2006 film. Republishing regenerates them
    under that guard.

HOW IT FINDS THEM. By the invariant, not by a list of eight slugs: any page
whose front matter says `quiz_source: "fandom_summary"`. The set shrinks on
its own as texts become available, and the script stays correct after today.

Publishing is a SIDE EFFECT of a normal /summary call (publish_book runs as a
FastAPI BackgroundTask), so this is a paced crawl of the live API. It creates
nothing itself and needs no credentials. The title and author are sent exactly
as the page stores them — Peter Pan is called as "J. M. Barrie", the spelling
whose cache entry was poisoned, deliberately: that is the test.

Two aborts rather than a false success. If a page comes back holding a
free_ebook payload older than _FREE_EBOOK_PAYLOAD_VERSION, the `force` fix is
not live on the deployed process and every quiz built now would come from the
wiki again — stop. If one of the three known-text books comes back without a
Gutenberg edition, the heal did not take for it — stop and say so.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.summary import _FREE_EBOOK_PAYLOAD_VERSION  # noqa: E402
from github_publisher import PUBLISH_CONTENT_VERSION  # noqa: E402

API = os.environ.get("LITHECA_API", "https://bookhub-api-hnv7.onrender.com")
SITE = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "bookhub"))
BOOKS = os.path.join(SITE, "_books")

PACE_SECONDS = 4.0       # Render free tier; the crawl is not in a hurry.
REQUEST_TIMEOUT = 300.0  # a cold instance takes 30-60s, and a rebuilt quiz
                         # means Gemini calls over the full text on top.


def _front_matter(path: str) -> dict:
    """title/author/content_version/quiz_source + the stored free_ebook."""
    text = open(path, encoding="utf-8").read()
    out = {}
    for field in ("title", "author", "quiz_source"):
        m = re.search(rf'^{field}: "(.*)"\s*$', text, re.M)
        if m:
            out[field] = m.group(1)
    m = re.search(r"^content_version: (\d+)\s*$", text, re.M)
    out["content_version"] = int(m.group(1)) if m else 0
    m = re.search(r"^free_ebook: (\{.*\})\s*$", text, re.M)
    if m:
        try:
            out["free_ebook"] = json.loads(m.group(1))
        except Exception:
            pass
    return out


def find_affected() -> list[dict]:
    """Pages whose committed quiz was built from a fan wiki."""
    pages = []
    for name in sorted(os.listdir(BOOKS)):
        if not name.endswith(".md"):
            continue
        fm = _front_matter(os.path.join(BOOKS, name))
        if fm.get("quiz_source") == "fandom_summary" and fm.get("title"):
            fm["file"] = name
            pages.append(fm)
    return pages


def _describe(fe: dict | None) -> str:
    if not fe:
        return "no free ebook"
    return (f"v{fe.get('v', 0)} {fe.get('source')}"
            + (f" gid {fe['gutenberg_id']}" if fe.get("gutenberg_id") else ""))


def refresh(page: dict, client: httpx.Client) -> tuple[str, bool]:
    """POST /summary so publish_book re-runs. Returns (outcome, keep_going)."""
    # A /summary call CANNOT rewrite a page that already records the current
    # content version: the Redis flag short-circuits publish_book before the
    # repo is read, and the repo check itself only rewrites when the page is a
    # version behind. So a page can be marked done while its content is wrong —
    # which is what happens to a page refreshed in the window between a
    # resolver fix and the cache heal that makes it take. Peter Pan was
    # refreshed to v11 while still holding the poisoned v2 free_ebook payload.
    #
    # Calling anyway would return a healthy-looking response and change
    # nothing, and this script would print a success. Refuse instead.
    if page["content_version"] >= PUBLISH_CONTENT_VERSION:
        return (f"BLOCKED already at v{page['content_version']} — a /summary "
                f"call cannot rewrite it; needs an explicit republish", True)
    try:
        r = client.post(f"{API}/summary", json={
            "title": page["title"], "author": page.get("author") or "",
        }, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        return f"error ({type(e).__name__})", True
    if r.status_code != 200:
        return f"http {r.status_code}", True
    try:
        fe = r.json().get("free_ebook")
    except Exception:
        return "unparseable", True

    if fe and fe.get("v", 0) < _FREE_EBOOK_PAYLOAD_VERSION:
        return (f"STALE {_describe(fe)} — the `force` fix is not live", False)

    # The three books that have a text: if the heal took, this is Gutenberg.
    # A page that stored an Archive scan and still does not have one means the
    # heal did not fire for it, and its quiz will come from the wiki again.
    was_archive = (page.get("free_ebook") or {}).get("source") == "internet_archive"
    if was_archive and (fe or {}).get("source") != "project_gutenberg":
        return (f"NOT HEALED {_describe(fe)} — was an Archive scan", False)

    if (fe or {}).get("source") == "project_gutenberg":
        return f"{_describe(fe)} -> expect quiz_source: gutenberg_text", True
    return f"{_describe(fe)} -> stays fandom_summary, regenerated", True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list the affected pages, change nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N pages (use 1 as a canary)")
    args = ap.parse_args()

    affected = find_affected()
    print(f"{len(affected)} committed page(s) carry a Fandom-grounded quiz:\n")
    for p in affected:
        print(f"  {p['file'][:-3]:<46} v{p['content_version']:<3} "
              f"{_describe(p.get('free_ebook'))}")
    if not affected:
        return 0
    if args.dry_run:
        print("\n--dry-run: nothing was called.")
        return 0

    todo = affected[:args.limit] if args.limit else affected
    print(f"\nRefreshing {len(todo)} page(s) via {API}, {PACE_SECONDS}s apart.\n")
    with httpx.Client() as client:
        for i, p in enumerate(todo, 1):
            outcome, keep_going = refresh(p, client)
            print(f"  [{i}/{len(todo)}] {p['file'][:-3]:<46} {outcome}")
            if not keep_going:
                print("\nAborting: republishing now would rebuild the quiz from "
                      "the wiki while reporting success. Nothing further was called.")
                return 1
            if i < len(todo):
                time.sleep(PACE_SECONDS)

    print("\nDone. The pages are rewritten by a BackgroundTask, so give the "
          "commits a minute, then `git pull` in the site repo and read the diff: "
          "quiz_source and content_version are the two fields to check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
