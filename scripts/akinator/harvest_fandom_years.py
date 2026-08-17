"""
scripts/akinator/harvest_fandom_years.py — first-publish year: Wikidata, then OL.

    python scripts/akinator/harvest_fandom_years.py --limit 10
    python scripts/akinator/harvest_fandom_years.py            # resumes

WHY THIS FIELD. Measured with `simulate.py --target-prefix /fandom/`:

    books WITH a first-publish year    25    24.0%
    books WITHOUT                     153    13.1%
    Open Library at the same ranks            29.2%   (the ceiling)

The year is worth about eleven points — books carrying one reach 82% of
what is achievable at that depth. The AUTHOR harvested alongside it was
worth almost nothing (1 of 69 matched a corpus author key), so this is
the field to chase.

WHY WIKIDATA FIRST, AND IT IS NOT A PREFERENCE. A first version asked
Open Library alone and produced a **~40% error rate**, audited by hand
over all 31 answers:

    Horrid Henry        1896   (1994)   OL's own row is wrong
    The Chronicles…     1970   (1950)   an omnibus edition's year
    Inkheart            2024   (2003)   matched "Inkheart 4"
    Lunar Chronicles    2014   (2012)   matched a COLORING BOOK
    Safehold            2014   (2007)   matched "Boxed Set 1"
    Fifty Shades…       2000   (2011)   OL's own row is wrong

Two separate causes. Open Library does not cleanly separate a WORK from
its editions and derivatives, so a search surfaces boxed sets, coloring
books and later volumes beside the thing itself; and some of its rows are
simply wrong. Wikidata models the distinction explicitly — `P31` says what
an entity is, and `Q3331189` ("version, edition or translation") is
filterable — and it carries `P577` on the work.

Re-checked by hand on the exact books Open Library got wrong: Wikidata
returned 1994, 2003 and 2011 correctly, and for Lunar Chronicles returned
NOTHING rather than a wrong year. Failing to null is the behaviour this
codebase requires; a wrong year is a confident claim about the era that
eliminates the right book.

OPEN LIBRARY IS STILL USED, as a fallback, because it covers books
Wikidata does not and the errors above are avoidable rather than
inherent. Two rules, both learned from that audit:

  * EXACT title match only. Every "boxed set / coloring book / volume 9"
    error came from allowing a prefix, so prefixes are gone.
  * The author must match by surname too, which is what makes the query
    identify a work rather than a string — searching Open Library by
    title alone is the "Against the Gods" trap, where OL's row under that
    title is Peter L. Bernstein's book on risk management.

This mirrors `book_data.py`'s Google Books -> Open Library chain: a
better-structured source first, a broader one behind it, and the
provenance recorded either way so a bad answer can be traced to its
source rather than argued about.

RATE LIMITS ARE REAL on Wikidata's SPARQL endpoint — 429s and timeouts
were both hit while testing. Slow by default, retried, and resumable.

Output: data/akinator_fandom_years.json (gitignored, regenerable), with
a `source` field per book.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authors import canonical_author, surname_token   # noqa: E402
from features import normalize                        # noqa: E402
from prove_fandom import _same_work                   # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTHORS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_authors.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_years.json")

WDQS = "https://query.wikidata.org/sparql"
OL_SEARCH = "https://openlibrary.org/search.json"
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

# `rdfs:label|skos:altLabel` on both sides: a work is filed under one name
# and known by another constantly, and so is an author. FILTER NOT EXISTS
# on Q3331189 is what keeps an EDITION from answering for the work -- the
# distinction Open Library does not make and this whole fallback order
# exists because of.
_SPARQL = """SELECT ?w ?date WHERE {
  ?w rdfs:label|skos:altLabel "%s"@en .
  ?w wdt:P50 ?a . ?a rdfs:label|skos:altLabel "%s"@en .
  ?w wdt:P577 ?date .
  FILTER NOT EXISTS { ?w wdt:P31 wd:Q3331189 }
} ORDER BY ?date LIMIT 3"""


def _escape(s: str) -> str:
    return s.replace("\\", "").replace('"', "").strip()


def wikidata_year(title: str, author: str, timeout: int = 45) -> int | None:
    q = _SPARQL % (_escape(title), _escape(author))
    url = f"{WDQS}?{urllib.parse.urlencode({'query': q, 'format': 'json'})}"
    req = urllib.request.Request(
        url, headers={**HEADERS, "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
    for r in rows:
        v = (r.get("date") or {}).get("value") or ""
        if len(v) >= 4 and v[:4].isdigit():
            y = int(v[:4])
            if 1500 <= y <= 2049:
                return y
    return None


def openlibrary_year(title: str, author: str) -> int | None:
    """Fallback. EXACT title match only -- see the module docstring."""
    q = urllib.parse.urlencode({
        "title": title, "author": author, "limit": 10,
        "fields": "title,author_name,first_publish_year"})
    req = urllib.request.Request(f"{OL_SEARCH}?{q}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    want_sur = surname_token(canonical_author(author))
    best: int | None = None
    for doc in data.get("docs") or []:
        year, got = doc.get("first_publish_year"), doc.get("title") or ""
        if not year or not got:
            continue
        if not any(surname_token(canonical_author(n)) == want_sur
                   for n in (doc.get("author_name") or [])):
            continue
        # No prefixes. "Inkheart 4", "Lunar Chronicles Coloring Book" and
        # "Safehold Boxed Set 1" all passed a prefix test and all gave the
        # wrong year.
        if not _same_work(title, got) or normalize(got) != normalize(title):
            continue
        if best is None or year < best:
            best = year
    return best


def harvest_one(title: str, author: str, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        try:
            y = wikidata_year(title, author)
            if y:
                return {"author": author, "year": y, "source": "wikidata"}
            break                      # answered, just had nothing
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError) as exc:
            # A 429 or a timeout is NOT "this book has no year" -- it is no
            # answer at all, and treating the two alike is how a rate limit
            # becomes a permanent gap in the data.
            if attempt == retries:
                return {"author": author, "year": None,
                        "source": f"wikidata_unavailable:{type(exc).__name__}"}
            time.sleep(8 * (attempt + 1))
        except Exception:                                       # noqa: BLE001
            break

    try:
        y = openlibrary_year(title, author)
        if y:
            return {"author": author, "year": y, "source": "openlibrary"}
    except Exception:                                           # noqa: BLE001
        pass
    return {"author": author, "year": None, "source": None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between books. Wikidata's SPARQL endpoint "
                         "returned 429s and timeouts during testing; this is "
                         "deliberately unhurried.")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--retry-unavailable", action="store_true",
                    help="re-attempt books whose last run hit a rate limit "
                         "or timeout, without redoing the ones that answered")
    args = ap.parse_args()

    with open(AUTHORS_PATH, encoding="utf-8") as fh:
        authors = json.load(fh)
    targets = [(t, r["author"]) for t, r in authors.items()
               if r.get("author") and not r.get("year")]

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)
    if args.retry_unavailable:
        done = {t: r for t, r in done.items()
                if not str(r.get("source") or "").startswith("wikidata_unavailable")}

    todo = [(t, a) for t, a in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(targets)} book(s) with an author and no year; "
          f"{len(done)} done; {len(todo)} to do\n")

    for i, (title, author) in enumerate(todo, 1):
        res = harvest_one(title, author)
        done[title] = res
        print(f"  [{i:>3}/{len(todo)}] {title[:28]:<28} {author[:20]:<20} "
              f"{str(res['year'] or '----'):<6} {res['source'] or ''}")
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    wd = sum(1 for r in done.values() if r.get("source") == "wikidata")
    ol = sum(1 for r in done.values() if r.get("source") == "openlibrary")
    un = sum(1 for r in done.values()
             if str(r.get("source") or "").startswith("wikidata_unavailable"))
    print(f"\n{wd + ol}/{len(done)} gained a year "
          f"({wd} from Wikidata, {ol} from Open Library)")
    if un:
        print(f"  {un} could not reach Wikidata (rate limit / timeout) — "
              f"re-run with --retry-unavailable; they are NOT 'no year'.")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
