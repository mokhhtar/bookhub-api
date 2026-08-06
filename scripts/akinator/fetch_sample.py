"""
scripts/akinator/fetch_sample.py — Phase 0 corpus for the book mind-reader.

    python scripts/akinator/fetch_sample.py --limit 500

WHY THIS EXISTS. Phase 0 of the Akinator plan is a gate, not a deliverable:
before committing to the 4 GB monthly dump pipeline we need to know whether
Open Library metadata can actually separate books in ~20 questions. That
question is answerable on a few hundred books pulled from the live search
API, which takes a minute instead of an afternoon.

WHY THE SEARCH API AND NOT THE DUMP. The dump is the Phase 1 source and this
script is deliberately NOT it — the API caps out well before 20k books and
its rate limits make a full corpus impractical. What it does give us, for
free and immediately, is the exact same field shapes the dump carries, so a
feature pipeline validated here transfers.

POPULARITY. `sort=readinglog` orders by `readinglog_count`, the prior the
requirements note settled on after measuring that `edition_count` is a trap
(classics get reprinted forever; Dune shows 77 against Moby-Dick's 1,116).
Fetching in popularity order means a truncated sample is still the *right*
sample: the books players actually think of.

Output: data/akinator_sample.json — raw OL docs, unprocessed. Normalization
lives in features.py so it can be re-run without re-fetching.
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
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_sample.json")

SEARCH_API = "https://openlibrary.org/search.json"

# Identified requests get 3 req/s from Open Library against 1 req/s for
# anonymous ones, and identification has to include a working contact.
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; contact@litheca.com)"}

# Only the fields the feature pipeline reads. Asking for `*` pulls ~100
# fields per doc including 30 trending-score columns we have no use for.
FIELDS = ",".join([
    "key",                      # /works/OL...W — the stable id everything anchors on
    "title",
    "author_name",
    "author_key",
    "first_publish_year",
    "subject",                  # the feature matrix's raw material
    "person",                   # characters, for fiction — see the module note below
    "place",                    # setting
    "time",                     # period
    "language",
    "number_of_pages_median",
    "readinglog_count",         # the popularity prior
    "ratings_count",
    "ratings_average",
    "edition_count",            # weak tiebreak only
    "id_wikidata",              # the join key into Wikidata for character traits
    "has_fulltext",
    "ebook_access",
])

PAGE_SIZE = 100


def fetch_page(offset: int, page_size: int) -> dict:
    url = SEARCH_API + "?" + urllib.parse.urlencode({
        "q": "*:*",
        "sort": "readinglog",
        "limit": page_size,
        "offset": offset,
        "fields": FIELDS,
    })
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch(limit: int, delay: float) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    while len(docs) < limit:
        page_size = min(PAGE_SIZE, limit - len(docs))
        for attempt in range(3):
            try:
                data = fetch_page(offset, page_size)
                break
            except Exception as exc:  # noqa: BLE001 — retry any transport failure
                if attempt == 2:
                    print(f"  ! giving up at offset {offset}: {exc}", file=sys.stderr)
                    return docs
                print(f"  . retry {attempt + 1} at offset {offset} ({exc})", file=sys.stderr)
                time.sleep(2 + attempt * 3)

        page = data.get("docs") or []
        if not page:
            break
        docs.extend(page)
        print(f"  {len(docs):>5} books  (last: {page[-1].get('title', '?')[:50]})")
        offset += page_size
        time.sleep(delay)
    return docs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=500, help="how many works to pull")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between pages")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    print(f"Fetching top {args.limit} works by readinglog_count...")
    docs = fetch(args.limit, args.delay)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(docs, fh, ensure_ascii=False, indent=1)

    with_subjects = sum(1 for d in docs if d.get("subject"))
    with_persons = sum(1 for d in docs if d.get("person"))
    with_wikidata = sum(1 for d in docs if d.get("id_wikidata"))
    print()
    print(f"Wrote {len(docs)} works to {args.out}")
    print(f"  with subjects : {with_subjects} ({with_subjects * 100 // max(1, len(docs))}%)")
    print(f"  with persons  : {with_persons} ({with_persons * 100 // max(1, len(docs))}%)")
    print(f"  with wikidata : {with_wikidata} ({with_wikidata * 100 // max(1, len(docs))}%)")


if __name__ == "__main__":
    main()
