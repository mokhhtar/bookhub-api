"""
scripts/akinator/fandom_books.py — the census novels as corpus rows.

Turns what the Fandom harvests produced into the same doc shape
`build_matrix.py` already consumes, exactly as `site_books.supplement()`
does for our published pages. Same job, third source:

    akinator_fandom_characters.json   cast, ranked, capped at 8  -> `person`
    akinator_fandom_traits.json       model labels               -> `_traits`
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
Enriching them from Fandom is a genuinely separate decision and it needs
something this harvest does not have: an AUTHOR. Matching on title alone
is how "Against the Gods" — a Chinese web novel — would acquire the cast
of Peter L. Bernstein's 1996 book on risk management, which is the row
Open Library actually holds under that title. `site_books.py` matches on
title PLUS author surname for precisely this reason. Until the Fandom
side carries an author, the safe operation is the one that cannot
mismatch: add the 184 books the game has never heard of, touch nothing
that already exists.

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

from features import normalize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
CHARS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_characters.json")
TRAITS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_traits.json")
SUBJECTS_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_subjects.json")

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

    added = []
    n = max(1, len(missing) - 1)
    for i, b in enumerate(missing):
        frac = i / n
        added.append({
            "key": "/fandom/" + (b["subdomain"] or normalize(b["title"]).replace(" ", "-")),
            "title": b["title"],
            # No author is recorded by the harvest, and guessing one from
            # wiki prose would be a claim the game then asks questions
            # about. Absent, so author questions read `unknown`.
            "author_name": [],
            "author_key": [],
            "first_publish_year": None,
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
        print(f"Fandom novels: +{len(added)} added ({with_cast} with a cast, "
              f"{with_labels} with trait labels, {with_subs} with mapped "
              f"subjects), {skipped} skipped as already in the corpus")
    return out
