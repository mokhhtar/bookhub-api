"""
scripts/akinator/fandom_books.py — the census novels as corpus rows.

Turns what the Fandom harvests produced into the same doc shape
`build_matrix.py` already consumes, exactly as `site_books.supplement()`
does for our published pages. Same job, third source:

    akinator_fandom_characters.json   cast, ranked, capped at 8  -> `person`
    akinator_fandom_traits.json       model labels               -> `_traits`
    akinator_fandom_subjects.json     mapped wiki categories     -> `subject`
    akinator_fandom_authors.json      author + year              -> both
    fandom_novel_census.json          which wikis are novels at all

NOT WIRED IN BY DEFAULT, and that is the point of this module existing
separately. `build_matrix.py` calls it only under `--with-fandom`, so the
shipped game is unchanged until somebody measures the difference and
decides. `simulate.py` can then be run over a build with and a build
without.

ADDITIONS ONLY — 44 OF THE 228 ARE SKIPPED. Measured 2026-08-16: 44
census titles already have an Open Library corpus row, several of them
highly ranked (The Hunger Games at ~31, Divergent at ~174). Those books
are already in the game with a real `readinglog_count` measured on the
same scale as everyone else's, real subjects and a real author key.
Enriching them from Fandom is a genuinely separate decision. Matching on
title alone is how "Against the Gods" — a Chinese web novel — would
acquire the cast of Peter L. Bernstein's 1996 book on risk management,
which is the row Open Library actually holds under that title.
`site_books.py` matches on title PLUS author surname for precisely this
reason. `harvest_fandom_authors.py` now supplies an author for 69 of the
97 books that have verified prose, so that merge has become POSSIBLE for
some of them — but it is still a separate change with its own blast
radius, and it is not made here. This module still only adds books the
game has never heard of and touches nothing that already exists.

POPULARITY IS A BAND, NOT A NUMBER, and the reasoning is `site_books.py`'s
verbatim: there is no popularity measurement for these books on Open
Library's scale, so inventing one would inject noise into the single most
load-bearing number in the engine. They are ordered against EACH OTHER by
their wiki's article count — a real measure of how much of a following a
work has — and then slotted into a conservative band of the existing
distribution. Present and findable; never outranking a book whose
popularity was actually measured.
"""
from __future__ import annotations

import json
import os

from authors import canonical_author, surname_token
from features import normalize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
CHARS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_characters.json")
TRAITS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_traits.json")
SUBJECTS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_subjects.json")
AUTHORS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_authors.json")

# Deeper into the corpus than site_books' (2000, 4800). Our own published
# pages are books we host and must never fail to guess; these are books we
# merely know exist. They should be findable without displacing anything.
DEFAULT_BAND = (3000, 4900)


def _load(path: str, key: str | None = None):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get(key, data) if key else data


def load_fandom_books() -> list[dict]:
    """Census novels that carry at least a cast or a trait label."""
    census = _load(CENSUS_PATH)
    chars = _load(CHARS_PATH)
    traits = _load(TRAITS_PATH, "traits")
    subjects = _load(SUBJECTS_PATH)
    authors = _load(AUTHORS_PATH)
    if not census:
        return []

    keep = {"NOVEL", "LITERARY_MULTI_MEDIA"}
    out = []
    for r in census.get("results", []):
        if r.get("type") not in keep:
            continue
        title = r["title"]
        cast = [c["name"] for c in (chars.get(title, {}).get("characters") or [])]
        labels = traits.get(title) or []
        # Only the categories that already map to a feature -- see
        # harvest_fandom_subjects.py on why the unmapped ones must never
        # reach `subject`, where their count would become `richness` and
        # tell the engine these books are well documented.
        subs = (subjects.get(title) or {}).get("subjects") or []
        auth = authors.get(title) or {}
        # Nothing harvested means nothing to contribute -- a row with no
        # cast and no labels is a title and a popularity guess, which is
        # all cost and no signal.
        if not cast and not labels and not subs:
            continue
        out.append({
            "title": title,
            "subdomain": r.get("subdomain"),
            "pages": r.get("pages") or 0,
            "cast": cast,
            "labels": labels,
            "subjects": subs,
            "author": auth.get("author"),
            "year": auth.get("year"),
        })
    return out


def supplement(docs: list[dict], band: tuple[int, int] = DEFAULT_BAND,
               verbose: bool = True) -> list[dict]:
    """Add census novels the corpus has no row for. Never edits a row."""
    books = load_fandom_books()
    if not books:
        return docs

    by_title = {normalize(d.get("title") or "") for d in docs}
    missing = [b for b in books if normalize(b["title"]) not in by_title]
    skipped = len(books) - len(missing)

    pop_scale = sorted((d.get("readinglog_count") or 0) for d in docs)
    lo, hi = band
    lo = min(lo, max(0, len(pop_scale) - 1))
    hi = min(hi, max(0, len(pop_scale) - 1))
    top_val = pop_scale[max(0, len(pop_scale) - 1 - lo)]
    bot_val = pop_scale[max(0, len(pop_scale) - 1 - hi)]

    # Article count orders them against each other -- the only following
    # measure available, and used ONLY as an ordering, never as a count.
    missing.sort(key=lambda b: -b["pages"])

    # AUTHOR KEYS BORROWED FROM THE CORPUS, exactly as site_books.py does
    # and for the same reason. `book_traits()` looks Wikidata up BY KEY,
    # and the Fandom harvest produces an author NAME. Without this step
    # every one of these books sits at `unknown` for nationality, gender,
    # living and prolific -- questions the game asks constantly. These
    # authors are rarely strangers to the corpus: Frank Herbert, Isaac
    # Asimov and David Weber all have other books in it, with keys.
    #
    # Name first, then SURNAME, the same two-step and the same tolerance
    # for the two sources spelling a person differently.
    by_name: dict[str, str] = {}
    by_surname: dict[str, str] = {}
    for d in docs:
        keys = d.get("author_key") or []
        for i, nm in enumerate(d.get("author_name") or []):
            if i >= len(keys) or not keys[i]:
                continue
            by_name.setdefault(normalize(nm), keys[i])
            sur = surname_token(canonical_author(nm))
            if sur:
                by_surname.setdefault(sur, keys[i])

    def author_key_for(name: str) -> list[str]:
        if not name:
            return []
        hit = by_name.get(normalize(name))
        if not hit:
            hit = by_surname.get(surname_token(canonical_author(name)))
        return [hit] if hit else []

    added = []
    borrowed = 0
    n = max(1, len(missing) - 1)
    for i, b in enumerate(missing):
        frac = i / n
        akey = author_key_for(b["author"] or "")
        if akey:
            borrowed += 1
        added.append({
            "key": "/fandom/" + (b["subdomain"] or normalize(b["title"]).replace(" ", "-")),
            "title": b["title"],
            # Harvested by harvest_fandom_authors.py -- infobox first, the
            # model on verified prose second, and null rather than a guess.
            # A book with no author still reads `unknown`, never wrong.
            "author_name": [b["author"]] if b["author"] else [],
            "author_key": akey,
            "first_publish_year": b["year"],
            # The trait labels are NOT subjects: they are already keyed
            # (`t:magic`) and are handed to build_matrix separately so they
            # go through the same path as the Open Library labels rather
            # than being re-derived from strings. These strings are the
            # wiki's own categories, pre-filtered to the ones that map.
            "subject": b["subjects"],
            "person": b["cast"],
            "language": ["eng"],
            "readinglog_count": int(top_val - frac * (top_val - bot_val)),
            "ebook_access": "",
            "_fandom_wiki": b["subdomain"],
            "_fandom_traits": b["labels"],
        })

    out = docs + added
    out.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    if verbose:
        with_cast = sum(1 for b in missing if b["cast"])
        with_labels = sum(1 for b in missing if b["labels"])
        with_subs = sum(1 for b in missing if b["subjects"])
        with_auth = sum(1 for b in missing if b["author"])
        with_year = sum(1 for b in missing if b["year"])
        print(f"Fandom novels: +{len(added)} added ({with_cast} with a cast, "
              f"{with_labels} with trait labels, {with_subs} with mapped "
              f"subjects, {with_auth} with an author of which {borrowed} "
              f"matched a corpus author key, {with_year} with a year), "
              f"{skipped} skipped as already in the corpus")
    return out
