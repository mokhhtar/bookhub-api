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
  4. the label or alias that matched IS the queried name, exactly — never
     a near neighbour (structural since the batch rewrite: the name is the
     literal the query matches on, so nothing fuzzy can come back)
  5. no book we hold for that author predates their birth

Rules 1 and 2 do most of the work. Searching "Bell Hooks" returns the
writer, a mixtape, a painting by Emma Amos, and two New York Times
articles; only one of the six is a human who writes. Rule 3 is what makes
a common name fail closed instead of guessing. Rule 4 is the lesson from
today's Fandom cross-check, where a loose title test cheerfully confirmed
"Against the Gods" as a 1976 Allen Drury novel about Akhenaten.

ASKED IN BATCHES, VIA SPARQL, AND NOT ONE NAME AT A TIME. The first
version called `wbsearchentities` per author, and it ran at roughly ONE
AUTHOR PER MINUTE — thirty hours for this list. The cause was not
Wikidata and not the algorithm: timing the raw calls showed two requests
in three failing with a connection timeout after ~22 seconds, while a
request that connected returned in 0.8s. This machine's network drops
connections constantly (the same fact that forces NO_PROXY='*' on every
httpx script here), so the fix is not faster retries, it is FEWER
REQUESTS.

One SPARQL query resolves 150 names at once: 38 rows in 4.0 seconds for
11 names in testing, against ~60 seconds per name through the search API.
It also makes rule 4 structural rather than a post-check — the name is
the VALUES literal matched against `rdfs:label|skos:altLabel`, so a
result IS an exact name match and cannot be a fuzzy neighbour. Rules 1
and 2 move into the query as `wdt:P31 wd:Q5` and a required `wdt:P106`.

Verified to reproduce the per-name results exactly on the smoke-test set,
including both refusals: Alvin Schwartz comes back with three items and
Scott Cunningham with five, so both still fail rule 3, and Mojang returns
no row at all because it is not a human.

UNAVAILABLE IS NOT NOT_FOUND. A timeout says nothing about whether the
author exists. Anything that fails to complete is recorded as unreachable
and left for the next run; only a completed lookup with no qualifying
candidate is written as a negative. `asked` is tracked apart from
`found`, because "we looked and found nothing" and "we never looked" have
been conflated in this repo four times. With batching this matters more,
not less: one dropped query would otherwise bury 150 names as absent.

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

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
    "Accept": "application/sparql-results+json",
}

SHIPPED_BOOKS = 5000

# 150 names per query returned in ~4s in testing. WDQS times out at one
# minute and a timeout loses the WHOLE batch, so this stays conservative —
# the same reasoning as harvest_works.py's smaller batch.
BATCH = 150

# Rules 1 and 2 live here, server-side: a human, with an occupation. The
# occupation VALUE is filtered in Python against WRITING_OCCUPATIONS, which
# keeps the whitelist in one readable place instead of inside a query
# string. rdfs:label|skos:altLabel is what lets Open Library's 太宰 治 reach
# an item whose English label is "Osamu Dazai".
QUERY = """SELECT ?nm ?item ?occ ?gender ?birth ?death ?country WHERE {
  VALUES ?nm { %s }
  ?item rdfs:label|skos:altLabel ?nm .
  ?item wdt:P31 wd:Q5 .
  ?item wdt:P106 ?occ .
  OPTIONAL { ?item wdt:P21  ?gender }
  OPTIONAL { ?item wdt:P27  ?country }
  OPTIONAL { ?item wdt:P569 ?birth }
  OPTIONAL { ?item wdt:P570 ?death }
}"""

# Which language tags to try a name under. A label is stored per language,
# so "太宰 治" is only findable as @ja. Latin-script names are asked as @en,
# which is the language nearly every person item carries.
_SCRIPTS = [
    (0x4E00, 0x9FFF, ["ja", "zh", "ko"]),      # CJK ideographs
    (0x3040, 0x30FF, ["ja"]),                  # kana
    (0xAC00, 0xD7AF, ["ko"]),                  # hangul
    (0x0400, 0x04FF, ["ru", "uk"]),            # cyrillic
    (0x0600, 0x06FF, ["ar", "fa"]),            # arabic
    (0x0590, 0x05FF, ["he"]),                  # hebrew
    (0x0370, 0x03FF, ["el"]),                  # greek
]


def lang_tags(name: str) -> list[str]:
    tags = ["en"]
    for ch in name:
        cp = ord(ch)
        for lo, hi, langs in _SCRIPTS:
            if lo <= cp <= hi:
                for lg in langs:
                    if lg not in tags:
                        tags.append(lg)
    return tags

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


def _query(names: list[str], attempts: int = 4, timeout: int = 75) -> list[dict]:
    """One SPARQL round trip for many names. Raises Unavailable, never lies.

    Retries are short and immediate rather than backed off: the failure
    mode measured on this network is a connection that never establishes,
    not a server pushing back, and sleeping does not make a dropped
    connection more likely to succeed.
    """
    values = " ".join(
        f"{json.dumps(n, ensure_ascii=False)}@{tag}"
        for n in names for tag in lang_tags(n))
    url = ENDPOINT + "?" + urllib.parse.urlencode(
        {"query": QUERY % values, "format": "json"})
    last = ""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)["results"]["bindings"]
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 429:
                time.sleep(15)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < attempts - 1:
            time.sleep(1.5)
    raise Unavailable(last or "unknown")


def _year(literal: str | None) -> int | None:
    if literal and len(literal) > 5:
        try:
            return int(literal[1:5]) if literal[0] in "+-" else int(literal[:4])
        except ValueError:
            return None
    return None


def _fold(rows: list[dict]) -> dict[str, dict]:
    """SPARQL returns one row per (item x occupation x country x ...).

    Collapse to one record per (name, item), keeping sets — a person
    routinely has several occupations and several citizenships, and
    assignment instead of accumulation would keep whichever came last.
    """
    out: dict[str, dict] = {}
    for b in rows:
        nm = b["nm"]["value"]
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        rec = out.setdefault(f"{nm}\x00{qid}", {
            "name": nm, "qid": qid, "occupations": set(),
            "country_qids": set(), "gender": None,
            "birth": None, "death": None,
        })
        if "occ" in b:
            rec["occupations"].add(b["occ"]["value"].rsplit("/", 1)[-1])
        if "country" in b:
            rec["country_qids"].add(b["country"]["value"].rsplit("/", 1)[-1])
        if "gender" in b:
            g = GENDER.get(b["gender"]["value"].rsplit("/", 1)[-1])
            if g:
                rec["gender"] = g
        for key, prop in (("birth", "birth"), ("death", "death")):
            if prop in b and rec[key] is None:
                rec[key] = _year(b[prop]["value"])
    return out


def decide(name: str, candidates: list[dict],
           earliest_book_year: int | None) -> tuple[dict | None, str]:
    """Rules 2, 3 and 5 over one name's candidates. Rules 1 and 4 already
    held: the query demanded a human, and matched the name exactly."""
    if not candidates:
        return None, "no candidate is a human with an occupation"

    qualified = [c for c in candidates
                 if c["occupations"] & WRITING_OCCUPATIONS]      # rule 2
    if not qualified:
        return None, f"none of {len(candidates)} is a human who writes"
    if len(qualified) > 1:                                       # rule 3
        return None, "ambiguous: " + ", ".join(
            sorted(c["qid"] for c in qualified)[:4])

    c = qualified[0]
    # Rule 5. A book cannot predate its author — the only check here that
    # uses what WE hold rather than what Wikidata says.
    if c["birth"] and earliest_book_year and earliest_book_year < c["birth"]:
        return None, (f"rejected: our earliest book is {earliest_book_year}, "
                      f"{c['qid']} was born {c['birth']}")

    return ({"qid": c["qid"], "gender": c["gender"],
             "country_qids": sorted(c["country_qids"]),
             "occupations": sorted(c["occupations"]),
             "birth": c["birth"], "death": c["death"],
             "matched_label": c["name"]}, "ok")


def label_countries(store: dict) -> int:
    """Turn P27 QIDs into the country NAMES the rest of the pipeline uses.

    `author_traits.py` compares against strings like "United States" and
    "Canada", so an unlabelled QID would silently answer every nationality
    question as unknown — the harvest would look complete and teach the
    game nothing. Labels are cached on disk: they are stable, and there is
    no reason to re-ask Wikidata for "Q30" on every run.
    """
    cache_path = os.path.join(REPO_ROOT, "data", "akinator_country_labels.json")
    labels = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            labels = json.load(fh)

    need = sorted({q for v in store.values()
                   if v.get("status") == "found"
                   for q in (v.get("country_qids") or [])
                   if q not in labels})
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        for attempt in range(4):
            try:
                url = ("https://www.wikidata.org/w/api.php?"
                       + urllib.parse.urlencode({
                           "action": "wbgetentities", "ids": "|".join(chunk),
                           "props": "labels", "languages": "en",
                           "format": "json"}))
                req = urllib.request.Request(url, headers={
                    "User-Agent": HEADERS["User-Agent"]})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    ents = json.load(resp).get("entities") or {}
                for q, e in ents.items():
                    labels[q] = (((e.get("labels") or {}).get("en") or {})
                                 .get("value") or q)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2 + attempt * 2)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(labels, fh, ensure_ascii=False, indent=1)

    filled = 0
    for v in store.values():
        if v.get("status") != "found":
            continue
        named = [labels[q] for q in (v.get("country_qids") or []) if q in labels]
        if named != v.get("countries"):
            v["countries"] = named
            filled += 1
    return filled


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
    ap.add_argument("--delay", type=float, default=1.5,
                    help="between BATCHES, not between names")
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
    done = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        names = sorted({t[1] for t in chunk})
        try:
            folded = _fold(_query(names))
        except Unavailable as exc:
            # One dropped query must not bury 150 names as absent. Every
            # name in the batch stays retryable.
            for key, name, _books, _e in chunk:
                store[key] = {"status": "unavailable", "name": name,
                              "reason": str(exc)}
            unavailable += len(chunk)
            done += len(chunk)
            print(f"  [{done}/{len(pending)}] BATCH UNAVAILABLE ({exc}) — "
                  f"{len(chunk)} names kept for retry")
            continue

        by_name: dict[str, list[dict]] = {}
        for rec in folded.values():
            by_name.setdefault(rec["name"], []).append(rec)

        for key, name, books, earliest in chunk:
            rec, why = decide(name, by_name.get(name, []), earliest)
            done += 1
            if rec:
                found += 1
                store[key] = {"status": "found", "name": name,
                              "books": books, **rec}
                print(f"  [{done}/{len(pending)}] ok {name[:32]:<32} "
                      f"{rec['qid']:<12} ({books} books)")
            else:
                notfound += 1
                store[key] = {"status": "not_found", "name": name,
                              "books": books, "reason": why}
                print(f"  [{done}/{len(pending)}] -  {name[:32]:<32} {why[:42]}")

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    # Always, even when nothing was pending: a record resolved by an older
    # run may still be carrying bare QIDs.
    filled = label_countries(store)
    if filled:
        print(f"\n  country labels filled in for {filled} author(s)")

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
