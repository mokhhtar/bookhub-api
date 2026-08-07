"""
scripts/akinator/harvest_works.py — screen adaptations and series, via authors.

    python scripts/akinator/harvest_works.py
    python scripts/akinator/harvest_works.py --limit 500   # resumes

THE JOIN THAT MAKES THIS POSSIBLE. Book-level Wikidata looked closed:
Open Library carries a Wikidata id on **6% of books**, which is why
[[2026-08-07 — Akinator phase 2]] abandoned book facts and harvested
authors instead. That 6% was never a limit on Wikidata — it was a limit on
one join key.

Phase 2 left 5,107 matched author QIDs behind, and Wikidata can be walked
from an author to their works (`?work wdt:P50 ?author`), reaching books
that carry no Open Library id at all. Measured on the top 2,000:

    our books whose author matched Wikidata          77%
    those matching a Wikidata work by author+title   69%
    effective book-level coverage                   ~50%

Nine times what the direct route allowed.

WHAT IT COLLECTS, and why these two ride together. The traversal is the
expensive part; both facts come off the same query for free.

  * **Screen adaptation** (`?film wdt:P144 ?work`). Measured at **46% of
    matched books** — close to an even split, and something anyone
    picturing a book can answer instantly. The best question found since
    the geographic set.
  * **Series membership** (`wdt:P179`). This is a repair, not a new
    feature: "Is it part of a series?" reads as 4.2% in our data only
    because Open Library rarely records series, which is a data failure
    rather than a rare property. It is currently kept alive by FORCE_KEEP
    in features.py and answers `unknown` almost everywhere.

MATCHING IS BY AUTHOR + NORMALISED TITLE, never title alone — same-title
collisions across different works are common, and the author key is what
disambiguates them. Titles go through `features.normalize()`, the same
normalizer used everywhere else, so two spellings cannot resolve
differently.

GROUNDING. A book we could not match is absent from the output entirely,
and the feature layer scores that as `unknown` rather than "no". A gap in
Wikidata must never make a book harder to guess — the same rule that
governs a missing subject.

Output: data/akinator_works_wd.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import normalize  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
AUTHORS_WD_PATH = os.path.join(REPO_ROOT, "data", "akinator_authors_wd.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_works_wd.json")

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
    "Accept": "application/sparql-results+json",
}

# Works queries return many rows per author (every work x every adaptation),
# so the batch is smaller than the author harvest's 400. WDQS times out at
# one minute and a timeout loses the whole batch, not just the overflow.
BATCH = 60
DELAY = 2.5

# Q11424 film, Q5398426 television series. A player answering "was it made
# into a film?" does not draw a line between a feature film and a
# prestige TV adaptation, and including both roughly doubles the hit rate,
# so the question is worded for screen rather than cinema.
QUERY = """SELECT ?author ?work ?workLabel ?adapted ?series WHERE {
  VALUES ?author { %s }
  ?work wdt:P50 ?author .
  OPTIONAL {
    ?adapted wdt:P144 ?work .
    ?adapted wdt:P31/wdt:P279* ?kind .
    VALUES ?kind { wd:Q11424 wd:Q5398426 }
  }
  OPTIONAL { ?work wdt:P179 ?series }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}"""


def load_targets() -> tuple[list[tuple[str, str]], set[tuple[str, str]]]:
    """Authors as (OL key, QID) **in popularity order**, and the
    (author, title) pairs present in the corpus.

    Popularity order is not cosmetic. This job runs for a long time against
    a shared public endpoint and is built to be interrupted, so whatever it
    finishes first has to be the part that matters. Sorting by key instead
    — as the first version did — samples authors essentially at random, and
    the smoke test showed the cost immediately: a 180-author alphabetical
    run found screen adaptations on 17% of matched books, against 46% when
    the sample was drawn from the most-read end. Obscure authors are less
    adapted and less documented, and they are also the books players are
    least likely to be thinking of.

    Only pairs in the corpus are kept — Wikidata knows far more works than
    we carry.
    """
    with open(AUTHORS_WD_PATH, encoding="utf-8") as fh:
        wd = json.load(fh)

    docs = []
    with open(CORPUS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))

    wanted: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for doc in docs:
        title = normalize(doc.get("title") or "")
        if not title:
            continue
        for key in doc.get("author_key") or []:
            if key not in wd:
                continue
            wanted.add((key, title))
            if key not in seen:
                seen.add(key)
                ordered.append((key, wd[key]["qid"]))
    return ordered, wanted


def run_query(qids: list[str], timeout: int) -> list[dict]:
    values = " ".join(f"wd:{q}" for q in qids)
    url = ENDPOINT + "?" + urllib.parse.urlencode(
        {"query": QUERY % values, "format": "json"})
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["results"]["bindings"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N authors (0 = all)")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    ordered, wanted = load_targets()
    authors = dict(ordered)
    wanted_titles: dict[str, set[str]] = {}
    for key, title in wanted:
        wanted_titles.setdefault(key, set()).add(title)

    keys = [k for k, _ in ordered]      # popularity order — see load_targets
    if args.limit:
        keys = keys[:args.limit]
    print(f"{len(keys)} matched authors covering {len(wanted)} corpus books\n")

    found: dict[str, dict] = {}
    asked: set[str] = set()
    asked_path = args.out.replace(".json", "_asked.json")
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            found = json.load(fh)
    if os.path.exists(asked_path):
        with open(asked_path, encoding="utf-8") as fh:
            asked = set(json.load(fh))
    if asked:
        print(f"Resuming: {len(asked)} authors already asked, "
              f"{len(found)} books matched.\n")

    qid_to_key = {qid: key for key, qid in authors.items()}
    todo = [k for k in keys if k not in asked]
    failures = 0

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            rows = run_query([authors[k] for k in chunk], args.timeout)
            failures = 0
        except Exception as exc:  # noqa: BLE001 — any transport/timeout failure
            failures += 1
            if failures >= 4:
                print("\n! four consecutive failures; stopping. Re-run to resume.",
                      file=sys.stderr)
                break
            print(f"  . batch at {i} failed ({str(exc)[:70]}); retrying",
                  file=sys.stderr)
            time.sleep(10 * failures)
            continue

        before = len(found)
        for b in rows:
            qid = b["author"]["value"].rsplit("/", 1)[-1]
            key = qid_to_key.get(qid)
            if not key:
                continue
            label = b.get("workLabel", {}).get("value") or ""
            title = normalize(label)
            # Only keep works we actually carry — Wikidata knows far more.
            if not title or title not in wanted_titles.get(key, ()):
                continue
            rec = found.setdefault(f"{key}|{title}",
                                   {"film": False, "series": False, "sid": None})
            if "adapted" in b:
                rec["film"] = True
            if "series" in b:
                rec["series"] = True
                # The series IDENTITY, not just its existence. The first
                # version stored a boolean, which answers "is it part of a
                # series?" but cannot group Harry Potter's twelve rows into
                # one candidate — and that grouping is the fix for both the
                # wrong-volume guesses and the question count.
                rec["sid"] = b["series"]["value"].rsplit("/", 1)[-1]

        asked.update(chunk)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False)
        with open(asked_path, "w", encoding="utf-8") as fh:
            json.dump(sorted(asked), fh)

        print(f"  {i + len(chunk):>6}/{len(todo)} authors   "
              f"{len(found):>6} books matched (+{len(found) - before})")
        time.sleep(DELAY)

    if found:
        films = sum(1 for r in found.values() if r["film"])
        series = sum(1 for r in found.values() if r["series"])
        sids = {r.get("sid") for r in found.values() if r.get("sid")}
        print(f"\n{len(found)} corpus books matched to a Wikidata work")
        print(f"  screen adaptation  {films:>6} ({films * 100 // len(found)}%)")
        print(f"  in a series        {series:>6} ({series * 100 // len(found)}%)"
              f" across {len(sids)} distinct series")
        print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
