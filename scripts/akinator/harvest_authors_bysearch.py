"""
scripts/akinator/harvest_authors_bysearch.py — the authors P648 cannot reach.

    python scripts/akinator/harvest_authors_bysearch.py --limit 50   # try it
    python scripts/akinator/harvest_authors_bysearch.py              # the lot

WHY A SECOND AUTHOR HARVEST. `harvest_authors.py` joins Open Library author
keys to Wikidata through **P648**, the Open Library ID recorded on the
Wikidata item. That join is exact and needs no judgement, which is why it
is the primary source and stays authoritative. But it only finds authors
somebody has already linked: of the 4,134 authors in the shipped 5,000
books, **1,962 (47%) match and 2,172 do not**, and those 2,172 carry
**2,404 book-slots**.

They are not obscure. Julia Quinn (10 books here), Freida McFadden (11),
E. L. James, bell hooks, Amish Tripathi, 太宰治 — all thoroughly documented
on Wikidata, all missing the back-link. The gap is in the link, not in the
knowledge, so this asks by NAME instead.

THE PRICE OF ASKING BY NAME is that a name is not an identifier, and this
project's rule is that a WRONG author is worse than none: author facts fan
out to every book that author wrote, so one bad match poisons a whole
shelf, silently. So a candidate is accepted only when ALL of these hold:

  1. it is a human            (P31 = Q5)
  2. it has a writing occupation (P106 in WRITING_OCCUPATIONS)
  3. **exactly one** candidate in the result set satisfies 1 and 2
  4. the text that actually matched is the queried name, normalised —
     `wbsearchentities` is a fuzzy search and will happily offer a near
     neighbour when nothing matches
  5. no book we hold for that author predates their birth

Rules 1 and 2 do most of the work. Searching "Bell Hooks" returns the
writer, a mixtape, a painting by Emma Amos, and two New York Times
articles; only one of the six is a human who writes. Rule 3 is what makes
a common name fail closed instead of guessing. Rule 4 is the lesson from
today's Fandom cross-check, where a loose title test cheerfully confirmed
"Against the Gods" as a 1976 Allen Drury novel about Akhenaten.

UNAVAILABLE IS NOT NOT_FOUND. This network drops connections regularly —
it did so twice while this script was being designed — and a timeout says
nothing about whether the author exists. Anything that fails to complete
is recorded as unreachable and left for the next run; only a completed
search with no qualifying candidate is written as a negative. `asked` is
tracked apart from `found`, because "we looked and found nothing" and "we
never looked" have been conflated in this repo four times.

Output: data/akinator_authors_search.json — a SEPARATE file, merged after
the P648 harvest so the exact join always wins a disagreement. Gitignored,
regenerable, resumable: re-running only asks about names not yet resolved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
P648_PATH = os.path.join(REPO_ROOT, "data", "akinator_authors_wd.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_authors_search.json")

API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

SHIPPED_BOOKS = 5000
CANDIDATES = 7          # how many search hits to interrogate

GENDER = {"Q6581097": "male", "Q6581072": "female"}

# A human who writes. Anything outside this set is not evidence that the
# person we found is the person who wrote our book — the point of the
# filter is to reject the actor, the footballer and the mixtape.
WRITING_OCCUPATIONS = {
    "Q36180",      # writer
    "Q482980",     # author
    "Q6625963",    # novelist
    "Q49757",      # poet
    "Q11774202",   # essayist
    "Q6673651",    # essayist (alt)
    "Q1930187",    # journalist
    "Q28389",      # screenwriter
    "Q214917",     # playwright
    "Q333634",     # translator
    "Q13570226",   # translator (alt)
    "Q201788",     # historian
    "Q4964182",    # philosopher
    "Q1607826",    # editor
    "Q3499072",    # editor (alt)
    "Q15980158",   # non-fiction writer
    "Q18939491",   # children's writer
    "Q12144794",   # children's writer (alt)
    "Q11569986",   # poet (alt)
    "Q7042855",    # non-fiction writer (alt)
    "Q644687",     # illustrator — picture books are books, and their
                   # illustrator is often the credited author
    # Kept despite being weak evidence on their own: this corpus is full of
    # textbooks, and their authors are routinely catalogued ONLY as
    # academics. Safe here because rules 3 and 4 still have to hold — a
    # common name shared with any other qualifying human fails closed.
    "Q1622272",    # university teacher
    "Q3400985",    # academic
    # DELIBERATELY ABSENT, having been in the first draft by mistake:
    # Q1281618 sculptor, Q245068 comedian, Q2526255 film director. None of
    # them implies the person wrote anything, and this filter exists to
    # reject exactly that — the search for "Bell Hooks" offers a mixtape, a
    # painting and two newspaper articles before it offers the writer.
    # Dropping them cost 0 of the 19 matches in the 25-author smoke test.
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def norm_name(s: str) -> str:
    """Compare names without punctuation, case or diacritics.

    Deliberately NOT features.normalize(), which strips trailing genre
    qualifiers — harmless on a subject, wrong on a person called
    "Someone General".
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _PUNCT.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


def same_person_name(a: str, b: str) -> bool:
    """Normalised equality, then again with whitespace removed entirely.

    A space is not identity-bearing in CJK: Open Library writes 太宰 治 and
    Wikidata's alias is 太宰治, and on the first pass those compared unequal.
    That matters more than it looks — Japan, Korea and China are precisely
    the coverage this harvest exists to reach (China stands at ONE author).
    Removing spaces also merges "Jo Ann" with "Joann", which is the same
    person, and cannot merge two different names that differ by anything
    other than spacing.
    """
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na.replace(" ", "") == nb.replace(" ", "")


class Unavailable(Exception):
    """The lookup did not complete. Never a finding about the author."""


def _get(params: dict, attempts: int = 3, timeout: int = 40) -> dict:
    params = {**params, "format": "json"}
    url = API + "?" + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 429:
                time.sleep(10 + attempt * 10)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
        if attempt < attempts - 1:
            time.sleep(3 + attempt * 4)
    raise Unavailable(last or "unknown")


def _claim_ids(entity: dict, prop: str) -> list[str]:
    out = []
    for c in (entity.get("claims") or {}).get(prop, []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(v["id"])
    return out


def _claim_time_year(entity: dict, prop: str) -> int | None:
    for c in (entity.get("claims") or {}).get(prop, []):
        t = (((c.get("mainsnak") or {}).get("datavalue") or {})
             .get("value") or {}).get("time")
        if t and len(t) > 5:
            try:
                return int(t[1:5])
            except ValueError:
                pass
    return None


def resolve(name: str, earliest_book_year: int | None) -> tuple[dict | None, str]:
    """(record, reason). Raises Unavailable if the lookup did not complete."""
    hits = _get({"action": "wbsearchentities", "search": name,
                 "language": "en", "uselang": "en", "type": "item",
                 "limit": CANDIDATES}).get("search") or []
    if not hits:
        return None, "no candidates"

    # Rule 4 first, and before spending an entity call: keep only hits whose
    # MATCHED TEXT is this name. wbsearchentities is fuzzy by design.
    named = [h for h in hits
             if same_person_name((h.get("match") or {}).get("text", ""), name)
             or same_person_name(h.get("label", ""), name)]
    if not named:
        return None, f"no exact-name candidate among {len(hits)}"

    ents = _get({"action": "wbgetentities",
                 "ids": "|".join(h["id"] for h in named[:CANDIDATES]),
                 "props": "claims|labels", "languages": "en"}
                ).get("entities") or {}

    qualified = []
    for h in named:
        e = ents.get(h["id"])
        if not e:
            continue
        if "Q5" not in _claim_ids(e, "P31"):
            continue                                   # rule 1
        occ = _claim_ids(e, "P106")
        if not (set(occ) & WRITING_OCCUPATIONS):
            continue                                   # rule 2
        qualified.append((h["id"], e, occ))

    if not qualified:
        return None, f"none of {len(named)} is a human who writes"
    if len(qualified) > 1:                             # rule 3
        return None, ("ambiguous: " +
                      ", ".join(q[0] for q in qualified[:4]))

    qid, ent, occ = qualified[0]
    birth = _claim_time_year(ent, "P569")
    death = _claim_time_year(ent, "P570")

    # Rule 5. A book cannot predate its author. This is cheap and it is the
    # only check here that uses what WE know rather than what Wikidata says.
    if birth and earliest_book_year and earliest_book_year < birth:
        return None, (f"rejected: our earliest book is {earliest_book_year}, "
                      f"{qid} was born {birth}")

    countries = []
    for c in _claim_ids(ent, "P27"):
        countries.append(c)
    gender = next((GENDER[g] for g in _claim_ids(ent, "P21")
                   if g in GENDER), None)
    return ({"qid": qid, "gender": gender, "country_qids": countries,
             "occupations": occ, "birth": birth, "death": death,
             "matched_label": ((ent.get("labels") or {}).get("en") or {})
                              .get("value", "")}, "ok")


def load_targets() -> list[tuple[str, str, int, int | None]]:
    """(author_key, name, book_count, earliest_year) for authors P648 missed,
    most-published-here first."""
    with open(P648_PATH, encoding="utf-8") as fh:
        p648 = json.load(fh)
    have = {k for k, v in p648.items() if isinstance(v, dict) and v.get("qid")}

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
    docs = docs[:SHIPPED_BOOKS]

    count: dict[str, int] = {}
    name: dict[str, str] = {}
    earliest: dict[str, int] = {}
    for d in docs:
        yr = d.get("first_publish_year")
        for k, n in zip(d.get("author_key") or [], d.get("author_name") or []):
            count[k] = count.get(k, 0) + 1
            name.setdefault(k, n)
            if isinstance(yr, int) and yr > 0:
                earliest[k] = min(earliest.get(k, yr), yr)

    out = [(k, name[k], count[k], earliest.get(k))
           for k in count if k not in have and name.get(k)]
    out.sort(key=lambda t: -t[2])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--limit", type=int, default=0,
                    help="only the N most-published unresolved authors")
    ap.add_argument("--delay", type=float, default=0.35)
    args = ap.parse_args()

    targets = load_targets()
    store = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            store = json.load(fh)

    # Resume: skip anything already decided. An UNAVAILABLE is NOT decided —
    # that is the whole point of keeping it as its own state.
    pending = [t for t in targets
               if store.get(t[0], {}).get("status") in (None, "unavailable")]
    if args.limit:
        pending = pending[:args.limit]

    print(f"{len(targets)} authors unresolved by P648; "
          f"{len(pending)} to ask about now\n")

    found = notfound = unavailable = 0
    for i, (key, name, books, earliest) in enumerate(pending, 1):
        try:
            rec, why = resolve(name, earliest)
        except Unavailable as exc:
            unavailable += 1
            store[key] = {"status": "unavailable", "name": name,
                          "reason": str(exc)}
            print(f"  [{i}/{len(pending)}] ?  {name[:34]:<34} "
                  f"UNAVAILABLE ({exc}) — will retry")
        else:
            if rec:
                found += 1
                store[key] = {"status": "found", "name": name,
                              "books": books, **rec}
                print(f"  [{i}/{len(pending)}] ok {name[:34]:<34} "
                      f"{rec['qid']:<12} {rec['matched_label'][:26]:<26} "
                      f"({books} books)")
            else:
                notfound += 1
                store[key] = {"status": "not_found", "name": name,
                              "books": books, "reason": why}
                print(f"  [{i}/{len(pending)}] -  {name[:34]:<34} {why[:44]}")

        if i % 25 == 0 or i == len(pending):
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(store, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)

    asked = found + notfound + unavailable
    print(f"\n  asked {asked}: {found} found, {notfound} no match, "
          f"{unavailable} unreachable")
    if asked:
        print(f"  match rate on COMPLETED lookups: "
              f"{100*found/max(1, found+notfound):.0f}% "
              f"({found}/{found+notfound})")
    gained = sum(v.get("books", 0) for v in store.values()
                 if v.get("status") == "found")
    print(f"  book-slots that gain author facts: {gained}")
    if unavailable:
        print(f"  {unavailable} left UNAVAILABLE — re-run to retry those only")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
