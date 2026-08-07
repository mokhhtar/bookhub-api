"""
scripts/akinator/harvest_authors.py — author facts from Wikidata.

    python scripts/akinator/harvest_authors.py
    python scripts/akinator/harvest_authors.py --limit 2000   # resumes

WHY AUTHORS AND NOT BOOKS, which is what the build plan said.

Phase 2 was specified as "a Wikidata SPARQL harvest for the 20k QIDs". There
are no 20k QIDs. Measured on the finished corpus, Open Library's own
`id_wikidata` field is present on **6% of books**, and it decays with
popularity exactly like everything else — 23% in the top thousand, 3% in the
tail. Harvesting book facts that way would enrich the head, which is the
part that least needs it.

Asking from Wikidata's side instead changes the picture completely.
**505,593 Wikidata items carry an Open Library ID** (P648), against the
1,289 of our books that carry a Wikidata id. But sampling those P648 values
shows what they point at:

    editions (OL...M)   61%
    authors  (OL...A)   34%
    works    (OL...W)    4%

So book-level matching stays poor — but we hold `author_key` for **98% of
works**, and a test batch of our 400 most-read authors matched Wikidata at
**69%**, carrying gender, nationality and death dates.

THE REASON THIS IS THE RIGHT LEVER. Phase 1 measured the game failing in the
tail because metadata thins with obscurity: a book at rank 10,000 has half
the subjects of one at rank 1,000. **Author facts do not thin the same
way.** An obscure book still has an author, and that author may well be a
documented person — so one match enriches every book they wrote, including
the ones Open Library barely describes. Author traits were also already
identified as the best *early-game* questions ("is the author still alive?",
"is the author a woman?"), which is the half of the game the corpus is
shortest on.

WHAT IS DELIBERATELY NOT HERE. Characters. Wikidata has P674 on only 3,086
of the half-million P648 items, so Open Library's `person` field remains the
better cast source — see characters.py. Chasing Wikidata for characters
would be the same mistake as chasing it for books.

Output: data/akinator_authors_wd.json (gitignored, regenerable).
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
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_authors_wd.json")

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
    "Accept": "application/sparql-results+json",
}

# WDQS is a shared public service with a one-minute query timeout. Batches
# of 400 VALUES came back in a few seconds in testing; going much wider
# risks the timeout and gets nothing back for the whole batch.
BATCH = 400
DELAY = 2.0

QUERY = """SELECT ?olid ?item ?gender ?country ?countryLabel ?birth ?death ?occ WHERE {
  VALUES ?olid { %s }
  ?item wdt:P648 ?olid .
  OPTIONAL { ?item wdt:P21  ?gender }
  OPTIONAL { ?item wdt:P27  ?country }
  OPTIONAL { ?item wdt:P569 ?birth }
  OPTIONAL { ?item wdt:P570 ?death }
  OPTIONAL { ?item wdt:P106 ?occ }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}"""

# Wikidata gender items, mapped to what a question can ask.
GENDER = {"Q6581097": "male", "Q6581072": "female"}


def author_keys(corpus_path: str) -> list[str]:
    """Every author key in the corpus, most-read first."""
    docs = []
    with open(corpus_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    keys, seen = [], set()
    for doc in docs:
        for key in doc.get("author_key") or []:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def run_query(olids: list[str], timeout: int) -> list[dict]:
    values = " ".join(f'"{k}"' for k in olids)
    url = ENDPOINT + "?" + urllib.parse.urlencode(
        {"query": QUERY % values, "format": "json"})
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["results"]["bindings"]


def fold(rows: list[dict], into: dict) -> None:
    """Collapse SPARQL's row-per-combination output into one record each.

    A single author comes back many times over — one row per
    (country x occupation x ...) combination, and Wikidata often holds
    several countries for one person ('United Kingdom' and 'United Kingdom
    of Great Britain and Ireland' both appeared for H. G. Wells in
    testing). Sets, not assignment.
    """
    for b in rows:
        olid = b["olid"]["value"]
        rec = into.setdefault(olid, {
            "qid": b["item"]["value"].rsplit("/", 1)[-1],
            "gender": None, "countries": [], "occupations": [],
            "birth": None, "death": None,
        })
        if "gender" in b:
            g = GENDER.get(b["gender"]["value"].rsplit("/", 1)[-1])
            if g:
                rec["gender"] = g
        if "countryLabel" in b:
            c = b["countryLabel"]["value"]
            if c and c not in rec["countries"]:
                rec["countries"].append(c)
        if "occ" in b:
            o = b["occ"]["value"].rsplit("/", 1)[-1]
            if o not in rec["occupations"]:
                rec["occupations"].append(o)
        for field, key in (("birth", "birth"), ("death", "death")):
            if key in b and not rec[field]:
                rec[field] = b[key]["value"][:10]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default=CORPUS_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--limit", type=int, default=0,
                    help="only the N most-read authors (0 = all)")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    keys = author_keys(args.corpus)
    if args.limit:
        keys = keys[:args.limit]

    found: dict[str, dict] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            found = json.load(fh)
        print(f"Resuming with {len(found)} authors already harvested.")

    # `found` only holds matches, so a key absent from it may simply have no
    # Wikidata item. A separate list records which keys were asked about, so
    # a resume doesn't re-ask for known misses.
    asked_path = args.out.replace(".json", "_asked.json")
    asked: set[str] = set()
    if os.path.exists(asked_path):
        with open(asked_path, encoding="utf-8") as fh:
            asked = set(json.load(fh))

    todo = [k for k in keys if k not in asked]
    print(f"{len(keys)} authors in corpus, {len(todo)} still to ask about.\n")

    failures = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            rows = run_query(chunk, args.timeout)
            failures = 0
        except Exception as exc:  # noqa: BLE001 — any transport/timeout failure
            failures += 1
            if failures >= 4:
                print(f"\n! four consecutive failures; stopping. Re-run to resume.",
                      file=sys.stderr)
                break
            print(f"  . batch at {i} failed ({str(exc)[:70]}); retrying",
                  file=sys.stderr)
            time.sleep(10 * failures)
            continue

        before = len(found)
        fold(rows, found)
        asked.update(chunk)

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False)
        with open(asked_path, "w", encoding="utf-8") as fh:
            json.dump(sorted(asked), fh)

        print(f"  {i + len(chunk):>6}/{len(todo)} asked   "
              f"{len(found):>6} matched (+{len(found) - before})")
        time.sleep(DELAY)

    if found:
        g = sum(1 for r in found.values() if r["gender"])
        c = sum(1 for r in found.values() if r["countries"])
        d = sum(1 for r in found.values() if r["death"])
        print(f"\n{len(found)} authors matched of {len(asked)} asked "
              f"({len(found) * 100 // max(1, len(asked))}%)")
        print(f"  gender      {g:>6} ({g * 100 // len(found)}%)")
        print(f"  nationality {c:>6} ({c * 100 // len(found)}%)")
        print(f"  death date  {d:>6} ({d * 100 // len(found)}%)")
        print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
