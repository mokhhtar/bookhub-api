"""
scripts/akinator/characters.py — character names out of Open Library's `person`.

THE FIND THIS MODULE EXISTS FOR (2026-08-06). The plan assumed character
data would have to come from Wikidata `P674` plus Gemini extraction from
Wikipedia articles. It turns out **Open Library's `person` field already
carries the cast** for a large share of fiction, in the same dump we were
downloading anyway:

    Harry Potter and the Philosopher's Stone
      Harry Potter, Ron Weasley, Hermione Granger, Neville Longbottom,
      Rubeus Hagrid, Albus Dumbledore, Minerva McGonagall, ...
    Pride and Prejudice
      Elizabeth Bennet, Fitzwilliam Darcy, Charlotte Lucas, George Wickham, ...

Measured on the Phase 0 sample: 55% of the 500 most-read works carry it.
That makes "is the main character named Poirot?" answerable without a single
Gemini call, and demotes Wikidata from primary source to enrichment layer
(traits: gender, species, real-or-fictional).

AND IT ARRIVES WITH THE ALIAS PROBLEM BUILT IN — the exact failure the plan
predicted, visible in one book's own list:

    Murder on the Orient Express
      'Hercule Poirot (Fictitious character)', 'Poirot',
      'Hercule (Fictitious character)'
    The Hound of the Baskervilles
      'Mr. Sherlock Holmes (Fictional character)', 'Sherlock Holmes',
      'Dr. Watson (Fictional character)', 'John H. Watson (Fictitious character)'

Three spellings of one detective, two spellings of the qualifier itself
("Fictitious" vs "Fictional"). Left alone these become three separate
characters, and a player thinking "Poirot" matches only one of them. So
canonicalization is not a refinement here, it is the feature working at all.
"""
from __future__ import annotations

import re

from features import normalize

# "(Fictitious character)", "(Fictional character)", "(Spirit)", and the
# occasional trailing date range on real people: "Napoleon, 1769-1821".
_QUALIFIER = re.compile(
    r"\s*\((fictitious|fictional|fictitous|spirit|legendary|biblical)[^)]*\)",
    re.I,
)
_DATES = re.compile(r",?\s*\d{3,4}\s*[-–]\s*\d{0,4}\s*$")
_HONORIFICS = re.compile(
    r"^(mr|mrs|ms|miss|dr|doctor|prof|professor|sir|lady|lord|capt|captain|"
    r"rev|reverend|st|saint|king|queen|prince|princess|uncle|aunt)\.?\s+",
    re.I,
)

# `person` is a subject field, so it also collects groups, places and
# non-character entries. These are not people anyone thinks of by name.
_NOT_A_CHARACTER = (
    "family", "families", "brothers", "sisters", "children", "society",
    "company", "school", "university", "club", "team", "army",
)


def canonical_name(raw: str) -> str:
    """One character name, reduced to a comparable form.

    'Mr. Sherlock Holmes (Fictional character)' and 'Sherlock Holmes' both
    become 'sherlock holmes', which is what lets a player's guess meet our
    record. Returns '' for entries that are not usable character names.
    """
    s = _QUALIFIER.sub("", raw or "")
    s = _DATES.sub("", s)
    s = normalize(s)
    s = _HONORIFICS.sub("", s).strip()
    if not s or len(s) < 3:
        return ""
    if any(w in s for w in _NOT_A_CHARACTER):
        return ""
    if len(s.split()) > 4:
        return ""      # descriptive phrase, not a name
    return s


def name_tokens(canonical: str) -> set[str]:
    """The individual name words a player might use on their own.

    A player thinks 'Poirot' or 'Hermione', rarely the full record form.
    Tokens of 3+ characters only, so initials and particles ('de', 'van')
    don't become questions.
    """
    return {t for t in canonical.split() if len(t) >= 3}


def extract_characters(persons: list[str]) -> tuple[set[str], set[str]]:
    """(canonical full names, individual name tokens) for one book.

    Deduplicates the aliases OL files separately: Poirot's three entries
    collapse because 'hercule poirot' and 'poirot' share the token
    'poirot'. The full-name set stays available for display and for the
    end-of-game reveal search.
    """
    names: set[str] = set()
    tokens: set[str] = set()
    for raw in persons or []:
        canon = canonical_name(raw)
        if not canon:
            continue
        names.add(canon)
        tokens |= name_tokens(canon)
    return names, tokens


# Common words that survive tokenization but identify nobody.
TOKEN_STOPWORDS = {
    "the", "and", "der", "von", "van", "del", "los", "las", "ibn",
    "jr", "sr", "iii", "man", "boy", "girl", "old", "new", "one", "two",
    "god", "lord", "king", "queen", "doctor", "father", "mother",
}


def usable_token(token: str) -> bool:
    """Reject anything a player would never say as a character's name.

    The digit test is not hypothetical: OL files real people as
    'Shakespeare, William, 1564-1616' and dates in parentheses survive
    normalization, so the first run produced 'is one of them named 1564?'
    among its character questions.
    """
    if any(ch.isdigit() for ch in token):
        return False
    return token not in TOKEN_STOPWORDS and len(token) >= 4
