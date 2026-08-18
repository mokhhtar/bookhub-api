"""
scripts/akinator/harvest_fandom_subjects.py — wiki categories, as subjects.

    python scripts/akinator/harvest_fandom_subjects.py --limit 10
    python scripts/akinator/harvest_fandom_subjects.py            # resumes

WHY, and what the measurement said. `simulate.py --target-prefix /fandom/`
scored the 167 Fandom rows in the shipped corpus at **0.0%** — not one
guessed. The cause was not obscurity (Open Library books at the same ranks
carry a median of 10 present features and are guessable) but that those
rows carried a median of ONE. `fandom_books.py` gives them no subjects, no
author and no year, so nearly every question reads `unknown` and belief
can never concentrate on them.

Wiki categories are the nearest thing to a subject list these books have,
and they are already fetched once by the census.

**ONLY THE CATEGORIES THAT MAP TO A FEATURE ARE KEPT, and that restriction
is the whole safety argument.** `features.extract()` sets

    richness = len(content_subjects)

and `absence_confidence()` reads richness to decide what a MISSING feature
means: 0.45 at richness <= 2, and 0.15 above 15 — "richly documented, so
absence really does mean something". Shadow Slave's wiki has 187 sized
categories. Passing them all would give it richness 187 and therefore 85%
confidence that it lacks any feature its categories did not happen to
name — so a player answering "yes, it has magic" would eliminate the
correct book, hard. That is the exact failure this project forbids.

The two kinds of string only look alike. Open Library subjects are
curated topical descriptors ("sea stories", "detective and mystery
stories"). Wiki categories are an in-world filing system — "Volume 3
Characters", "Sunny", "Aspect", "Government-Affiliated". A wiki with 187
categories is elaborately organised about its own fiction, not richly
documented about its themes, and richness must not be told otherwise.

Keeping only mapped categories makes richness mean what it says: these
books ARE thinly documented, they land at 2-6, and absence stays close to
uninformative. Measured yield is small and honest — 2 to 4 features per
wiki before `GENRE_MIN_SUPPORT` drops the singletons.

Output: data/akinator_fandom_subjects.json (gitignored, regenerable).
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from features import is_stop_subject, map_subject, normalize   # noqa: E402
from prove_fandom import _GENERIC_CATEGORY, Unavailable, _fetch  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
PROVEN_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_subjects.json")

MAX_CATEGORY_PAGES = 4
MIN_CATEGORY_PAGES = 3    # a category with two members organises nothing

# WIKI VOCABULARY THAT COLLIDES WITH OPEN LIBRARY'S, MEANING SOMETHING
# ELSE. Filtering on "does it map to a feature" was not enough: it asks
# whether a string matches, never whether the match MEANS anything. Found
# live by a player whose Lord of the Mysteries game was pushed down by
# five wrong cells (2026-08-18):
#
#   "Pages using ISBN magic links" -> theme:magic     48 books
#       A MediaWiki maintenance category about automatic ISBN linking.
#       Half a hundred books were told they contain magic by a footnote
#       about hyperlinks.
#   "Non-Canon"                    -> form:classic
#       In a wiki "canon" is whether a story counts; in a catalogue it is
#       the literary canon. Same word, unrelated meaning, and it made a
#       2018 web serial a classic.
#   "Games"                        -> genre:sports    36 books
#       In-story games, not sport.
#
# This is the substring trap features.py already documents ("Warsaw is a
# war book") in a harder form: these ARE whole words, correctly matched,
# from a vocabulary that uses them differently.
_WIKI_MAINTENANCE = re.compile(
    r"^(pages? using|pages? with|articles? using|articles? with|non[- ]canon"
    r"|candidates for|.*needing|.*to be |browse|community|site |wiki )",
    re.IGNORECASE)

# How many mapped categories may reach `subject`. Not a tidiness cap --
# `features.extract()` sets `richness = len(content_subjects)` and
# `absence_confidence()` reads it: above 15 an absent feature is scored at
# 0.15, i.e. "richly documented, so absence really means something".
# Measured on this harvest: median 10, max 430, and 102 rows above 15.
# A wiki with 430 filing categories is not a well-documented BOOK, and
# telling the engine otherwise is what let three wrong cells eliminate
# the right book with confidence. 12 keeps these rows at 0.25 -- real
# evidence, never proof -- which is what they have actually earned.
MAX_SUBJECTS = 12


def wiki_subjects(subdomain: str) -> tuple[list[str], int]:
    """(categories that map to a feature, categories seen).

    The second number is reported but never used as richness -- see the
    module docstring on why that would be actively harmful.
    """
    seen = 0
    kept: list[str] = []
    cont: str | None = None
    for _ in range(MAX_CATEGORY_PAGES):
        params = {"action": "query", "list": "allcategories",
                  "acprop": "size", "aclimit": 500, "format": "json"}
        if cont:
            params["acfrom"] = cont
        try:
            d = _fetch(f"https://{subdomain}.fandom.com/api.php?"
                       + urllib.parse.urlencode(params))
        except Unavailable:
            break
        for c in (d.get("query") or {}).get("allcategories") or []:
            name = c.get("*", "")
            if not name or c.get("pages", 0) < MIN_CATEGORY_PAGES:
                continue
            seen += 1
            if _GENERIC_CATEGORY.match(name) or _WIKI_MAINTENANCE.match(name):
                continue
            n = normalize(name)
            if not n or is_stop_subject(n):
                continue
            if map_subject(n):
                kept.append(name)
        cont = (d.get("continue") or {}).get("accontinue")
        if not cont:
            break
    # Deduplicated: two categories normalising to the same string are one
    # source, and GENRE_MIN_SUPPORT counts independent sources.
    out, seen_norm = [], set()
    for name in kept:
        n = normalize(name)
        if n not in seen_norm:
            seen_norm.add(n)
            out.append(name)
    # Shortest first: a one- or two-word category ("Dragons", "Magic") is
    # a topic; a long one ("Characters that appear in Kingdom Hearts") is
    # a filing path that happens to contain a topical word.
    out.sort(key=lambda s: (len(s.split()), len(s)))
    return out[:MAX_SUBJECTS], seen


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--source", choices=("census", "proven", "both"),
                    default="both",
                    help="'proven' is akinator_fandom.json — the 41 pairs "
                         "verified by prove_fandom.py, which Category:"
                         "Books_hub does not contain and the census "
                         "therefore never sees. Lord of the Mysteries, the "
                         "book that started the Fandom work, is one of them.")
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    if args.source in ("census", "both"):
        with open(CENSUS_PATH, encoding="utf-8") as fh:
            results = json.load(fh)["results"]
        keep = {"NOVEL", "LITERARY_MULTI_MEDIA"}
        for r in results:
            if r["type"] in keep and r.get("subdomain"):
                targets.append((r["title"], r["subdomain"]))
                seen.add(r["subdomain"])
    if args.source in ("proven", "both") and os.path.exists(PROVEN_PATH):
        with open(PROVEN_PATH, encoding="utf-8") as fh:
            for title, entry in json.load(fh).items():
                sub = entry.get("subdomain")
                if sub and sub not in seen:
                    seen.add(sub)
                    targets.append((title, sub))

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)
    todo = [(t, s) for t, s in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(targets)} target(s); {len(done)} done; {len(todo)} to do\n")

    for i, (title, sub) in enumerate(todo, 1):
        try:
            subjects, seen = wiki_subjects(sub)
        except Exception as exc:                                # noqa: BLE001
            subjects, seen = [], 0
            print(f"    ! {title[:30]}: {type(exc).__name__}")
        done[title] = {"subdomain": sub, "subjects": subjects,
                       "categories_seen": seen}
        print(f"  [{i:>3}/{len(todo)}] {title[:30]:<30} "
              f"{len(subjects):>2} of {seen:>4} categories map  "
              f"{', '.join(subjects[:3])[:40]}")
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    got = [t for t, r in done.items() if r["subjects"]]
    total = sum(len(r["subjects"]) for r in done.values())
    print(f"\n{len(got)}/{len(done)} book(s) gained at least one mapped "
          f"subject; {total} subjects total")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
