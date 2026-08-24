"""
scripts/akinator/author_overrides.py — hand-set author facts, and the
aliases that decide whose facts they are.

WHAT THIS IS NOT: a new author registry. `AuthorIndex` in authors.py
already decides who an author is, and it does it well — `canonical_author`
folds case, diacritics, punctuation, honorific suffixes and "Last, First"
inversion, so "J.R.R. Tolkien" / "J. R. R. Tolkien" and "Eugène Schwartz" /
"Eugene Schwartz" merge for free and always have. Inventing a second
identity scheme on top of that would mean a hand-set fact could be keyed to
something the build never computes, and would therefore never apply. This
file is an OVERLAY on the identity that already exists, keyed by exactly
what `AuthorIndex.add()` itself would produce:

    "OL8384995A"            an Open Library author key, present on ~98% of works
    "name:colleen hoover"   `name:` + merge_key(canonical_author(name)), the
                            fallback `AuthorIndex` uses when there is no key

That is the single correctness property the whole feature hangs on, which
is why `name_key()` below IMPORTS `canonical_author`/`merge_key` rather than
reimplementing the folding. A near-copy that drifted by one rule would
produce keys that silently match nothing.

THE TWO GAPS IT CLOSES, both measured rather than assumed:

  * **Facts Wikidata does not have.** `author_traits.book_traits()`
    recomputes every author fact at each rebuild from
    `akinator_authors_wd.json` / `akinator_authors_search.json`, so fixing
    Wikidata propagates for free. There was no path at all for an author
    Wikidata does not know, or gets wrong. 2,172 of the 4,134 authors in
    the shipped corpus have no P648 match; the by-name search reaches 632
    more, under five strict rules, and deliberately refuses the rest.
  * **The same person typed two ways.** A manually-added book carries no
    `author_key`, so its author is identified by name alone. "Colleen
    Hoover" and "C. Hoover" do not canonicalize to the same string — they
    are two different names, not two spellings of one — so they become two
    entities with the facts and the book count split between them. An
    alias teaches the build that they are one.

THREE VALUES, AND `null` IS NOT "NO OVERRIDE". A fact may be set `true`,
`false`, or `null` — and `null` means "force this back to unknown", which
is a real verdict and the one this codebase's central rule most often
wants: Wikidata asserting something wrong is worse than Wikidata being
silent, because a wrong fact fans out to every book that author wrote. An
entry with no key for a question is what "no override" looks like.

WHAT AN ALIAS DOES NOT DO. It is not a retroactive merge. It teaches the
NEXT rebuild that a name belongs to an existing entity; it does not rewrite
the book count that entity was exported with, and it does not credit the
aliased book toward `author:prolific` (that counts `author_key` occurrences
in the corpus, which an aliased manual row still has none of). It does
redirect the fact lookup, so the aliased book gains the entity's hand-set
facts — that is the point of merging, and without it a merge would fix the
endgame question while leaving the facts split.

Applied through `author_traits.apply_author_facts()`, which both
`build_matrix.py` and `simulate.py` call — so the artifact and the
measurement of it cannot disagree about an override. That sharing is not
tidiness: those two files carried a byte-identical copy of the author-fact
block, and this project has already paid twice for simulate.py drifting
from the game it claims to measure.
"""
from __future__ import annotations

import json
import os

from authors import canonical_author, merge_key

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Same "one file, both readers" location as excluded.json /
# admin_corrections.json / question_overrides.json: written by
# tools/akinator_admin.py through the GitHub API, read from disk here at
# build time. A missing file is the normal state until something has been
# written, exactly like overrides.json.
AUTHOR_OVERRIDES_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator",
    "author_overrides.json"))


def name_key(name: str) -> str:
    """`name:{merge_key}` — what `AuthorIndex` calls an author with no OL key.

    Returns "" when the name carries no identifying content, which is the
    same guard `merge_key` exists for: a script-stripping normalizer once
    collapsed Homer, Tolstoy, Dostoevsky, Liu Cixin, Dazai, Murakami and
    Laozi into a single empty key. An empty key is never looked up and
    never written.
    """
    mkey = merge_key(canonical_author(name or ""))
    return f"name:{mkey}" if mkey else ""


def is_valid_key(key: str) -> bool:
    """Only the two identities `AuthorIndex` can produce.

    Enforced at write time by the admin endpoint and again here, because a
    key that is neither an OL author key nor a `name:` merge key can never
    match anything the build computes — it would sit in the file looking
    like a correction and do nothing at all.
    """
    if not isinstance(key, str) or not key:
        return False
    if key.startswith("name:"):
        return bool(key[5:]) and key == name_key(key[5:])
    return (key.startswith("OL") and key.endswith("A")
            and key[2:-1].isdigit() and len(key) <= 24)


def load_overrides(path: str = AUTHOR_OVERRIDES_PATH) -> dict[str, dict]:
    """The overlay, or {} — never an exception.

    A build must not die because a hand-edited JSON file has a trailing
    comma in it; the same reasoning as `_load_question_overrides`. Rows with
    an unusable key are dropped rather than kept, so a typo cannot look like
    a correction that is simply not working yet.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for key, entry in data.items():
        if not is_valid_key(key) or not isinstance(entry, dict):
            continue
        facts = entry.get("facts")
        aliases = entry.get("aliases")
        out[key] = {
            "facts": {q: v for q, v in (facts or {}).items()
                      if isinstance(q, str) and (v is None or isinstance(v, bool))}
            if isinstance(facts, dict) else {},
            "aliases": [a for a in (aliases or []) if isinstance(a, str) and a.strip()]
            if isinstance(aliases, list) else [],
        }
    return out


def alias_index(overrides: dict[str, dict]) -> dict[str, str]:
    """Every alias's `name:` key -> the entry that claims it.

    ONLY `name:` KEYS ARE PRODUCED, because an alias can only ever be
    consulted for an author position that has no Open Library key — see
    `AuthorIndex.add`, where a real id outranks a spelling and always has.
    Merging two OL-keyed authors is deliberately not expressible here: two
    Open Library records for one person is a fact about Open Library, and
    silently folding them would mean this file could move a book off the id
    the catalogue itself asserts.

    An entry that aliases a name already claimed by another entry loses to
    the first one seen, sorted, so the result does not depend on dict
    ordering. The admin endpoint refuses to create that collision in the
    first place; this only decides what an already-collided file means.
    """
    out: dict[str, str] = {}
    for key in sorted(overrides):
        for alias in overrides[key]["aliases"]:
            nk = name_key(alias)
            # An alias that resolves to its own entry's key teaches nothing
            # and is not a collision — skip it silently.
            if not nk or nk == key:
                continue
            out.setdefault(nk, key)
    return out


def lookup_keys(ol_keys: list[str], names: list[str],
                chosen: str | None, aliases: dict[str, str]) -> list[str]:
    """Which override entries speak for THIS book, strongest first.

    `chosen` is the author key `author_traits.facts_author()` picked — the
    same author whose Wikidata record produced the facts being overlaid.
    Looking anywhere else would blend two people: a co-authored book takes
    the lead author's facts, and an overlay keyed to the second author must
    not quietly become the book's answer.

    Two keys come back for a keyed author: the OL key, and the `name:` form
    of the same author's name. The OL key wins. The sibling exists because
    an owner working from the Authors tab sees a name, and an override
    written against the name should not silently miss a row that happens to
    carry a key — it is the same author either way.

    When the book has no `author_key` at all — every manually added row —
    the first author's `name:` key is the only identity there is, which is
    exactly the case this whole file was built for.
    """
    keys: list[str] = []
    if chosen is not None:
        keys.append(chosen)
        try:
            i = ol_keys.index(chosen)
        except ValueError:
            i = -1
        if 0 <= i < len(names):
            nk = name_key(names[i])
            if nk:
                keys.append(aliases.get(nk, nk))
    elif names:
        nk = name_key(names[0])
        if nk:
            keys.append(aliases.get(nk, nk))
    return keys


def facts_for(keys: list[str], overrides: dict[str, dict]) -> dict[str, bool | None]:
    """The hand-set facts for a book, merged across its lookup keys.

    Earlier keys win per question, so an override on the Open Library id
    beats one on the name form of the same author. Questions no entry
    mentions are absent from the result — "no override" and "overridden to
    unknown" are different answers and the caller has to be able to tell
    them apart.
    """
    out: dict[str, bool | None] = {}
    for key in reversed(keys):
        entry = overrides.get(key)
        if entry:
            out.update(entry["facts"])
    return out
