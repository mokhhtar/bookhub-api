"""
scripts/akinator/series.py — pooling volumes into the thing players think of.

THE PROBLEM, reported by the owner from live play. Harry Potter is twelve
rows in the corpus, at ranks 8, 36, 41, 48, 58, 61, 81, 214 and beyond. A
player thinking "Harry Potter" spreads their belief across all of them, so
no single volume crosses the guess threshold and the game keeps asking —
and when it finally does guess, it names a volume the player was not
thinking of. They are usually not thinking of a volume at all.

Two fixes were on the table and the owner chose one of each:

  * derivatives and translations — **merged/dropped** (corpus_filter.py):
    nobody thinks of a workbook, and a translation is the same book twice.
  * series — **pooled at guess time**, here. The volumes are real books and
    stay as candidates; only the decision of what to GUESS is taken at the
    group level.

Why pooled rather than merged: a player who is thinking of *Prisoner of
Azkaban* specifically should still be able to get it, and merging would
throw away the per-volume features that make that possible. Pooling costs
nothing in the question phase and only changes what we say out loud.

WHAT COUNTS AS THE SAME SERIES is Wikidata's P179, harvested by
harvest_works.py, with names from harvest_series_labels.py. An unnamed
series id is treated as no series at all — grouping by "Q8337" would be
correct and useless, because the point of pooling is to have a name to say.
"""
from __future__ import annotations

import json
import os
import re

from features import normalize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKS_PATH = os.path.join(REPO_ROOT, "data", "akinator_works_wd.json")
LABELS_PATH = os.path.join(REPO_ROOT, "data", "akinator_series_labels.json")

# A "series" of one is just a book. Pooling starts at two.
MIN_MEMBERS = 2

# How much of a group's belief one volume must hold before we name it rather
# than the series.
#
# Set high on purpose, and the asymmetry is the reason. "Harry Potter" is a
# correct answer to someone thinking of the series AND to someone thinking
# of any single volume. "Goblet of Fire" is correct only for the one player
# in seven who meant that volume. Naming the group is almost free; naming a
# volume is a bet.
#
# 0.60 was measured and left the reported case unfixed: a player answering
# at series level still saw "Goblet of Fire", because volume-specific
# features (page counts, cast) concentrate belief even when the player is
# being deliberately generic. 0.85 means a volume is named only when it has
# genuinely won on its own.
VOLUME_DOMINANCE = 0.85


def load_labels(path: str = LABELS_PATH) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_works(path: str = WORKS_PATH) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def common_title_prefix(titles: list[str]) -> str:
    """A name for a series Wikidata never named, from its members' titles.

    Needed because **Harry Potter's series item (Q8337) has no English
    label at all** — the label service returns the QID itself. Without a
    fallback, the single most obvious series in the corpus would be the one
    the grouping silently skipped.

    Word-wise, not character-wise: a character prefix of "Harry Potter and
    the Philosopher's Stone" and "...Chamber of Secrets" is "Harry Potter
    and the ", which is not a name. Requires at least two words so that a
    shared "The" produces nothing.
    """
    if len(titles) < 2:
        return ""
    split = [t.split() for t in titles]
    prefix: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
    while prefix and prefix[-1].lower() in ("and", "the", "a", "of", "in"):
        prefix.pop()
    return " ".join(prefix) if len(prefix) >= 2 else ""


def series_for_docs(docs: list[dict], works: dict[str, dict] | None = None,
                    labels: dict[str, str] | None = None
                    ) -> tuple[list[str | None], dict[str, str]]:
    """(series id per book index, id -> display name).

    A book gets a series only if the group ends up with at least two
    members in this corpus — a lone volume needs no pooling — and only if
    the group can be NAMED, from Wikidata or from its members' titles.
    """
    works = load_works() if works is None else works
    labels = load_labels() if labels is None else labels

    raw: list[str | None] = []
    for doc in docs:
        title = normalize(doc.get("title") or "")
        sid = None
        if title:
            for key in doc.get("author_key") or []:
                rec = works.get(f"{key}|{title}")
                if rec and rec.get("sid"):
                    sid = rec["sid"]
                    break
        raw.append(sid)

    members: dict[str, list[int]] = {}
    for i, sid in enumerate(raw):
        if sid:
            members.setdefault(sid, []).append(i)

    names: dict[str, str] = {}
    for sid, idx in members.items():
        if len(idx) < MIN_MEMBERS:
            continue
        name = labels.get(sid)
        if not name:
            name = common_title_prefix([docs[i].get("title") or "" for i in idx])
        if name:
            names[sid] = name

    out = [sid if sid in names else None for sid in raw]

    # Fallback: volumes that SAY they are volumes.
    #
    # Everything above depends on Wikidata P179, which does not cover
    # translated web novels at all. The owner played Lord of the Mysteries —
    # three volumes in the corpus, at ranks 3232, 3313 and 3403, titled
    # "Lord of the Mysteries, Volume 3: Traveler" and so on — and the game
    # never pooled them, so their belief split three ways and the mechanism
    # built for exactly this case did nothing. They even answer `unknown` to
    # "is it part of a series?".
    #
    # DELIBERATELY STRICT, because loose title matching is this project's
    # most repeated bug (Warsaw, Indiana, "A Good Girl's Guide to Murder").
    # All four must hold: an explicit volume marker with a number, an
    # identical normalized prefix before it, the same first author, and at
    # least two members. Measured over the shipped 5,000 that yields exactly
    # two groups — Heartstopper and Lord of the Mysteries — and no false
    # ones. It only ever fires where Wikidata gave nothing.
    for (prefix, _author), idx in _volume_groups(docs).items():
        if len(idx) < MIN_MEMBERS or any(out[i] for i in idx):
            continue
        sid = "title:" + prefix
        # The name is the title BEFORE the volume marker, in its original
        # case. NOT common_title_prefix, which is word-wise over the whole
        # titles and therefore returns "Heartstopper, Volume".
        m = _VOLUME_RE.match(docs[idx[0]].get("title") or "")
        name = m.group(1).strip(" ,:-") if m else ""
        if not name:
            continue
        names[sid] = name
        for i in idx:
            out[i] = sid

    # THE PARENT JOINS ITS OWN GROUP.
    #
    # `_volume_groups` only ever sees titles carrying a volume marker, so
    # a row titled plainly "Lord of the Mysteries" was excluded from the
    # group named after it — three volumes pooled together and the series
    # row standing outside, which a player sees as the volumes appearing
    # beside the book rather than folded into it. Reported from live play.
    #
    # Matched on the normalized title alone, with no author test, because
    # the row that names a series often carries no author at all: the
    # /fandom/ rows are wikis, and a wiki has no single author to record.
    # Safe because the prefix must already have produced a real group of
    # its own — this can add a member to an existing group, never invent
    # one.
    by_prefix = {sid[len("title:"):]: sid for sid in names
                 if sid.startswith("title:")}
    for i, doc in enumerate(docs):
        if out[i]:
            continue
        sid = by_prefix.get(normalize(doc.get("title") or ""))
        if sid:
            out[i] = sid

    return out, names


# "..., Volume 3: Traveler", "... Book 2", "... Part One". A NUMBER is
# required — a title merely containing the word "book" is not a volume —
# but it may be spelled out, because Heartstopper ships "Volume 1" through
# "Volume 4" and then "Volume Five". Dropping the fifth out of its own
# series would be exactly the silent hole this fallback exists to close.
_NUMBER_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|"
                 "eleven|twelve")
_VOLUME_RE = re.compile(
    r"^(.*?)[,:]?\s*\b(?:volume|vol\.?|book|part)\s*"
    r"(\d+|" + _NUMBER_WORDS + r")\b.*$", re.I)


def _volume_groups(docs: list[dict]) -> dict[tuple[str, str], list[int]]:
    """(normalized title prefix, normalized first author) -> book indices."""
    groups: dict[tuple[str, str], list[int]] = {}
    for i, doc in enumerate(docs):
        m = _VOLUME_RE.match(doc.get("title") or "")
        if not m:
            continue
        prefix = normalize(m.group(1))
        author = normalize((doc.get("author_name") or [""])[0])
        # A very short prefix is not a series name, it is a coincidence.
        if len(prefix) < 4 or not author:
            continue
        groups.setdefault((prefix, author), []).append(i)
    return groups


def group_members(series_of: list[str | None]) -> dict[str, list[int]]:
    members: dict[str, list[int]] = {}
    for i, sid in enumerate(series_of):
        if sid:
            members.setdefault(sid, []).append(i)
    return members
