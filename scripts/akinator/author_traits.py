"""
scripts/akinator/author_traits.py — Wikidata author facts as game questions.

WHY THESE ARE THE QUESTIONS THE GAME WAS MISSING. Phase 1 measured the game
failing in the tail: a book at rank 10,000 carries half the subjects of one
at rank 1,000, so the usable feature count fell 55 -> ~35 and success fell
with it. Subject-derived questions cannot fix that, because the subjects are
what is missing.

Author facts break the dependency. **An obscure book still has an author**,
and one Wikidata match enriches every book that author wrote. Harvest rates
measured on the corpus: 57% of the 1,200 most-read authors matched, and of
those matches 99% carry gender and 92% carry nationality — dense facts,
unlike the sparse subject strings they supplement.

They are also *early-game* questions, which is the half the corpus was
shortest on. "Is the author still alive?" splits a popularity-ranked corpus
close to the middle; "does it take place at sea?" never can.

GROUNDING, and the base-rate rule that phase 0 paid for. An author we could
not match is **unknown**, not "no". Every trait here returns `None` when
Wikidata is silent, and `features.extract()` routes those into the `unknown`
set so they score at the base rate rather than being read as a denial. An
unmatched author must not make their books harder to guess — that would
punish the book for a gap in Wikidata, which is the same failure as
[[Grounding Rule]] applied to a missing subject.

ONE LIVING AUTHOR CAVEAT. Wikidata records a death date or it does not, and
absence is genuinely ambiguous — it means "alive" for a contemporary
novelist and "undocumented" for a 17th-century one. `is_alive` therefore
returns None unless a birth date makes the answer safe, rather than reading
every missing death date as "still with us".
"""
from __future__ import annotations

import json
import os

import author_overrides

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTHORS_WD_PATH = os.path.join(REPO_ROOT, "data", "akinator_authors_wd.json")

# WORDING IS OWNER-REVIEWED (2026-08-07). Three changes came out of that
# pass, and all three are about what a reader can answer rather than what
# Wikidata can state:
#
#   "Was the author born in the 20th century?" -> "Is the author a modern
#     writer?" — readers hold an era, not a birth century.
#   "Did the author die more than a century ago?" — CUT and folded into
#     "Is the author still alive?", which asks the same thing in the form
#     people actually think in.
#   "Is the author from outside Europe and North America?" -> named
#     regions, because negated geography is hard to answer under pressure.
AUTHOR_QUESTIONS = {
    "author:female": "Was it written by a woman?",
    "author:alive": "Is the author still alive?",
    "author:american": "Is the author American?",
    "author:british": "Is the author British?",
    "author:european": "Is the author from continental Europe?",
    "author:nonwestern": "Is the author from Asia, Africa, or Latin America?",
    "author:prolific": "Has the author written many books?",
    "author:c20": "Is the author a modern writer?",
}

_AMERICAN = {"United States", "United States of America"}
_BRITISH = {
    "United Kingdom", "United Kingdom of Great Britain and Ireland",
    "England", "Scotland", "Wales", "Great Britain", "Kingdom of England",
    "Kingdom of Great Britain",
}
_EUROPEAN = {
    "France", "Germany", "Italy", "Spain", "Russia", "Soviet Union",
    "Portugal", "Netherlands", "Belgium", "Switzerland", "Austria",
    "Sweden", "Norway", "Denmark", "Finland", "Poland", "Czech Republic",
    "Czechoslovakia", "Hungary", "Greece", "Ireland", "Romania", "Ukraine",
    "Serbia", "Croatia", "Bulgaria", "Kingdom of Italy", "Nazi Germany",
    "German Empire", "Weimar Republic", "Kingdom of France", "Prussia",
    "Russian Empire",
}
# Anglophone settler countries sit with the US/UK for the purposes of the
# "outside Europe and North America" question — a player thinking of a
# Canadian novelist would not call them non-Western.
_WESTERN_OTHER = {"Canada", "Australia", "New Zealand", "Ireland"}


AUTHORS_SEARCH_PATH = os.path.join(REPO_ROOT, "data",
                                   "akinator_authors_search.json")


def load_wikidata(path: str = AUTHORS_WD_PATH,
                  search_path: str = AUTHORS_SEARCH_PATH) -> dict[str, dict]:
    """The P648 join, plus the name-matched authors it could not reach.

    TWO SOURCES, ONE OF THEM AUTHORITATIVE. `harvest_authors.py` joins on
    P648 — an identifier somebody asserted, needing no judgement. It covers
    1,962 of the 4,134 authors in the shipped corpus.
    `harvest_authors_bysearch.py` finds another 632 by NAME, under five
    acceptance rules, because a name is not an identifier.

    So a P648 record ALWAYS wins: a judgement call never overrides an
    asserted identity, and a search record is only used for an author the
    exact join left empty. Missing search file = the game simply has the
    coverage it had before, never wrong facts.

    Shapes are reconciled here rather than at harvest time so each file
    stays a faithful record of what its own source said: the search
    harvest stores years as integers and countries as QIDs plus resolved
    names, and `traits_for` expects date-like strings and country names.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        merged = json.load(fh)

    if not os.path.exists(search_path):
        return merged

    with open(search_path, encoding="utf-8") as fh:
        found = json.load(fh)

    added = 0
    for key, rec in found.items():
        if rec.get("status") != "found":
            continue
        existing = merged.get(key)
        if isinstance(existing, dict) and existing.get("qid"):
            continue                      # the exact join wins, always
        merged[key] = {
            "qid": rec.get("qid"),
            "gender": rec.get("gender"),
            "countries": list(rec.get("countries") or []),
            "occupations": list(rec.get("occupations") or []),
            # traits_for()'s _year() parses a leading year off a string.
            "birth": str(rec["birth"]) if rec.get("birth") else None,
            "death": str(rec["death"]) if rec.get("death") else None,
        }
        added += 1
    if added:
        print(f"Author facts: +{added} matched by name "
              f"(P648 kept priority on every conflict)")
    return merged


def _year(date_str: str | None) -> int | None:
    """Leading year of a Wikidata date, tolerating BCE and padded years."""
    if not date_str:
        return None
    s = date_str.lstrip("-")[:4]
    try:
        year = int(s)
    except ValueError:
        return None
    return -year if date_str.startswith("-") else year


def traits_for(record: dict | None, book_count: int, this_year: int = 2026
               ) -> dict[str, bool | None]:
    """One author's facts as answerable questions.

    `None` everywhere Wikidata is silent — see the module docstring. Only
    `author:prolific` is answerable without a match, because it comes from
    our own corpus rather than from Wikidata.
    """
    out: dict[str, bool | None] = {k: None for k in AUTHOR_QUESTIONS}

    # Known from the corpus alone, match or no match.
    out["author:prolific"] = book_count >= 5

    if not record:
        return out

    gender = record.get("gender")
    if gender:
        out["author:female"] = (gender == "female")

    countries = set(record.get("countries") or [])
    if countries:
        out["author:american"] = bool(countries & _AMERICAN)
        out["author:british"] = bool(countries & _BRITISH)
        out["author:european"] = bool(countries & _EUROPEAN)
        out["author:nonwestern"] = not bool(
            countries & (_AMERICAN | _BRITISH | _EUROPEAN | _WESTERN_OTHER))

    birth = _year(record.get("birth"))
    death = _year(record.get("death"))

    if birth is not None:
        # "A modern writer" — born in the 20th century or later. The
        # underlying test is unchanged; only the question it answers was
        # reworded, because a reader holds an era and not a birth year.
        out["author:c20"] = birth >= 1900

    if death is not None:
        out["author:alive"] = False
    elif birth is not None:
        # No death date is ambiguous on its own: it means "alive" for a
        # contemporary writer and "undocumented" for an old one. Only
        # commit where the birth year makes it safe.
        age = this_year - birth
        if age < 95:
            out["author:alive"] = True
        elif age > 150:
            out["author:alive"] = False
        # Between 95 and 150 it stays unknown, which is the honest answer.

    return out


def facts_author(author_ids: list[str], wd: dict[str, dict]) -> str | None:
    """WHOSE facts a book takes — the one decision, in one place.

    Co-authored books take the first author's facts. Averaging two authors'
    nationalities would invent a fact about neither of them, and a player
    thinking of a co-authored book almost always has the lead author in
    mind. "First" means the first author Wikidata actually knows, falling
    back to the literal first: a lead author nobody documented should not
    cost the book the co-author's real facts.

    Split out of `book_traits` so the hand-set overlay can look up the SAME
    author these facts came from. Recomputing that choice separately would
    let an override for the second author quietly become the book's answer
    — two people's facts on one row, which is the exact failure the
    "co-authored books take the first author" rule exists to prevent.
    """
    for aid in author_ids:
        # `aid` is the Open Library author key when one exists, which is
        # also what the harvest is keyed on.
        if wd.get(aid):
            return aid
    return author_ids[0] if author_ids else None


def book_traits(author_ids: list[str], wd: dict[str, dict],
                book_counts: dict[str, int]) -> dict[str, bool | None]:
    """Merge the traits of a book's authors into the book's own features."""
    aid = facts_author(author_ids, wd)
    if aid is None:
        return {k: None for k in AUTHOR_QUESTIONS}
    return traits_for(wd.get(aid), book_counts.get(aid, 0))


def apply_author_facts(book: dict, doc: dict, wd: dict[str, dict],
                       book_counts: dict[str, int],
                       overrides: dict[str, dict] | None = None,
                       aliases: dict[str, str] | None = None) -> None:
    """Wikidata's author facts, then the owner's, onto one book's sets.

    SHARED BY build_matrix.py AND simulate.py, and that is the point rather
    than a convenience. Those two carried a byte-identical copy of this
    block, and this project has twice paid for simulate.py drifting from
    the game it claims to measure — most recently when a whole branch of
    the trait-labelling rule existed in one file and not the other, making
    a FORCE_KEEP question invisible to every measurement ever taken.

    PRECEDENCE: the hand-set overlay wins over Wikidata, the same way
    `admin_corrections.json` wins over a catalogued year. A person who
    looked the author up is better evidence than a database that has not
    been told yet — and `null` in the overlay is a real verdict meaning
    "back to unknown", not an absent one, because a wrong fact fans out to
    every book that author wrote while a missing one costs almost nothing.
    """
    ol_keys = doc.get("author_key") or []
    facts = book_traits(ol_keys, wd, book_counts)
    if overrides:
        keys = author_overrides.lookup_keys(
            ol_keys, doc.get("author_name") or [],
            facts_author(ol_keys, wd), aliases or {})
        facts.update(author_overrides.facts_for(keys, overrides))

    present = set(book["present"])
    unknown = set(book["unknown"])
    known_false = set(book.get("known_false") or ())
    for key, value in facts.items():
        # Three states, and they are not two. An author nobody matched is
        # UNKNOWN — scoring that as "no" would punish the book for a gap in
        # Wikidata. But Wikidata saying the author IS American does mean
        # they are certainly not African, and scoring THAT like an
        # unrecorded subject is what let the game ask both.
        present.discard(key)
        unknown.discard(key)
        known_false.discard(key)
        if value is None:
            unknown.add(key)
        elif value:
            present.add(key)
        else:
            known_false.add(key)
    book["present"] = sorted(present)
    book["unknown"] = sorted(unknown)
    book["known_false"] = sorted(known_false)
