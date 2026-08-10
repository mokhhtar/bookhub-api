"""
scripts/akinator/corpus_filter.py — one row per thing a player can think of.

WHY THIS EXISTS. The owner played the live game and reported that it takes
too many questions and failed on *Atomic Habits* — which is rank 0, the
single most-read book in the corpus. Investigating turned all of it into
one problem: **the corpus holds several rows for one thing in a player's
head**, belief splits across them, nothing crosses the guess threshold, and
the game keeps asking.

    rank 0     Atomic Habits                            10 subjects
    rank 849   Companion Workbook : Atomic Habits        0 subjects
    rank 2253  Summary of Atomic Habits by James Clear   0 subjects
    rank 2285  Atomic Habits Journal                     0 subjects
    rank 3614  Atomic Habits Daily Journal               0 subjects

Worse than a split: the four companions have no subjects at all, so nearly
every feature reads "absent", and a run of "no" answers can lift them above
the real book.

This module removes the two kinds that should not be candidates at all
(derivatives, translations). Series are handled differently — they are real
books and stay, pooled at guess time. See the build plan.

PRECISION OVER RECALL, deliberately. Dropping a real book is a silent
failure the player experiences as "it doesn't know that one"; keeping a
workbook merely costs a little belief mass. So every rule here is paired:
a marker word alone is never enough.

That is not caution for its own sake — the first attempt at this WAS a bare
title regex, and among its hits were *A Good Girl's Guide to Murder* and
*The Hitchhiker's Guide to the Galaxy*. Same substring trap as "New England
is in Britain" and "Warsaw is a war book". Every rule below is validated
against controls in the docstring of the function that uses it.
"""
from __future__ import annotations

import re

from corrections import apply_corrections

# How many books ship, and therefore how many any measurement of the game
# must use. Not a tuning knob: success was measured at 76% on 1,000 books,
# 68% on 2,500 and 40% on 10,000, because a book at rank 10,000 carries half
# the subjects of one at rank 1,000. 5,000 is the measured ceiling for the
# sources we have.
#
# It lives HERE, imported by both build_matrix.py and simulate.py, because
# both defaulted to "the whole corpus" independently — 19,890 books — and so
# the builder produced a game nobody plays while the simulator measured one
# nobody ships. Given the numbers above, that gap is worth tens of points of
# success rate. One definition, two callers.
SHIPPED_BOOKS = 5000

# Words that a derivative product usually has and a novel usually does not.
# Necessary but NOT sufficient — see `is_derivative`.
_MARKER = re.compile(
    r"\b(summary|summaries|workbook|work book|study guide|analysis of|"
    r"key takeaways|conversation starters|cliffs?notes|sparknotes|"
    r"coloring book|colouring book|activity book|boxed set|box set|omnibus|"
    r"complete collection|journal|notebook|planner|companion|trivia|"
    r"quiz book)\b",
    re.I,
)

# "Summary of X by Y" / "Study Guide ... by Y" — the shape of a book about
# another book.
_ABOUT_ANOTHER_BOOK = re.compile(
    r"\b(summary|workbook|study guide|analysis|guide)\b.*\bby\s+[A-Z]", re.I)

# Publishers whose entire catalogue is summaries of other people's books.
_SUMMARY_PUBLISHERS = (
    "supersummary", "macat", "bookrags", "instaread", "quickread",
    "mtg editorial", "sparknotes", "cliffsnotes", "everest media",
    "book pro", "podium press", "fizzy diamond",
)


def is_derivative(doc: dict) -> bool:
    """A workbook, summary, journal or box set rather than a book.

    Requires a marker word AND a second signal, because the marker alone
    has false positives that matter. Validated on the corpus: flags 11 of
    the top 5,000 and clears every control it is aimed near —
    *A Good Girl's Guide to Murder*, *The Hitchhiker's Guide to the
    Galaxy*, *The Diary of a Young Girl*, *Bridget Jones's Diary* and
    *Diary of a Wimpy Kid* all survive.
    """
    title = doc.get("title") or ""
    if not _MARKER.search(title):
        return False
    author = " ".join(doc.get("author_name") or []).lower()
    if any(p in author for p in _SUMMARY_PUBLISHERS):
        return True
    if _ABOUT_ANOTHER_BOOK.search(title):
        return True
    # A marker word and no subject metadata at all: a real book this
    # popular would have some.
    return not doc.get("subject")


def find_translations(docs: list[dict], median_richness: float) -> set[int]:
    """Indices of rows that are very likely a translation of an earlier row.

    Signal: the row is not in English, its author already has a
    more-popular row in the corpus, and it is thinly described. Catches
    *Los Cuatro Acuerdos* against *The Four Agreements*, *Como ganar
    amigos* against *How to Win Friends*, *Essencialismo* against
    *Essentialism*.

    THE RICHNESS CONDITION IS THE GUARD, and it is there because the first
    two signals alone are not enough: "Believe It to Achieve It" and
    "Until Love Sets Us Apart" both matched them and are simply *other
    books by the same author*, not translations. Requiring the row to be
    thinly described as well means a wrong drop only ever removes a row
    that was contributing almost nothing — the trade the module docstring
    argues for, made explicit.

    `docs` must be sorted by popularity, so an earlier index is a more
    popular row.
    """
    english_by_author: dict[str, int] = {}
    out: set[int] = set()

    for i, doc in enumerate(docs):
        langs = doc.get("language") or []
        keys = doc.get("author_key") or []
        if "eng" in langs:
            for key in keys:
                english_by_author.setdefault(key, i)
            continue
        if not langs:
            continue        # no language recorded: not evidence of anything
        if len(doc.get("subject") or []) > median_richness:
            continue        # well described; keep it as its own candidate
        for key in keys:
            earlier = english_by_author.get(key)
            if earlier is not None and earlier < i:
                out.add(i)
                break
    return out


def filter_corpus(docs: list[dict], verbose: bool = True) -> list[dict]:
    """Drop derivatives and translation duplicates. `docs` sorted by rank.

    Also applies the hand-verified metadata corrections. This is the one
    function both build_matrix.py and simulate.py pass their documents
    through, so applying them here is what stops the shipped artifact and
    the measurement of it disagreeing about a fact.
    """
    fixed = apply_corrections(docs, verbose=verbose)
    if fixed and verbose:
        print(f"Corrections: {fixed} verified-wrong field(s) fixed")

    richness = sorted(len(d.get("subject") or []) for d in docs)
    median = richness[len(richness) // 2] if richness else 0

    translations = find_translations(docs, median)
    kept, dropped_deriv, dropped_trans = [], [], []
    for i, doc in enumerate(docs):
        if is_derivative(doc):
            dropped_deriv.append(doc)
        elif i in translations:
            dropped_trans.append(doc)
        else:
            kept.append(doc)

    if verbose:
        print(f"Corpus filter: {len(docs)} -> {len(kept)} "
              f"({len(dropped_deriv)} derivative, "
              f"{len(dropped_trans)} translation duplicates)")
        for label, rows in (("derivative", dropped_deriv),
                            ("translation", dropped_trans)):
            for d in rows[:6]:
                print(f"    dropped ({label}): {(d.get('title') or '')[:52]}")
    return kept
