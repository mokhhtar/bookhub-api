"""
scripts/akinator/census_fandom_novels.py — a census of novel wikis on
Fandom, using the ONE real enumeration source found for this project:
`Module:WikiData` on wikis.fandom.com, a community-maintained Lua data
table covering ~9,900 Fandom wikis (name, subdomain, language, article
count). Verified live 2026-08-14 — this is not a documented API and
carries no official guarantee, but it exists, it is current (its own
`__UPDATE__` field was dated the same day this was fetched), and no
alternative was found: Fandom's own `Wikis/List` API answers 403,
Wikidata's P1811 covers 6 literary works after type-filtering, and
Fandom's hub-browsing pages (`fandom.com/wikis/*`) are dead.

    python scripts/akinator/census_fandom_novels.py --refresh-module
    python scripts/akinator/census_fandom_novels.py --limit 15   # sample
    python scripts/akinator/census_fandom_novels.py              # the 279

PHASE A ONLY: `Category:Books_hub` on wikis.fandom.com (279 members,
Fandom's own classification) cross-referenced against the module cache
for instant subdomain/page-count lookup. This is a small, high-precision
slice of the ~9,900-wiki module, not the whole thing — a genuinely broad
sweep (phase B) is a separate, much longer-running undertaking flagged
in the owner's plan and not attempted here.

WHY A SEPARATE MODULE CACHE FILE. `Module:WikiData` is ~1.8M characters
of Lua and this network drops mid-download unpredictably (confirmed:
three attempts needed for a clean fetch while researching this). Fetched
once, cached, and reused — `--refresh-module` forces a re-fetch when the
cached copy's own `day` field looks stale.

THE CLASSIFICATION PROBLEM THE MODULE DOES NOT SOLVE. Its five fields
(wiki, host, link, language, pages) carry no type/genre information —
verified directly against the full text. So "novel vs. game vs. TV
series" has to be read from each wiki's own MediaWiki category structure,
exactly the technique `prove_fandom.py`'s `signal_content()` already
uses and tested yesterday. That function is REUSED here via its
`_WORK_CATEGORY` / `_GENERIC_CATEGORY` patterns, extended with an
explicit exclusion set for other-medium categories — not reimplemented.

FOUR STATES, NOT TWO, because a binary novel/not-novel call is
structurally wrong here: some works are genuinely multi-medium (Harry
Potter's wiki is full of film content too, and excluding it for that
would be exactly backwards — it started as, and still is, a novel
series). A wiki earns:

  NOVEL                strong narrative-work categories, no strong
                        other-medium signal
  LITERARY_MULTI_MEDIA  both work-shaped AND other-medium categories,
                        BUT an explicit "Books"/"Novels" category with
                        real membership settles the origin medium —
                        this is a novel series with adaptations
                        (Warriors, Dune, Agatha Christie), safe to treat
                        as a novel result
  MULTI_MEDIA           both signals present, no explicit book category
                        to settle origin medium — genuinely ambiguous,
                        needs a human look, not an automatic keep or drop
  NOT_NOVEL             strong other-medium signal, no narrative-work
                        categories

Split from an original three-state design (NOVEL/MULTI_MEDIA/NOT_NOVEL)
after a 100-wiki hand review the owner ran in parallel (2026-08-16)
found real novel serieses — Warriors, Dune, Agatha Christie, Nancy Drew —
sitting in the undifferentiated MULTI_MEDIA bucket even though each
wiki's own category structure already said "Books"/"Novels" outright.
This mirrors the CONFIRMED/LIKELY/AMBIGUOUS discipline `prove_fandom.py`
already established: never collapse "needs a look" into a firm yes or no.

Output: data/fandom_novel_census.csv — title, subdomain, pages, type,
evidence. Read-only with respect to the pipeline's existing outputs:
never touches akinator_fandom.json or tools/fandom.py's FANDOM_WIKIS.
What to do with a NOVEL result is the owner's call, same as every other
harvest in this project.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows console codepages (e.g. cp1256 for Arabic locales) can't encode
# every wiki title this script prints (diacritics like "ū"); reconfigure
# to UTF-8 with replacement rather than crash mid-run on a print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from prove_fandom import (  # noqa: E402
    _GENERIC_CATEGORY,
    _WORK_CATEGORY,
    MIN_ARTICLES,
    Unavailable,
    _fetch,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE_CACHE = os.path.join(REPO_ROOT, "data", "fandom_wikidata_module.json")
OUT_CSV = os.path.join(REPO_ROOT, "data", "fandom_novel_census.csv")
OUT_JSON = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}
WIKIS_WIKI_API = "https://wikis.fandom.com/api.php"

# Named for a work Fandom itself would call "not a novel" -- verified
# distinct from _WORK_CATEGORY (character/chapter/faction... categories
# any narrative-work wiki grows) rather than a rewrite of it.
#
# "cast" and bare "games?" were both cut after live false positives on
# Shadow Slave, a pure web novel: "Main Cast" is the story's own
# character roster, not a film/TV cast list, and "Ariel's Game" is a
# named in-story arc, not a video-game adaptation. Both are common
# English words used in their everyday sense, not signals of another
# medium -- the pattern keeps only phrasings specific enough that a
# narrative-work wiki would not organically produce them. "actors"/
# "voice actors" stay: a novel wiki essentially never has real actors
# unless adapted to screen. "video games?" stays as the qualified,
# unambiguous form of the term "games?" had to lose.
_OTHER_MEDIUM_CATEGORY = re.compile(
    r"\b(video ?games?|tv ?series|television|anime|manga|films?"
    r"|movies?|comics?|soundtracks?|voice ?actors?|actors?"
    r"|episodes? guide|seasons?)\b", re.IGNORECASE)

# A wiki with BOTH work-shaped and other-medium categories is genuinely
# ambiguous by content alone -- Harry Potter's wiki is full of film
# content and is still correctly a novel wiki. Found live in a 100-wiki
# hand review the owner ran in parallel (2026-08-16): Warriors (Books,
# 358 members), Dune (Novels, 44), Agatha Christie (Novels, 90), Nancy
# Drew (Books, 586) all landed in the old undifferentiated MULTI_MEDIA
# bucket despite each wiki stating outright, via its own category
# structure, that it is a book series first. An explicit "Books"/
# "Novels" category with real membership is a categorically stronger,
# more specific signal than the generic _WORK_CATEGORY hits (which also
# fire for any narrative work regardless of origin medium) -- worth
# distinguishing from the wikis where no such category exists and origin
# medium genuinely can't be told from category structure alone.
_BOOK_CATEGORY = re.compile(
    r"\b(books?|novels?|novellas?|light[- ]?novels?|web[- ]?novels?"
    r"|bibliography)\b", re.IGNORECASE)

# Name-level exclusion, checked BEFORE any network call -- classify()
# costs two live API requests per wiki, and these two shapes are wrong
# from the wiki's own *scope*, not from its category contents, so no
# amount of category evidence would ever rescue them.
#
# Found live sampling the 15 largest Books_hub members (2026-08-14):
# "Wings of Fire Fanon Wiki" and "Percy Jackson Fanfiction Wiki" are fan
# fiction about a novel, not the novel's own wiki -- they carry the same
# work-shaped categories (characters, chapters) as the real thing and
# would classify NOVEL by content alone. Excluded by scope, not content.
_FANON_NAME = re.compile(
    r"\b(fanon|fan[- ]?fiction|fanfic|oc'?s?)\b", re.IGNORECASE)

# Found live same sampling: "Alien Species" documents creatures pulled
# from novels, films, AND games indiscriminately -- a cross-media
# reference work, not a novel wiki. "Star Wars Saga Edition Wiki" is a
# tabletop-RPG rulebook wiki (d20-based), not a novel at all. Both would
# otherwise need a human to notice the wiki's own name already gives it
# away. Deliberately narrow: ordinary book titles like "Fire Emblem" or
# "Wings of Fire" don't contain any of these words, so they pass through
# untouched.
_OTHER_MEDIUM_NAME = re.compile(
    r"\b(species|database|directory|reference"
    r"|rpg|rulebook|d20|tabletop|saga edition)\b", re.IGNORECASE)


def excluded_by_name(name: str) -> str | None:
    """Return an exclusion reason if `name` (the wiki's own title) rules
    it out before spending any API call on classify(), else None."""
    if _FANON_NAME.search(name):
        return "fan fiction/fanon wiki (scope, not a novel's own wiki)"
    if _OTHER_MEDIUM_NAME.search(name):
        return "cross-media reference / RPG-rulebook wiki, not a novel"
    return None


def load_module(force_refresh: bool) -> dict[str, dict]:
    """The ~9,900-entry Module:WikiData cache, fetched once and reused.

    Its own `__UPDATE__.day` field is the freshness signal -- refetch
    only when explicitly asked or the cache is missing, never silently,
    since a fresh fetch costs a multi-megabyte download this network
    drops unpredictably (three attempts were needed once already).
    """
    if os.path.exists(MODULE_CACHE) and not force_refresh:
        with open(MODULE_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        if data:
            return data

    print("Fetching Module:WikiData (~1.8M chars; this network drops "
          "mid-download -- retrying is normal here)...")
    q = urllib.parse.urlencode({
        "action": "parse", "page": "Module:WikiData", "prop": "wikitext",
        "format": "json"})
    url = f"{WIKIS_WIKI_API}?{q}"
    text = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            d = json.loads(raw.decode("utf-8"))
            text = d["parse"]["wikitext"]["*"]
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  attempt {attempt+1} failed: {type(exc).__name__} "
                  f"{str(exc)[:60]} -- retrying")
            time.sleep(3)
    if text is None:
        raise Unavailable("could not fetch Module:WikiData after 5 attempts")

    pat = re.compile(
        r'\["(?P<key>(?:[^"\\]|\\.)*)"\]\s*=\s*\{\s*'
        r'wiki\s*=\s*"(?P<wiki>(?:[^"\\]|\\.)*)",\s*'
        r'host\s*=\s*"(?P<host>(?:[^"\\]|\\.)*)",\s*'
        r'link\s*=\s*"(?P<link>(?:[^"\\]|\\.)*)",\s*'
        r'language\s*=\s*"(?P<language>(?:[^"\\]|\\.)*)",\s*'
        r"pages\s*=\s*(?P<pages>\d+)\s*\}",
        re.S)
    entries = {m.group("key"): {
        "wiki": m.group("wiki"), "host": m.group("host"),
        "link": m.group("link"), "language": m.group("language"),
        "pages": int(m.group("pages"))}
        for m in pat.finditer(text)}
    print(f"  parsed {len(entries)} wiki entries")
    with open(MODULE_CACHE, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False)
    return entries


def books_hub_members() -> list[str]:
    q = urllib.parse.urlencode({
        "action": "query", "list": "categorymembers",
        "cmtitle": "Category:Books_hub", "cmlimit": 500, "format": "json"})
    d = _fetch(f"{WIKIS_WIKI_API}?{q}")
    return [m["title"] for m in (d.get("query") or {}).get("categorymembers", [])]


def classify(subdomain: str) -> tuple[str, dict]:
    """NOVEL / MULTI_MEDIA / NOT_NOVEL / UNAVAILABLE, from the wiki's own
    category structure -- the same random-sample + allcategories technique
    `prove_fandom.py.signal_content` already uses, reused via its own
    _WORK_CATEGORY / _GENERIC_CATEGORY patterns rather than duplicated.
    """
    try:
        d = _fetch(f"https://{subdomain}.fandom.com/api.php?" +
                   urllib.parse.urlencode({
                       "action": "query", "generator": "random",
                       "grnnamespace": 0, "grnlimit": 12,
                       "prop": "categories", "cllimit": 200, "format": "json"}))
    except Unavailable as exc:
        return "UNAVAILABLE", {"reason": str(exc)}

    cats: list[str] = []
    for page in (d.get("query") or {}).get("pages", {}).values():
        for c in page.get("categories") or []:
            cats.append((c.get("title") or "").split(":", 1)[-1])

    # acprop=size, so an exclusion signal can require real ARTICLE
    # members, not a stray single file. Found live: Shadow Slave -- a
    # pure web novel -- carries a category literally named "Comic" whose
    # only member is `File:Comic 1.png`, one image, zero article pages.
    # Matched by the same "comics?" pattern that correctly flags a real
    # manhwa-adaptation category elsewhere, and there is no way to tell
    # the two apart by NAME alone -- only by what is actually filed
    # under it. A real adaptation-content category has real pages.
    sized: dict[str, int] = {}
    try:
        d2 = _fetch(f"https://{subdomain}.fandom.com/api.php?" +
                    urllib.parse.urlencode({
                        "action": "query", "list": "allcategories",
                        "acprop": "size", "aclimit": 500, "format": "json"}))
        for c in (d2.get("query") or {}).get("allcategories") or []:
            name = c.get("*", "")
            if name:
                sized[name] = c.get("pages", 0)
                cats.append(name)
    except Unavailable:
        pass   # the random-page sample alone is enough to attempt a call

    # Crossover-database check, BEFORE any work/other-medium scoring --
    # found live (owner's hand review, 2026-08-16) against two wikis that
    # otherwise scored NOVEL/LITERARY_MULTI_MEDIA on category-name content
    # alone: "Yuna's Princess adventure" (really a giant Heroes/Villains
    # crossover wiki -- 29 distinct `<X> Heroes`/`<X> Villains` categories
    # including "Batman Heroes", "Adult Swim Heroes") and "Non-alien
    # Creatures" (a cryptid/monster compendium -- 126 distinct `<X>
    # Universe` categories). Both carry incidental "Characters" and even
    # "Books" categories from the many unrelated franchises they catalog,
    # which fooled the per-category signals above. A wiki about ONE work
    # never organically produces 3+ *different* franchise-tagged
    # Heroes/Villains/Universe categories -- at most one, self-referential.
    # This reuses the SAME `sized` allcategories fetch already made above,
    # no extra network call.
    _franchise_tag = re.compile(r"\b(heroes|villains|universe)$", re.IGNORECASE)
    franchise_tags = {c for c in sized
                      if _franchise_tag.search(c) and sized.get(c, 0) >= 5}
    if len(franchise_tags) >= 3:
        return "NOT_NOVEL", {
            "reason": "crossover database (Heroes/Villains/Universe "
                      "categories span many unrelated franchises)",
            "franchise_tag_sample": sorted(franchise_tags)[:8]}

    cats = [c.strip() for c in dict.fromkeys(cats) if c and c.strip()]
    specific = [c for c in cats if not _GENERIC_CATEGORY.match(c)]
    work_hits = [c for c in specific if _WORK_CATEGORY.search(c)]
    other_hits = [c for c in specific if _OTHER_MEDIUM_CATEGORY.search(c)
                  # sized.get(c, 1) defaults to 1 (assume real) for a
                  # category found only via the random-page sample, which
                  # never carries a member count -- absence of size data
                  # is not evidence the category is empty.
                  and sized.get(c, 1) >= 2]
    # Same size-floor reasoning as other_hits: require real membership,
    # not a stub category someone created and never filled.
    book_hits = [c for c in specific if _BOOK_CATEGORY.search(c)
                 and sized.get(c, 1) >= 3]

    evidence = {"categories_seen": len(cats), "specific": len(specific),
               "work_shaped_sample": work_hits[:6],
               "other_medium_sample": other_hits[:6],
               "book_category_sample": book_hits[:6]}

    if work_hits and not other_hits:
        return "NOVEL", evidence
    if work_hits and other_hits and book_hits:
        # Both signals present, but an explicit Books/Novels category
        # with real membership settles the origin medium -- this is a
        # novel series that also has adaptations (Warriors, Dune,
        # Agatha Christie), not a genuinely ambiguous cross-media wiki.
        return "LITERARY_MULTI_MEDIA", evidence
    if work_hits and other_hits:
        # Ambiguous by content alone -- no explicit book category found
        # to settle origin medium. Needs a human look, same discipline
        # as before this split.
        return "MULTI_MEDIA", evidence
    if other_hits and not work_hits:
        return "NOT_NOVEL", evidence
    return "NOT_NOVEL", evidence   # neither signal: too thin to call a novel


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-module", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample-random", type=int, default=0,
                    help="take a random sample of this size instead of "
                         "the --limit largest-first slice, so a preview "
                         "run sees typical small/mid wikis, not only the "
                         "extreme cases the top-of-list already covers")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed for --sample-random, for a reproducible sample")
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--min-pages", type=int, default=MIN_ARTICLES,
                    help=f"skip wikis below this article count "
                         f"(default {MIN_ARTICLES}, matching prove_fandom.py)")
    args = ap.parse_args()

    module = load_module(args.refresh_module)
    members = books_hub_members()
    print(f"\nBooks_hub: {len(members)} members; module cache: "
          f"{len(module)} entries\n")

    targets = []
    unmatched = []
    for name in members:
        entry = module.get(name)
        if not entry:
            unmatched.append(name)
            continue
        if entry["pages"] < args.min_pages:
            continue
        targets.append((name, entry["link"], entry["pages"]))
    targets.sort(key=lambda t: -t[2])   # richest wikis first
    if args.sample_random:
        rng = random.Random(args.seed)
        targets = rng.sample(targets, min(args.sample_random, len(targets)))
    elif args.limit:
        targets = targets[:args.limit]

    print(f"{len(unmatched)} Books_hub member(s) not found in the module "
          f"cache (name mismatch -- skipped, not counted as NOT_NOVEL)")
    print(f"Classifying {len(targets)} wiki(s) with >= {args.min_pages} "
          f"articles\n")

    results = []
    for name, sub, pages in targets:
        reason = excluded_by_name(name)
        if reason:
            status, ev = "NOT_NOVEL", {"reason": reason, "excluded_by_name": True}
        else:
            status, ev = classify(sub)
        results.append({"title": name.removesuffix(" Wiki").removesuffix(" Wikia"),
                        "wiki_name": name, "subdomain": sub, "pages": pages,
                        "type": status, "evidence": ev})
        print(f"  [{status:<20}] {name[:40]:<40} {pages:>6} pages")
        time.sleep(args.delay)

    tally: dict[str, int] = {}
    for r in results:
        tally[r["type"]] = tally.get(r["type"], 0) + 1
    print("\n" + "=" * 60)
    for k in ("NOVEL", "LITERARY_MULTI_MEDIA", "MULTI_MEDIA", "NOT_NOVEL",
             "UNAVAILABLE"):
        if tally.get(k):
            print(f"  {k:<20} {tally[k]:>4}")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "subdomain", "url", "pages", "type",
                   "work_shaped", "other_medium", "book_category"])
        for r in results:
            w.writerow([r["title"], r["subdomain"],
                       f"https://{r['subdomain']}.fandom.com/",
                       r["pages"], r["type"],
                       ";".join(r["evidence"].get("work_shaped_sample", [])),
                       ";".join(r["evidence"].get("other_medium_sample", [])),
                       ";".join(r["evidence"].get("book_category_sample", []))])
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump({"results": results}, fh, ensure_ascii=False, indent=1)

    novels = [r for r in results
             if r["type"] in ("NOVEL", "LITERARY_MULTI_MEDIA")]
    print(f"\n{len(novels)} classified NOVEL or LITERARY_MULTI_MEDIA. Not "
          f"written to akinator_fandom.json or FANDOM_WIKIS -- for review.")
    if novels:
        print("\nsample:")
        for r in novels[:15]:
            print(f"   {r['title'][:44]:<44} https://{r['subdomain']}.fandom.com/")
    print(f"\n-> {OUT_CSV}\n-> {OUT_JSON}")


if __name__ == "__main__":
    main()
