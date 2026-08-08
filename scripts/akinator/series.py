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
    return out, names


def group_members(series_of: list[str | None]) -> dict[str, list[int]]:
    members: dict[str, list[int]] = {}
    for i, sid in enumerate(series_of):
        if sid:
            members.setdefault(sid, []).append(i)
    return members
