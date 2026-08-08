"""
scripts/akinator/fetch_covers.py — cover ids for the corpus.

    python scripts/akinator/fetch_covers.py          # resumes

WHY A SEPARATE PASS. `cover_i` was not among the fields `build_corpus.py`
asked for, and the owner wants the cover shown with the guess. Re-running
the full corpus fetch would work and takes about an hour; this asks the same
paginated query for **two fields instead of twenty**, so the responses are a
fraction of the size and it finishes in minutes.

WHY NOT CONSTRUCT THE URL FROM THE WORK ID. Tested first, because it would
have needed no fetching at all: `covers.openlibrary.org/b/olid/OL17930368W-M.jpg`
returns **404** for every work id in the corpus. The covers API resolves
edition ids and its own numeric `cover_i`, not work ids.

Output: data/akinator_covers.json — `{work_key: cover_i}`. Merged at build
time rather than back into the corpus file, so a failed or partial run can
never corrupt the corpus itself.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_covers.json")

SEARCH_API = "https://openlibrary.org/search.json"
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}
PAGE_SIZE = 100


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=6000,
                    help="how deep to walk; the shipped corpus is 5,000 "
                         "after filtering, so a little margin is wanted")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    covers: dict[str, int] = {}
    offset = 0
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        covers = saved.get("covers", saved)
        offset = saved.get("walked", 0) if isinstance(saved, dict) else 0
        print(f"Resuming at offset {offset} with {len(covers)} covers held.")

    # The walked distance is tracked separately from the number of covers
    # found. Resuming on len(covers) would drift backwards every time a
    # book had no cover — re-walking pages already done, forever.
    failures = 0

    while offset < args.limit:
        url = SEARCH_API + "?" + urllib.parse.urlencode({
            "q": "*:*", "sort": "readinglog",
            "limit": min(PAGE_SIZE, args.limit - offset),
            "offset": offset, "fields": "key,cover_i",
        })
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                docs = json.load(resp).get("docs") or []
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures >= 5:
                print("\n! five failures in a row; stopping. Re-run to resume.",
                      file=sys.stderr)
                break
            print(f"  . offset {offset} failed ({str(exc)[:50]}); retrying",
                  file=sys.stderr)
            time.sleep(5 * failures)
            continue

        if not docs:
            break
        for doc in docs:
            key, cid = doc.get("key"), doc.get("cover_i")
            if key and cid:
                covers[key] = cid
        offset += len(docs)

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"walked": offset, "covers": covers}, fh)
        if offset % 1000 < PAGE_SIZE:
            print(f"  {offset:>6} walked, {len(covers)} with a cover")
        time.sleep(args.delay)

    print(f"\n{len(covers)} covers -> {args.out}")


if __name__ == "__main__":
    main()
