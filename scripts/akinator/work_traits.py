"""
scripts/akinator/work_traits.py — the harvested work facts, as questions.

Reads what `harvest_works.py` and `harvest_protagonist.py` produced and
turns it into the three features they were built for:

    book:film         Has it been made into a film or TV series?
    form:series       Is it part of a series?          (a repair — see below)
    char:femalelead   Is the main character a woman?

WHY THESE THREE ARE WORTH A HARVEST EACH. `book:film` measured at 46% of
matched books, which is about as close to an even split as this project has
found, and it is answerable instantly by anyone picturing a book — the two
axes that phase 3 established have to hold together.

`form:series` is not a new question, it is a **repair**. It already exists
in `features.py` and reads as 4.2% of the corpus, which looks like a rare
property and is actually a data failure: Open Library seldom records series
membership. Wikidata's P179 comes free on the same traversal that fetches
adaptations, so the fix costs nothing extra. Where both sources are silent
the book still answers `unknown`, exactly as before.

GROUNDING, and it is the whole reason these functions return `None` so
readily. A book we could not match to a Wikidata work is unknown on all
three, never "no". Roughly half the corpus is in that position, and if
absence were read as denial we would be confidently asserting that half our
books were never filmed and have no series — turning a gap in Wikidata into
a wrong answer about the book. Same rule as a missing subject; see
[[Grounding Rule]].
"""
from __future__ import annotations

import json
import os

from characters import canonical_name
from features import normalize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKS_PATH = os.path.join(REPO_ROOT, "data", "akinator_works_wd.json")
PROTAG_PATH = os.path.join(REPO_ROOT, "data", "akinator_protagonist.json")

WORK_QUESTIONS = {
    "book:film": "Has it been made into a film or TV series?",
    "char:femalelead": "Is the main character a woman?",
}


def load_works(path: str = WORKS_PATH) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_protagonists(path: str = PROTAG_PATH) -> dict[str, str | None]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _work_record(doc: dict, works: dict[str, dict]) -> dict | None:
    """Find this book in the harvest, keyed by author + normalised title.

    Never by title alone: same-title collisions between different works are
    common and the author key is what separates them.
    """
    title = normalize(doc.get("title") or "")
    if not title:
        return None
    for key in doc.get("author_key") or []:
        rec = works.get(f"{key}|{title}")
        if rec:
            return rec
    return None


def traits_for(doc: dict, works: dict[str, dict],
               protagonists: dict[str, str | None]) -> dict[str, bool | None]:
    """The three features for one book. `None` wherever we cannot say."""
    out: dict[str, bool | None] = {
        "book:film": None,
        "form:series": None,
        "char:femalelead": None,
    }

    rec = _work_record(doc, works)
    if rec is not None:
        # Matched. A matched work with no adaptation recorded really is a
        # "no" — Wikidata documents adaptations well for works it holds.
        out["book:film"] = bool(rec.get("film"))
        out["form:series"] = bool(rec.get("series"))

    # The first name Open Library lists is the protagonist — 8 of 8 in spot
    # checks; see harvest_protagonist.py for the evidence and the caveat.
    for raw in doc.get("person") or []:
        name = canonical_name(raw)
        if not name or any(ch.isdigit() for ch in name):
            continue
        gender = protagonists.get(name)
        if gender:
            out["char:femalelead"] = (gender == "female")
        break      # only the first listed name is the protagonist

    return out


def merge_into(book: dict, doc: dict, works: dict[str, dict],
               protagonists: dict[str, str | None]) -> None:
    """Fold the three features into a book's present/unknown sets in place.

    Three destinations, not two. `None` goes to `unknown` (we cannot say);
    `False` goes to `known_false` (we positively determined it is not so).

    The distinction is recorded but the engine currently scores
    `known_false` exactly like ordinary absence — giving it its own low
    probability measured much worse. See the note above
    `EXCLUSIVE_GROUPS` in features.py before changing that.
    """
    present = set(book["present"])
    unknown = set(book["unknown"])
    known_false = set(book.get("known_false") or ())
    for key, value in traits_for(doc, works, protagonists).items():
        if value is None:
            unknown.add(key)
        elif value:
            present.add(key)
            known_false.discard(key)
        else:
            known_false.add(key)
            unknown.discard(key)
    book["present"] = sorted(present)
    book["unknown"] = sorted(unknown)
    book["known_false"] = sorted(known_false)
