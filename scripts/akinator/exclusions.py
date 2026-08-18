"""
scripts/akinator/exclusions.py — books the owner has removed by hand.

WHY THIS EXISTS. Playing the game surfaces books that should not be in it
at all — a wiki that turned out to be the wrong kind of thing, a corpus row
that is noise rather than a book. Until today there was no way to act on
that short of editing `corpus_filter.py` by hand and pushing a commit.

WHERE THE FILE LIVES, and why not here. `excluded.json` is written by the
admin page (`tools/akinator_admin.py`) into the `bookhub` repo, at
`games/data/akinator/excluded.json` — the same place `books.json` and
`questions.json` already live. Two readers need it and both already read
from that location: the CLIENT (`book-mind-reader.html`, fetched at every
page load, to zero an excluded book's prior instantly) and, locally, a
full `build_matrix.py` rebuild (via the sibling `../bookhub` checkout
`site_books.py` already reads for `_books/*.md`). One file, one source of
truth, no copy to keep in sync between repos.

APPLIED TWICE, same reason `apply_corrections()` is. `corpus_filter.py`
runs before `site_supplement()`/`fandom_supplement()` add their own rows,
so a book excluded there is invisible to a first pass — it has to be
checked again after the supplements, in `build_matrix.load_corpus()` and
`simulate.load_docs()`, exactly where `apply_corrections()` already runs
a second time.
"""
from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXCLUDED_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator", "excluded.json"))


def load_excluded() -> set[str]:
    """Work keys the admin page has excluded. Missing file = nothing excluded."""
    if not os.path.exists(EXCLUDED_PATH):
        return set()
    try:
        with open(EXCLUDED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    return {k for k in data if isinstance(k, str)}


def drop_excluded(docs: list[dict], verbose: bool = False) -> list[dict]:
    """Remove any doc whose key is in excluded.json. Safe to call more than once."""
    excluded = load_excluded()
    if not excluded:
        return docs
    kept = [d for d in docs if d.get("key") not in excluded]
    dropped = len(docs) - len(kept)
    if dropped and verbose:
        print(f"Exclusions: {dropped} book(s) removed by admin action")
    return kept
