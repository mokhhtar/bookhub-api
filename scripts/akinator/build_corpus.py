"""
scripts/akinator/build_corpus.py — the real corpus, 20k books deep.

    python scripts/akinator/build_corpus.py --limit 20000
    python scripts/akinator/build_corpus.py --limit 20000   # resumes

WHY NOT THE 4 GB DUMP, which is what the build plan specified.

The plan assumed the monthly refresh had to process `ol_dump_works_latest`
(4 GB gzipped, ~30 GB raw, hours of streaming, ~50 GB free disk). Two things
measured on 2026-08-06 make the search API the better instrument for this
particular job:

  1. **Deep pagination works.** Verified to offset 50,000 — well past the
     20k the requirements note settled on. 200 requests at one per second
     is roughly 20 minutes.
  2. **`readinglog_count` comes pre-computed.** It is a Solr-side
     aggregate, not a field in the works dump. Getting the popularity prior
     out of the dumps means joining works against the separate reading-log
     dump — the single most important number in the whole system, arriving
     as the most expensive part of the pipeline.

On Open Library's guidance that their APIs are "not intended as a bulk data
backend or high-traffic commercial infrastructure": that warning is about
production dependency, and this is the opposite. Play time touches no OL
endpoint at all — the game ships as static artifacts. This runs monthly, at
their published rate limit for identified clients, and caches everything
locally so a re-run costs nothing. Two hundred requests a month is less
traffic than one person browsing the site.

If that ever stops being true — a block, a rate-limit change, a corpus
target past what pagination allows — the dump path is still the documented
fallback and the field shapes are identical, so only this file changes.

RESUMABILITY IS NOT OPTIONAL. A twenty-minute job over a public API will
fail partway sometimes; the Phase 0 fetch of 500 books already hit an HTTP
500 and a truncated read. Output is JSONL and every page is appended as it
arrives, so a re-run picks up from the last complete page.

Output: data/akinator_corpus.jsonl (gitignored — Open Library names no
licence for their catalogue data, so we don't redistribute it).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")

SEARCH_API = "https://openlibrary.org/search.json"

# Identified requests get 3 req/s against 1 req/s anonymous, and the contact
# has to be one somebody reads. See the note in book_data.py.
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

FIELDS = ",".join([
    "key", "title", "author_name", "author_key", "author_alternative_name",
    "first_publish_year", "subject", "person", "place", "time", "language",
    "number_of_pages_median", "readinglog_count", "ratings_count",
    "ratings_average", "edition_count", "id_wikidata", "has_fulltext",
    "ebook_access", "first_sentence",
])

PAGE_SIZE = 100


def load_existing(path: str) -> tuple[list[dict], set[str]]:
    """Previously fetched docs, and the work keys already held.

    Keys matter as well as the count: Solr's ordering is not perfectly
    stable between runs, so resuming purely on offset can duplicate or skip.
    """
    if not os.path.exists(path):
        return [], set()
    docs, keys = [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue  # a truncated final line from an interrupted run
            key = doc.get("key")
            if key and key not in keys:
                keys.add(key)
                docs.append(doc)
    return docs, keys


def fetch_page(offset: int, page_size: int, timeout: int) -> list[dict]:
    url = SEARCH_API + "?" + urllib.parse.urlencode({
        "q": "*:*",
        "sort": "readinglog",
        "limit": page_size,
        "offset": offset,
        "fields": FIELDS,
    })
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp).get("docs") or []


def build(limit: int, delay: float, timeout: int, out_path: str) -> None:
    existing, seen = load_existing(out_path)
    print(f"Resuming with {len(existing)} books already held.\n" if existing
          else "Starting a fresh corpus.\n")

    offset = len(existing)
    added = 0
    consecutive_failures = 0

    with open(out_path, "a", encoding="utf-8") as fh:
        while len(seen) < limit:
            page_size = min(PAGE_SIZE, limit - len(seen))
            try:
                docs = fetch_page(offset, page_size, timeout)
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — any transport failure
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    print(f"\n! five consecutive failures at offset {offset}; "
                          f"stopping. Re-run to resume.", file=sys.stderr)
                    break
                wait = min(60, 5 * consecutive_failures)
                print(f"  . offset {offset} failed ({str(exc)[:60]}); "
                      f"retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue

            if not docs:
                print(f"\nOpen Library returned nothing at offset {offset} — "
                      f"end of results.")
                break

            new = 0
            for doc in docs:
                key = doc.get("key")
                if not key or key in seen:
                    continue
                seen.add(key)
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                new += 1
            fh.flush()
            added += new
            offset += len(docs)

            if len(seen) % 1000 < PAGE_SIZE:
                tail = docs[-1]
                print(f"  {len(seen):>6} books   "
                      f"(readinglog {tail.get('readinglog_count')}, "
                      f"{str(tail.get('title'))[:40]})")

            time.sleep(delay)

    print(f"\nCorpus now holds {len(seen)} books ({added} added this run).")
    print(f"  {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20000,
                    help="corpus size; the requirements note settled on 20k")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (OL asks for <=1/s anonymous)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    build(args.limit, args.delay, args.timeout, args.out)


if __name__ == "__main__":
    main()
