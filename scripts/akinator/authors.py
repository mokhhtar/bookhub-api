"""
scripts/akinator/authors.py — author identity, and the questions it supports.

WHY AN ENTITY AND NOT A COLUMN. The build plan originally carried `author`
as a text field on the book, which cannot answer "is it by Agatha
Christie?" reliably — the same mistake as keying a book on its title
string. Authors get stable ids, aliases and a link table.

The data supports it better than anything else we have. Measured on the
Phase 0 sample: `author_key` (Open Library's `/authors/OL...A`) is present
on **98%** of works, against 96% for subjects and 55% for the cast.

TWO KINDS OF AUTHOR QUESTION, and they belong in different halves of the
game:

  * "Is it by Stephen King?" is an ENDGAME question. King tops the sample
    at 13 books out of 500 — naming an author splits off a handful, exactly
    like naming a character, and asking it early wastes a turn.
  * Author TRAITS — alive, gender, nationality, language — split the corpus
    near the middle and are what the early game is short of. They need
    Wikidata and belong to phase 2; this module leaves room for them
    (`traits`) but does not invent them.

THE NORMALIZER TRAP THIS MODULE EXISTS TO AVOID. A throwaway normalizer
written to *look* for alias collisions reported that eight authors shared
one name. They were:

    Όμηρος · Лев Толстой · Фёдор Михайлович Достоевский · రవే మంత్రీ
    刘慈欣 · 太宰 治 · 村上春樹 · 老子

Homer, Tolstoy, Dostoevsky, Liu Cixin, Dazai, Murakami and Laozi, collapsed
into a single empty string — because it stripped every non-ASCII character
and nothing was left. **A script-stripping normalizer silently merges every
non-Latin name into one entity.** Worse than the known Dostoyevsky /
Dostoevsky trap, because it fails totally and quietly.

So `canonical_author()` folds case and diacritics and punctuation and never
filters by script, and `merge_key()` refuses to return an empty or
digits-only key. 15 of 420 author strings in the sample are non-Latin, and
they are real cross-script aliases: a player typing "Tolstoy" has to reach
`Лев Толстой`. `ol_author_key` does NOT bridge that — Open Library files
those as separate author records — so the join has to come from Wikidata in
phase 2, with `author_aliases` carrying transliterations meanwhile.
"""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[.,\"'`’‘“”()\[\]]+")
_WS = re.compile(r"\s+")

# Suffixes Open Library appends to author records.
_AUTHOR_SUFFIX = re.compile(
    r",?\s*\b(jr|sr|phd|ph d|md|esq|ii|iii|iv|"
    r"editor|ed|trans|translator|illustrator|compiler)\b\.?\s*$",
    re.I,
)
_DATES = re.compile(r",?\s*\d{3,4}\s*[-–]\s*\d{0,4}\s*$")


def _uninvert(s: str) -> str:
    """'schapiro, meyer' -> 'meyer schapiro'. Catalogue sort order undone.

    Narrow on purpose. Only a clean two-part split is swapped, because the
    comma form is rare and inconsistent in this data — measured at 0.8% of
    author strings, and of four real examples only two were genuine
    inversions ('Schapiro, Meyer', 'Brown, Theodore, Jr.'); the others were
    'Imam ghozali, 2018' and 'Sapphire, Lofton, Ramona'. A greedy swap
    would mangle those to buy very little, and identity is anchored on
    `ol_author_key` for ~98% of works anyway.

    It still matters for `surname_token`, which feeds the endgame question
    regardless of how identity was resolved: without this, Meyer Schapiro's
    surname reads 'meyer'.
    """
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 2 or not all(parts):
        return s
    if not any(ch.isalpha() for ch in parts[1]):
        return s          # 'imam ghozali, 2018'
    return f"{parts[1]} {parts[0]}"


def canonical_author(raw: str) -> str:
    """Fold one author string to a comparable form.

    Case, diacritics, punctuation and initial spacing only. Deliberately
    does NOT filter by script — see the module docstring for the eight
    authors that cost. 'J.R.R. Tolkien' and 'J. R. R. Tolkien' both land on
    'j r r tolkien'; 'Eugène Schwartz' meets 'Eugene Schwartz'.
    """
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = _DATES.sub("", s)
    # Strip a trailing suffix before un-inverting, so 'brown, theodore, jr.'
    # becomes a clean two-part split rather than a three-part one.
    for _ in range(2):
        new = _AUTHOR_SUFFIX.sub("", s).strip(" ,")
        if new == s:
            break
        s = new
    s = _uninvert(s)
    # Dots to spaces BEFORE collapsing, so "J.K." and "J. K." agree.
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    for _ in range(2):
        new = _AUTHOR_SUFFIX.sub("", s).strip(" ,")
        if new == s:
            break
        s = new
    return _WS.sub(" ", s).strip()


def merge_key(canonical: str) -> str:
    """The key two spellings must share to be treated as one author.

    Returns '' when the canonical form carries no identifying content —
    which is the guard against the collapse described in the module
    docstring. An empty key is never merged on; the caller keeps the
    entities separate and falls back to `ol_author_key`.
    """
    if not canonical:
        return ""
    if not any(ch.isalpha() for ch in canonical):
        return ""      # digits or punctuation only
    return canonical


def surname_token(canonical: str) -> str:
    """The word a player is most likely to say on its own.

    'agatha christie' -> 'christie'. For a single-token name ('homer',
    '老子') that token is the whole thing, which is correct.

    Digit-bearing tokens are skipped, not just trimmed from the end: OL
    carries records like 'Imam ghozali, 2018' whose last token is a year,
    and the naive version answered that the author's surname was '2018'.
    Third time a stray number has tried to become a question in this
    pipeline — see `usable_token` in characters.py.
    """
    parts = [p for p in canonical.split()
             if len(p) > 1 and not any(ch.isdigit() for ch in p)]
    return parts[-1] if parts else ""


class AuthorIndex:
    """Authors of the corpus, deduplicated across spellings.

    Identity precedence, strongest first:
      1. `ol_author_key` — a real id, present on ~98% of works
      2. the canonical spelling, for records missing a key
    Aliases accumulate on whichever entity wins, so the endgame question
    and the reveal search can both match what a player actually types.
    """

    def __init__(self) -> None:
        self.by_id: dict[str, dict] = {}
        self._key_to_id: dict[str, str] = {}
        self._canon_to_id: dict[str, str] = {}

    def add(self, name: str, ol_key: str | None) -> str | None:
        canon = canonical_author(name)
        mkey = merge_key(canon)
        if not mkey and not ol_key:
            return None

        author_id = None
        if ol_key and ol_key in self._key_to_id:
            author_id = self._key_to_id[ol_key]
        elif mkey and mkey in self._canon_to_id:
            author_id = self._canon_to_id[mkey]

        if author_id is None:
            author_id = ol_key or f"name:{mkey}"
            self.by_id[author_id] = {
                "id": author_id,
                "ol_key": ol_key,
                "name": name,
                "aliases": set(),
                "surname": surname_token(canon),
                "traits": {},        # filled in phase 2 from Wikidata
                "book_count": 0,
            }

        entry = self.by_id[author_id]
        entry["aliases"].add(canon)
        if ol_key:
            self._key_to_id[ol_key] = author_id
        if mkey:
            self._canon_to_id[mkey] = author_id
        return author_id

    def note_book(self, author_id: str) -> None:
        if author_id in self.by_id:
            self.by_id[author_id]["book_count"] += 1

    def export(self) -> list[dict]:
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "surname": a["surname"],
                "aliases": sorted(a["aliases"]),
                "book_count": a["book_count"],
            }
            for a in sorted(self.by_id.values(), key=lambda x: -x["book_count"])
        ]
