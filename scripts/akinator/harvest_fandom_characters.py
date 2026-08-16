"""
scripts/akinator/harvest_fandom_characters.py — the cast, from the wikis.

    python scripts/akinator/harvest_fandom_characters.py --limit 10
    python scripts/akinator/harvest_fandom_characters.py            # resumes

WHY THIS EXISTS. `characters.py` reads Open Library's `person` field, and
that field is the reason character questions work at all for the catalogue
half of the corpus. It is also empty for exactly the books the Fandom work
is about. Measured against the corpus on 2026-08-16, for the three web
novels that appear in BOTH sources:

    The Beginning After the End   0 subjects   0 persons
    Harry Potter and the Methods…  7 subjects   1 person  ('Harry Potter')

Open Library has the row and none of the cast. The wiki has 385 and 1,700
character articles respectively. So this is not enrichment, it is the only
source there is for these books.

STRUCTURED DATA, NOT AN LLM, and that is a deliberate reversal of what
`tools/fandom.py`'s `extract_characters_from_fandom` does. That function
feeds wiki prose to Gemini because it must work for ANY book a visitor
asks about, live, including wikis with no usable category structure. This
runs offline over a known list, so it can use the wiki's own category
graph: a character page IS a character, no extraction step, no
hallucination surface, no quota. The anti-hallucination prompt discipline
that file documents is a mitigation for a risk this path simply does not
take.

THE HARD PART IS NOT FINDING CHARACTERS, IT IS RANKING THEM. Open Library's
`person` lists are SMALL — measured over the 5,103 corpus books that carry
one: median 2, p90 7, p95 14. A Fandom wiki hands you 641 (Worm) or 4,346
(In Death). Dumping those in would hand a few books a name for nearly every
name a player can say, and character questions would collapse into "yes"
for whichever book has the biggest wiki. So the cast has to be cut to the
part a reader would actually name, and that needs an importance signal.

FOUR SIGNALS WERE TRIED LIVE BEFORE THIS ONE WAS KEPT (2026-08-16):

  * `Category:Characters` alone — absent or useless on a third of wikis.
    Lord of Mysteries has none (its cast is under "Book One Characters",
    392 members); Shadow Slave's holds 11 while the real cast lives in
    "Volume N Characters"; Malazan has no character category at all and
    files people under "Males"/"Females".
  * `list=mostlinked` — rejected outright by Fandom's API.
  * `querypage&qppage=Mostlinked` — works, and returns CHAPTERS. Worm's
    top twelve are Scourge, Scarab, Sting, Interlude 19.x… navigation
    templates link every chapter to every other chapter, and that swamps
    any character. A real signal for a question nobody asked.
  * **Article length, over the whole category** — kept. Worm's longest
    character articles are Lisa Wilbourn, The Simurgh, Taylor Hebert,
    Victoria Dallon; In Death's is Eve Dallas; The Beginning After the
    End's second is Arthur Leywin. Protagonists have the most written
    about them, and unlike the three above this holds on every wiki
    tested.

AND IT ONLY WORKS ENUMERATED WHOLE. A first version ranked the first 200
members returned and lost Taylor Hebert — Worm's protagonist — because
`categorymembers` is alphabetical and 'T' falls past 200. `generator=
categorymembers` with `prop=info` returns titles and lengths together,
500 at a time, so the whole category costs 1-2 requests instead of one
per 50 pages. Cheap enough to never sample.

Output: data/akinator_fandom_characters.json (gitignored, regenerable).
Resumable — every book is written as it completes, so an interrupted run
picks up where it stopped. Writes NOTHING to akinator_fandom.json, the
corpus, or tools/fandom.py's FANDOM_WIKIS; wiring these into the game is
a separate, reviewed step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from characters import canonical_name, name_tokens, usable_token  # noqa: E402
from prove_fandom import Unavailable, _fetch                      # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
PROVEN_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_characters.json")

# Asked by exact name first -- one request answers "does this wiki use any
# of the usual names, and how many pages are in each".
#
# "People" was tried and REMOVED. On Malazan -- which has no character
# category at all -- it is the only thing that matched, and its two
# members are Steven Erikson and Ian C. Esslemont: the authors. The
# harvester duly reported the novels' two most important "characters" as
# the men who wrote them. A category named for real people collects real
# people; there is no version of this list that should contain it.
_EXACT_CATEGORIES = ("Characters", "Character", "Main Characters",
                     "Main characters", "Protagonists")

# ...and when none of those carry a real cast, the category list is swept
# for anything character-shaped. This is what rescues the wikis whose
# taxonomy is their own: "Book One Characters" (Lord of Mysteries, 392),
# "Volume 3 Characters" (Shadow Slave, 54).
_CATEGORY_PATTERN = re.compile(
    r"\b(characters?|protagonists?|antagonists?|villains?|heroes)\b",
    re.IGNORECASE)

# A category whose own name says it spans more than this work. Sweeping
# for "characters" on a wiki that catalogs several franchises would
# otherwise pull in every one of them.
_CATEGORY_REJECT = re.compile(
    r"\b(lists?|galler(y|ies)|templates?|images?|files?|stubs?"
    r"|unnamed|unknown|mentioned|deceased)\b", re.IGNORECASE)

# Pages that live in a character category and are not a character. Found
# live: "Parahuman Characters" (Worm — a list article, 75k long, would
# have ranked second), "Character Power Chart" and "Character Reference
# Sheet" (Shadow Slave), "Eve's Fashion - Work" and "Nightmares in Death
# (continued)" (In Death — the wiki files overflow pages beside the cast).
_PAGE_REJECT = re.compile(
    r"\b(list|lists|chart|charts|reference|index|timeline|gallery"
    r"|template|characters|glossary|summary|continued|chapter|episode"
    r"|volume|season|appendix|navigation|cosmology|mythos|wiki)\b",
    re.IGNORECASE)

# THE DISAMBIGUATOR PROBLEM, and it is not the one `characters.py` solves.
# That module strips Open Library's qualifier vocabulary -- "(Fictitious
# character)", "(Spirit)". Fandom's is entirely different, and left alone
# it lands inside the name: the first run produced 'victor frankenstein
# 2025 film', 'peter pan character' and 'hellraiser 1990 tv series' as
# character names. Two different harms in one shape, so two rules.
#
# REJECTED, because the page is about another medium's version of the
# character and not the novel's -- the same medium confusion the census
# spends its whole classification budget avoiding. Found live on the
# Frankenstein wiki: 'Victor Frankenstein (2025 film)', 'Elizabeth
# Lavenza (2025 film)', 'Frankenstein (DC Comics)'; on Peter Pan:
# 'Crocodile (Disney)', 'Captain Hook (Fox)'; on Stephen King:
# 'Carrie White (1976 film)', 'It (films)'.
_ADAPTATION_QUALIFIER = re.compile(
    r"\((?=[^)]*\b(film|films|movie|tv|television|series|comics?|game|games"
    r"|anime|manga|musical|disney|netflix|fox|hbo|\d{4})\b)[^)]*\)",
    re.IGNORECASE)

# STRIPPED, because the qualifier is only there to disambiguate a page
# title and the name underneath is the real one: 'Peter Pan (character)'
# is Peter Pan. Whatever survives both rules is deduped by name, which
# also collapses the several spellings one wiki files separately.
_BENIGN_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")

MAX_MEMBER_PAGES = 6      # 500 members per request; 3,000 is plenty to rank
MIN_ARTICLE_LENGTH = 400  # below this a page is a stub, not a named character

# WHY 8 AND NOT 25, which is what the first run shipped.
#
# The owner asked the right question of it: this is a book-guessing game,
# so what is a twenty-fifth character for? Three answers, none of them
# "more data is better":
#
#   * Character questions are ENDGAME questions. `engine.py` only pools
#     them once belief has narrowed, and then only for names a book still
#     in contention carries. They separate two or three candidates; they
#     never cut the 5,000 down.
#   * A minor character PENALISES the right book. If the book genuinely
#     has character #20 and the player — who does not remember character
#     #20 — answers no, the correct book is scored as contradicted. Depth
#     buys exactly this failure. It is the axis `traits.py` states for
#     itself: "Can a player answer it? … without looking anything up."
#   * `CHAR_ABSENT_CONFIDENCE = 0.06` is documented as calibrated on Open
#     Library's lists being "reasonably complete when present" — lists
#     whose median length is 2 and p90 is 7. Feeding 25-deep lists in
#     beside 2-deep ones puts two very different completeness regimes
#     under one constant.
#
# 8 sits at Open Library's p90, so the engine sees the shape it was tuned
# on. Measured cost of the depth it drops: 25 added 3,704 character tokens
# the shipped set did not already have, 8 adds 1,280.
DEFAULT_CAP = 8


def _api(subdomain: str, params: dict) -> dict:
    params = {"format": "json", **params}
    return _fetch(f"https://{subdomain}.fandom.com/api.php?"
                  + urllib.parse.urlencode(params))


def find_character_categories(subdomain: str) -> list[tuple[str, int]]:
    """(category, member count), best first — exact names, then a sweep.

    The sweep is only paid for when the exact names come back thin, which
    is the common case on a wiki with its own taxonomy but the expensive
    case everywhere else.
    """
    found: dict[str, int] = {}
    try:
        d = _api(subdomain, {
            "action": "query", "prop": "categoryinfo",
            "titles": "|".join(f"Category:{c}" for c in _EXACT_CATEGORIES)})
        for p in (d.get("query") or {}).get("pages", {}).values():
            n = (p.get("categoryinfo") or {}).get("pages", 0)
            if n > 0:
                found[(p.get("title") or "").split(":", 1)[-1]] = n
    except Unavailable:
        pass

    # Enough of a cast already? Don't pay for the sweep. The threshold is
    # deliberately low: any real character category clears it, and the
    # wikis that don't are exactly the ones the sweep exists for.
    if max(found.values(), default=0) >= 12:
        return sorted(found.items(), key=lambda t: -t[1])

    cont: str | None = None
    for _ in range(4):
        params = {"action": "query", "list": "allcategories",
                  "acprop": "size", "aclimit": 500}
        if cont:
            params["acfrom"] = cont
        try:
            d = _api(subdomain, params)
        except Unavailable:
            break
        for c in (d.get("query") or {}).get("allcategories") or []:
            name, n = c.get("*", ""), c.get("pages", 0)
            if (name and n >= 3 and _CATEGORY_PATTERN.search(name)
                    and not _CATEGORY_REJECT.search(name)):
                found[name] = max(found.get(name, 0), n)
        cont = (d.get("continue") or {}).get("accontinue")
        if not cont:
            break
    return sorted(found.items(), key=lambda t: -t[1])


def category_pages(subdomain: str, category: str) -> dict[str, int]:
    """{page title: article length} for a whole category.

    Whole, not sampled — see the module docstring on Taylor Hebert.
    """
    pages: dict[str, int] = {}
    cont: dict | None = None
    for _ in range(MAX_MEMBER_PAGES):
        params = {"action": "query", "generator": "categorymembers",
                  "gcmtitle": f"Category:{category}", "gcmnamespace": 0,
                  "gcmlimit": 500, "prop": "info"}
        if cont:
            params.update(cont)
        try:
            d = _api(subdomain, params)
        except Unavailable:
            break
        for p in (d.get("query") or {}).get("pages", {}).values():
            title = p.get("title")
            if title:
                pages[title] = p.get("length", 0)
        cont = d.get("continue")
        if not cont:
            break
    return pages


def rank_cast(pages: dict[str, int], cap: int) -> list[dict]:
    """The `cap` longest real character articles, longest first.

    Length is the importance signal (see docstring); this only has to
    remove what is not a person and keep the order.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for title, length in sorted(pages.items(), key=lambda t: -t[1]):
        if len(out) >= cap:
            break
        if length < MIN_ARTICLE_LENGTH or "/" in title:
            continue
        if _ADAPTATION_QUALIFIER.search(title):
            continue          # the film's character, not the book's
        if _PAGE_REJECT.search(title):
            continue
        canon = canonical_name(_BENIGN_QUALIFIER.sub("", title))
        # The same bar the Open Library path applies: a name nobody would
        # say is not a character question. Reusing `usable_token` keeps
        # both sources answering to one definition.
        if not canon or not any(usable_token(t) for t in name_tokens(canon)):
            continue
        if canon in seen:
            continue          # longest article wins; sorted, so this is it
        seen.add(canon)
        out.append({"name": canon, "page": title, "length": length})
    return out


MERGE_IF_UNDER = 100   # members; above this one category is the whole cast
MAX_MERGED_CATEGORIES = 6


def harvest_one(title: str, subdomain: str, cap: int) -> dict:
    cats = find_character_categories(subdomain)
    if not cats:
        return {"subdomain": subdomain, "characters": [],
                "reason": "no character-shaped category on this wiki"}

    # MERGED, NOT PICKED, when the wiki splits its cast up. Taking the
    # single biggest category was tried and lost Shadow Slave's
    # protagonist: its cast is filed per volume, "Volume 3 Characters" is
    # the biggest at 54 members, and Sunny -- whose article is 110,302
    # bytes, twice the longest in any other volume -- sits in "Volume 1
    # Characters" with 19. Member count ranks categories, not characters.
    # A wiki with one real "Characters" category is already whole, so it
    # skips the merge and the extra requests with it.
    chosen = [cats[0][0]]
    if cats[0][1] < MERGE_IF_UNDER:
        chosen = [c for c, _ in cats[:MAX_MERGED_CATEGORIES]]

    pages: dict[str, int] = {}
    for cat in chosen:
        pages.update(category_pages(subdomain, cat))

    cast = rank_cast(pages, cap)
    if not cast:
        return {"subdomain": subdomain, "characters": [],
                "categories_tried": chosen,
                "reason": "categories held no page that survived filtering"}
    return {"subdomain": subdomain, "categories": chosen,
            "categories_available": [c for c, _ in cats[:6]],
            "characters": cast}


def load_targets(args) -> list[tuple[str, str]]:
    """(title, subdomain) pairs — proven wikis, or the census's novels."""
    if args.source == "proven":
        with open(PROVEN_PATH, encoding="utf-8") as fh:
            return [(t, e["subdomain"]) for t, e in json.load(fh).items()
                    if e.get("subdomain")]
    with open(CENSUS_PATH, encoding="utf-8") as fh:
        results = json.load(fh)["results"]
    keep = {"NOVEL", "LITERARY_MULTI_MEDIA"}
    return [(r["title"], r["subdomain"]) for r in results
            if r["type"] in keep and r.get("subdomain")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("census", "proven"), default="census",
                    help="census = the NOVEL/LITERARY_MULTI_MEDIA rows of "
                         "fandom_novel_census.json; proven = the verified "
                         "pairs already in akinator_fandom.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help=f"characters kept per book (default {DEFAULT_CAP}; "
                         f"Open Library's p95 is 14)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-harvest books already in the output file")
    args = ap.parse_args()

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)

    targets = load_targets(args)
    todo = [(t, s) for t, s in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(targets)} target(s); {len(done)} already harvested; "
          f"{len(todo)} to do\n")

    for i, (title, sub) in enumerate(todo, 1):
        try:
            res = harvest_one(title, sub, args.cap)
        except Exception as exc:                                # noqa: BLE001
            # One wiki's failure must not cost the run -- same rule as
            # _gather_extras()' closures in tools/summary.py.
            res = {"subdomain": sub, "characters": [],
                   "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
        done[title] = res
        cast = res.get("characters") or []
        names = ", ".join(c["name"] for c in cast[:3])
        print(f"  [{i:>3}/{len(todo)}] {title[:34]:<34} "
              f"{len(cast):>3} chars  {names[:46]}")
        # Written every time, not at the end: this is a long run over a
        # public API and it will be interrupted.
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    with_cast = [t for t, r in done.items() if r.get("characters")]
    total = sum(len(r["characters"]) for r in done.values() if r.get("characters"))
    print(f"\n{len(with_cast)}/{len(done)} book(s) have a cast; "
          f"{total} characters total")
    print(f"-> {OUT_PATH}")
    print("\nNot written to akinator_fandom.json, the corpus, or "
          "FANDOM_WIKIS -- wiring these into the game is a separate step.")


if __name__ == "__main__":
    main()
