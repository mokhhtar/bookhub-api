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


def load_wikidata(path: str = AUTHORS_WD_PATH) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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


def book_traits(author_ids: list[str], wd: dict[str, dict],
                book_counts: dict[str, int]) -> dict[str, bool | None]:
    """Merge the traits of a book's authors into the book's own features.

    Co-authored books take the first author's facts. Averaging two authors'
    nationalities would invent a fact about neither of them, and a player
    thinking of a co-authored book almost always has the lead author in
    mind.
    """
    for aid in author_ids:
        # `aid` is the Open Library author key when one exists, which is
        # also what the harvest is keyed on.
        record = wd.get(aid)
        if record:
            return traits_for(record, book_counts.get(aid, 0))
    if author_ids:
        return traits_for(None, book_counts.get(author_ids[0], 0))
    return {k: None for k in AUTHOR_QUESTIONS}
