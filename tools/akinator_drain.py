"""
tools/akinator_drain.py — play counts become a shipped override file.

The second half of the learning loop. `akinator_learn.py` records what
players answered; this turns those counts into `overrides.json` and commits
it to the bookhub repo, where the page loads it alongside the other
deferred artifacts.

WHAT AN OVERRIDE IS. One probability, replacing what the packed matrix
would have said for one (book, question) cell:

    { "/works/OL12345W": { "genre:war": 0.83 }, ... }

Keyed by **work key**, not row index. The monthly rebuild re-sorts every
index by popularity; an override filed under one would silently move to a
different book. This is the same class of error `question_hash` guards
against in the matrix itself, and it is the single most important detail
in this file.

THE POSTERIOR, and why it is a prior plus evidence rather than a tally.
A cell with three plays should not be decided by three players. So the
matrix's own value enters as pseudo-counts:

    p = (yes + PRIOR_STRENGTH * p_matrix) / (total + PRIOR_STRENGTH)

with `yes` counting a firm yes as 1 and a "probably" as 0.65 — the same
weights the engine uses to fold an answer into belief, so the two halves of
the system agree about what a hedge is worth. "Don't know" is counted
separately and deliberately excluded from both numerator and denominator:
it is not evidence about the answer, it is evidence about the QUESTION,
and conflating them would drag every cell toward whatever the matrix
already said.

WHY IT IS CLAMPED, and to 0.90 rather than higher. PRESENCE_CONFIDENCE is
0.90 — what the engine says when a fact is VERIFIED. Play data must never
outrank that, or a brigade of agreeing players would carry more weight
than a catalogued fact, inverting the grounding hierarchy this project is
built on. The floor is 0.15, the strongest absence a richly-documented
book can express. Learned values live inside what verified data can say,
never beyond it.

MIN_PLAYS exists for the same reason: below it a cell is not written at
all, so a handful of players cannot move anything.

Run by a scheduled GitHub Action hitting POST /akinator/drain, the same
shape as akinator-sync.yml — Render holds GITHUB_PAT and can commit; CI
holds neither the corpus nor the secrets.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402
from fastapi import APIRouter, Header, HTTPException, Query  # noqa: E402
from tools.akinator_learn import (ARTIFACTS, COUNTS_PREFIX,  # noqa: E402
                                  QSTATS_KEY, TOUCHED_SET, _artifacts,
                                  _book_states)
import httpx  # noqa: E402
from tools.akinator_sync import (GITHUB_API, GITHUB_BRANCH,  # noqa: E402
                                 GITHUB_PAT, GITHUB_REPO, _commit_files,
                                 _commit_with_retry, _get_file, _head_sha,
                                 _HEADERS)

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "akinator"))
from features import absence_confidence as _absence_confidence  # noqa: E402

log = logging.getLogger("bookhub-api.akinator_drain")

router = APIRouter(prefix="/akinator", tags=["akinator"])

ARTIFACT_DIR = "games/data/akinator"
OVERRIDES_PATH = f"{ARTIFACT_DIR}/overrides.json"

# Cells the owner decided by hand, so the nightly drain leaves them alone.
#
# A SEPARATE FILE, NOT A FLAG INSIDE overrides.json, and that is deliberate:
# overrides.json is loaded by every player and its shape is a flat
# {work_key: {question_id: float}} the client reads directly. Adding
# structure to mark a locked cell would mean touching the play path for
# something only the drain needs to know. This file is never served to
# anyone — only Render reads it.
LOCKED_PATH = f"{ARTIFACT_DIR}/overrides_locked.json"

DRAIN_SECRET = os.environ.get("AKINATOR_SYNC_SECRET", "")

# How much the matrix's own value is worth, in units of players. Ten means
# a cell needs a lot of agreement to move far, which is the intent: this
# corrects the catalogue, it does not replace it.
PRIOR_STRENGTH = 10.0

# Below this many judged answers, write nothing at all.
MIN_PLAYS = 8

# Learned values may never exceed what VERIFIED data is allowed to claim.
CLAMP_HIGH = 0.90            # == features.PRESENCE_CONFIDENCE
CLAMP_LOW = 0.15             # == the strongest absence_confidence rung

# Same weights the engine folds an answer in with, so "probably" means the
# same thing to the learner as it does to the player's belief update.
ANSWER_WEIGHT = {"yes": 1.0, "probably_yes": 0.65,
                 "probably_no": 0.35, "no": 0.0}

# When a question's "don't know" rate is worth a human's attention. Both are
# REPORTING thresholds and neither retires anything — see question_health().
#
# 200 is the smaller judgement call of the two: below it a rate swings on a
# handful of games, and this counter is fed by an opt-in button rather than
# by every play, so it fills slowly.
#
# 0.40 is anchored on what the engine already does. `DK_BEFORE_DIMENSION_
# DROPPED = 2` says two "don't know"s about one dimension is enough to stop
# asking about it AT ALL for that player; a question that gets there with
# two players in five is doing the same damage to everyone. It is not a
# measured optimum — nothing has been measured here yet, which is the point
# of shipping the report first.
DK_MIN_SAMPLE = 200
DK_FLAG_RATE = 0.40


def _live_question_ids(art: dict | None = None) -> set[str]:
    """Every question id the game currently asks — packed AND cold.

    ONE function because the "is this question still live?" test is made in
    five places here, and a cold question fails the packed-only version of
    it. That failure is silent and total: the drain would file every cold
    answer under `retired` and write nothing, so the column that exists
    precisely to be filled by play would stay empty forever while the counts
    piled up in Redis and expired.

    Empty means "cannot tell" at every call site, which skips the filter
    rather than dropping everything — the same fail-open the artifact
    loader itself uses.
    """
    a = _artifacts() if art is None else art
    return set(a.get("qids") or ()) | set(a.get("cold_ids") or ())


def _matrix_prior(work_key: str, qid: str, states: dict) -> float:
    """What the shipped matrix says for this cell, as a probability.

    Mirrors the engine's own reading of a cell, including the fact that
    ABSENCE IS NOT ONE NUMBER: `absence_confidence` scales it by how well
    documented the book is, from 0.45 for a bare record to 0.15 for a rich
    one. Anchoring every absent cell to a flat value would have made the
    prior wrong in opposite directions at the two ends of the corpus.

    A cell the matrix does not assert anchors to 0.5 — the same neutral
    value the engine uses for it. Those are the cells most worth learning,
    so they get a prior rather than being skipped.
    """
    if qid not in states:
        return 0.5
    if states[qid]:
        return CLAMP_HIGH
    art = _artifacts()
    richness = (art.get("richness") or {}).get(work_key, 0)
    return _absence_confidence(richness)


def drain(dry_run: bool = False) -> dict:
    """Fold every pending count into overrides.json. One atomic commit."""
    touched = cache.set_members(TOUCHED_SET)
    if touched is None:
        # NOT the same as "nothing to do". Committing an empty file because
        # Redis was unreachable would erase every override the game has.
        return {"ok": False, "reason": "counts unreadable; nothing written"}
    if not touched:
        return {"ok": True, "books": 0, "cells": 0, "reason": "nothing pending"}

    if not _artifacts():
        return {"ok": False, "reason": "artifacts unreadable; cannot anchor priors"}

    # Both files at ONE commit, and the write below is parented on it. The
    # drain reads what the admin page writes, so an admin verdict landing
    # mid-drain would otherwise be read as absent and written back as gone —
    # the same loss `_get_file` documents, but a whole night's worth of it.
    # No retry loop here: the counts stay in Redis, so a rejected commit is
    # simply the next scheduled run's work.
    head = _head_sha()
    raw, _sha = _get_file(OVERRIDES_PATH, head or None)
    try:
        overrides = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "reason": "existing overrides.json is unparseable"}

    locked = _load_locked(head or None)

    # A book that is not in the shipped index is not a cell either, and the
    # same argument the retired-question filter below makes applies with more
    # force: an override keyed on a book nobody ships is downloaded by every
    # player, read by nothing, and never pruned. Counts can carry such a key
    # legitimately (a row dropped by a rebuild between submission and drain)
    # or maliciously (/akinator/submit validated only the key's SHAPE until
    # the check added alongside this one).
    #
    # Failing closed is free here, unlike at submission time: drain() has
    # already returned above if `_artifacts()` came back empty, so this index
    # is known-good rather than possibly-an-outage.
    index = _artifacts().get("index") or {}

    written = skipped = held = retired = unknown = 0
    books_written: set[str] = set()
    for work_key in touched:
        if work_key not in index:
            unknown += 1
            continue
        counts = cache.hgetall(COUNTS_PREFIX + work_key)
        if not counts:
            continue
        states = _book_states(work_key)

        per_question: dict[str, dict[str, int]] = {}
        for field, value in counts.items():
            qid, _, answer = field.rpartition(":")
            if not qid or answer not in ("yes", "probably_yes", "unknown",
                                         "probably_no", "no"):
                continue
            try:
                per_question.setdefault(qid, {})[answer] = int(value)
            except (TypeError, ValueError):
                continue

        live_ids = _live_question_ids()
        for qid, tally in per_question.items():
            # A RETIRED question is not a cell. Counts filed against one
            # outlive the question by design — they are keyed by question id
            # and the id simply stops being asked — so 18 of them were still
            # sitting in Redis from `fact:long`, `genre:thriller`,
            # `fact:famous` and the rest. Draining them would write override
            # keys nothing reads, growing a file every player downloads with
            # answers to questions nobody is asked.
            if live_ids and qid not in live_ids:
                retired += 1
                continue
            # A cell the owner already decided by hand is not up for a vote.
            # Play data may still be arriving on it, and it is still worth
            # keeping, but it must not quietly soften a judgement made by
            # someone who looked the book up — the whole reason the manual
            # route exists is that a person can be right before eight
            # players are.
            if qid in locked.get(work_key, ()):
                held += 1
                continue
            # "Don't know" is excluded from the judgement entirely — it says
            # nothing about the answer. It is still recorded, because how
            # ANSWERABLE a question is, is worth knowing later.
            judged = sum(n for a, n in tally.items() if a != "unknown")
            if judged < MIN_PLAYS:
                skipped += 1
                continue
            yes = sum(ANSWER_WEIGHT.get(a, 0.0) * n
                      for a, n in tally.items() if a != "unknown")
            prior = _matrix_prior(work_key, qid, states)
            p = (yes + PRIOR_STRENGTH * prior) / (judged + PRIOR_STRENGTH)
            p = max(CLAMP_LOW, min(CLAMP_HIGH, p))
            overrides.setdefault(work_key, {})[qid] = round(p, 4)
            written += 1
            books_written.add(work_key)

    if not written:
        return {"ok": True, "books": len(touched), "cells": 0, "held": held,
                "retired": retired, "unknown": unknown,
                "reason": f"{skipped} cells below the {MIN_PLAYS}-play floor"
                          + (f", {held} held by a manual decision" if held else "")
                          + (f", {retired} for retired questions" if retired else "")
                          + (f", {unknown} for books not in the shipped index"
                             if unknown else "")}

    if dry_run:
        return {"ok": True, "dry_run": True, "books": len(overrides),
                "cells": written, "skipped": skipped, "held": held,
                "retired": retired, "unknown": unknown}

    payload = json.dumps(overrides, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    # THE COMMIT MESSAGE IS THE AUDIT TRAIL, so it carries both numbers rather
    # than just the total. overrides.json is committed to a public repo on
    # every run, which makes git history the one permanent, un-expirable
    # record of what this loop has ever learned — the counts in Redis expire,
    # this does not. Cells alone cannot distinguish a busy night across the
    # catalogue from one book being pushed hard by one person, and that
    # distinction is exactly what /akinator/admin/drain/history reads back.
    ok = _commit_files({OVERRIDES_PATH: payload},
                       f"mind reader: {written} learned cell(s) across "
                       f"{len(books_written)} book(s) from play",
                       expect_head=head or None)
    if not ok:
        # Counts are left in Redis on purpose — an uncommitted drain must be
        # retryable, and these counters are idempotent to re-read.
        return {"ok": False, "reason": "commit failed; counts kept for retry"}

    # Remove exactly the members processed, never the whole set. A
    # submission arriving between the SMEMBERS above and this line would
    # otherwise be dropped from the queue while its counts sat unread —
    # a silent partial loss, which is this project's most repeated bug.
    cache.pipeline([["SREM", TOUCHED_SET, k] for k in touched])
    log.info("drained %d cells across %d books (%d held by hand, %d unknown)",
             written, len(touched), held, unknown)
    return {"ok": True, "books": len(touched), "cells": written,
            "skipped": skipped, "held": held, "retired": retired,
            "unknown": unknown}


def _load_locked(ref: str | None = None) -> dict:
    """{work_key: [question_id, ...]} the owner decided by hand.

    An unreadable or malformed file returns {} — the drain then treats every
    cell as open, which is the pre-existing behaviour and loses a lock rather
    than losing the file. Refusing to drain at all because a lock list would
    not parse would be a worse trade: the locks are a refinement, the drain
    is the loop.

    `ref` pins the read to one commit, so a caller that also reads
    overrides.json sees BOTH files as they were at the same instant. They
    are two halves of one decision and had drifted apart in exactly this
    way: two cells are locked today with no value behind them.
    """
    raw, _sha = _get_file(LOCKED_PATH, ref)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        log.warning("overrides_locked.json unparseable; treating all cells as open")
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}


# ── the owner's own hand, ahead of the eight-play floor ──────────────────
#
# MIN_PLAYS stays at 8. This does not lower it; it goes around it, for the
# one case the floor was never meant to catch.
#
# The floor exists because a handful of players must not move a cell — three
# people agreeing is not evidence. But the owner reading a submission,
# looking the book up and deciding is not three people agreeing; it is a
# different KIND of act, and applying the statistical machinery to it would
# be a category error. So a manual verdict does not compute a posterior from
# the counts at all. It writes the clamp bound directly:
#
#     yes -> CLAMP_HIGH (0.90), the same value a VERIFIED fact gets
#     no  -> CLAMP_LOW  (0.15), the strongest absence the system expresses
#
# That is exactly the ceiling play data may never exceed, which is the point:
# the owner is allowed to assert what the catalogue is allowed to assert, and
# no more. Nothing here can push a cell beyond what grounding permits.

from pydantic import BaseModel, Field                      # noqa: E402

MAX_TAUGHT_BOOKS = 60          # bounds the free tier's command budget


def _taught_rows(limit: int = MAX_TAUGHT_BOOKS) -> tuple[list, int]:
    """Every pending (book, question) tally, with what each side thinks."""
    touched = cache.set_members(TOUCHED_SET)
    if touched is None:
        raise HTTPException(status_code=503,
                            detail="play counts unreadable right now — this "
                                   "is not the same as there being none")
    total = len(touched)
    if not touched:
        return [], 0, 0
    touched = sorted(touched)[:limit]

    replies = cache.pipeline([["HGETALL", COUNTS_PREFIX + k] for k in touched])
    if replies is None:
        raise HTTPException(status_code=503, detail="play counts unreadable right now")

    locked = _load_locked()
    art = _artifacts()
    art_ok = bool(art)
    live_ids = _live_question_ids(art)
    rows = []
    retired_rows = 0
    for work_key, reply in zip(touched, replies):
        flat = reply.get("result") if isinstance(reply, dict) else None
        if not flat:
            continue
        counts = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        states = _book_states(work_key) if art_ok else {}

        per_question: dict[str, dict[str, int]] = {}
        for field, value in counts.items():
            qid, _, answer = field.rpartition(":")
            if not qid or answer not in ANSWER_WEIGHT and answer != "unknown":
                continue
            try:
                per_question.setdefault(qid, {})[answer] = int(value)
            except (TypeError, ValueError):
                continue

        for qid, tally in per_question.items():
            # Not shown, and not actionable: `apply_taught` refuses an id the
            # game does not ask, so a Yes button here would only ever 404.
            # Counted so the page can say they exist rather than pretending
            # the queue is shorter than it is.
            if live_ids and qid not in live_ids:
                retired_rows += 1
                continue
            judged = sum(n for a, n in tally.items() if a != "unknown")
            yes = sum(ANSWER_WEIGHT.get(a, 0.0) * n
                      for a, n in tally.items() if a != "unknown")
            prior = _matrix_prior(work_key, qid, states) if art_ok else None
            would = None
            if prior is not None and judged:
                would = round(max(CLAMP_LOW, min(CLAMP_HIGH,
                              (yes + PRIOR_STRENGTH * prior)
                              / (judged + PRIOR_STRENGTH))), 4)
            rows.append({
                "work_key": work_key,
                "question_id": qid,
                "tally": tally,
                "judged": judged,
                "unknown": tally.get("unknown", 0),
                # What the SHIPPED table says today: True / False / None for
                # "it does not assert anything", which is a third state and
                # not a missing value.
                "matrix": states.get(qid) if art_ok else None,
                "prior": round(prior, 4) if prior is not None else None,
                "drain_would_write": would,
                "below_floor": judged < MIN_PLAYS,
                "locked": qid in locked.get(work_key, ()),
            })
    # Readiest first: most judged answers, then biggest disagreement with
    # what the table currently holds.
    rows.sort(key=lambda r: (-r["judged"],
                             -abs((r["drain_would_write"] or 0.5) - (r["prior"] or 0.5))))
    return rows, total, retired_rows


def _audit_rows() -> tuple[list, dict]:
    """Every locked cell, against what the matrix says about it TODAY.

    THE GAP THIS CLOSES. A clamp written by `apply_taught`, `apply_batch` or
    `/authors/link` is held against the drain by `overrides_locked.json`
    FOREVER, until somebody removes it by hand. Rebuild the corpus after a
    correction — `/authors/link` now writes `author_name`/`author_key` into
    `admin_corrections.json`, so `book_traits()` can reach a different
    conclusion than the clamp did — and the old clamp goes on overriding the
    new computation silently. No warning, no indicator, nothing to look at.

    WHAT "REDUNDANT" MEANS HERE, and it is NOT "the states agree". The
    obvious test is to compare `present`/`absent` against the clamp's
    0.90/0.15 and call a match redundant, and that test is wrong in a way
    that would quietly weaken cells: removing a clamp does not return the
    cell to a STATE, it returns it to a PROBABILITY, and for an absent cell
    that probability is `absence_confidence(richness)` — 0.45 for a bare
    record, 0.25 for a middling one, and only 0.15 for a richly documented
    book. Measured over the four clamped books today: their richness is 1, 6,
    13 and 13, so `absence_confidence` gives 0.45/0.35/0.25/0.25 and NOT ONE
    absent-state clamp of 0.15 is actually redundant. Deleting the two the
    naive test calls redundant would have moved both cells.

    So the comparison is against `_matrix_prior` — the same function the
    drain anchors its posterior with, so the audit and the loop cannot hold
    different ideas of what a cell is worth without the clamp. Equal means
    the clamp is genuinely doing nothing and can go; anything else is a real
    difference for a person to look at.

    Four verdicts, and only the first is ever safe to act on automatically:

      redundant  the clamp equals what the engine would say anyway
      asserts    the matrix asserts NOTHING (unknown, prior 0.5) and the
                 clamp is the only thing answering — the common case, and
                 the reason most of these were written
      stronger   both point the same way and the clamp is firmer. An absent
                 cell on a thin record answers 0.45, and an owner who looked
                 the book up and wrote 0.15 is saying "absence really does
                 mean something here". That is a standing assertion, not
                 drift, and lumping it in with a contradiction would bury
                 the rows that matter under rows that do not.
      conflicts  the clamp and the matrix point in OPPOSITE directions. NOT
                 resolved here, on purpose: which one is right is a
                 judgement, and this is the same "human eye on the diff"
                 rule the merge cards and the taught queue already keep.

    A LOCK WITH NO CLAMP is reported too, as `orphan_lock`. Six of those were
    created by the stale-read bug in `akinator_sync._get_file` — a cell held
    against the drain forever while asserting nothing at all, which is pure
    cost. They are safe to clear by the same argument as `redundant`.
    """
    art = _artifacts()
    if not art:
        raise HTTPException(status_code=503,
                            detail="shipped artifacts unreadable right now — "
                                   "this is not the same as there being no clamps")
    head = _head_sha()
    raw, _ = _get_file(OVERRIDES_PATH, head or None)
    try:
        overrides = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=502,
                            detail="existing overrides.json is unparseable")
    if not isinstance(overrides, dict):
        overrides = {}
    locked = _load_locked(head or None)

    live_ids = _live_question_ids(art)
    index = art.get("index") or {}
    rows: list[dict] = []
    for work_key in sorted(locked):
        states = _book_states(work_key)
        shipped = work_key in index
        for qid in locked[work_key]:
            clamp = (overrides.get(work_key) or {}).get(qid)
            prior = _matrix_prior(work_key, qid, states)
            if not shipped or (live_ids and qid not in live_ids):
                # The book left the shipped list, or the question was
                # retired. Either way the lock guards a cell that is not
                # there — reported rather than deleted, because a rebuild
                # that dropped a book is worth noticing on its own.
                verdict = "stale"
            elif clamp is None:
                verdict = "orphan_lock"
            elif abs(clamp - prior) < 1e-9:
                verdict = "redundant"
            elif qid not in states:
                verdict = "asserts"
            elif (clamp >= 0.5) == states[qid]:
                # Same answer, firmer. 0.5 is the split because it is what
                # the engine treats as "tells us nothing" everywhere else.
                verdict = "stronger"
            else:
                verdict = "conflicts"
            rows.append({
                "work_key": work_key,
                "question_id": qid,
                "clamp": clamp,
                # What the engine would answer with the clamp removed. The
                # number, not the state, because the number is the thing
                # that would actually change.
                "without_clamp": round(prior, 4),
                "matrix": states.get(qid),          # True / False / None
                "richness": (art.get("richness") or {}).get(work_key),
                "verdict": verdict,
            })

    # Conflicts first — they are the only rows that need a decision.
    order = {"conflicts": 0, "orphan_lock": 1, "stale": 2, "redundant": 3,
             "stronger": 4, "asserts": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["work_key"], r["question_id"]))
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in order}
    return rows, counts


class TaughtApply(BaseModel):
    work_key: str = Field(..., max_length=220)
    question_id: str = Field(..., max_length=42)
    # "clear" removes the override AND the lock, returning the cell to the
    # matrix and to the drain's care.
    verdict: str = Field(..., max_length=8)


class TaughtApplyBatch(BaseModel):
    work_key: str = Field(..., max_length=220)
    # question_id -> "yes"|"no"|"clear", same vocabulary as TaughtApply.verdict.
    answers: dict[str, str] = Field(..., max_length=200)


# Its own router, under the SAME secret gate as every other admin write and
# for the same reason recorded in akinator_admin: `_require_admin` as a
# ROUTER dependency, because FastAPI resolves dependencies before it
# validates the body, so a caller without the secret gets 403 rather than a
# 422 that lists the schema.
from fastapi import Depends                                # noqa: E402
from tools.akinator_admin import _require_admin            # noqa: E402

admin_router = APIRouter(prefix="/akinator/admin/taught", tags=["akinator"],
                         dependencies=[Depends(_require_admin)])


@admin_router.post("")
def list_taught():
    """Everything players have taught that has not been written yet."""
    rows, books, retired = _taught_rows()
    return {"ok": True, "cells": rows, "books": books,
            "min_plays": MIN_PLAYS, "retired": retired}


@admin_router.post("/questions")
def question_health():
    """How answerable each question is, measured instead of guessed.

    REPORTS AND NEVER RETIRES, and that is a deliberate stopping point
    rather than a first draft. Retiring a question is close to a one-way
    door — its taught cells orphan the moment it stops being live, and
    `_live_question_ids` would then file every count against it as
    `retired` — so the decision belongs to a person looking at the wording,
    exactly as it did for the four questions cut by hand. This computes the
    number that person never had.

    THE SAMPLE IS BIASED AND SAYING SO IS PART OF THE ANSWER. A submission
    only happens when a player presses "Teach it this book", so these are
    games somebody cared enough about to correct. That skew applies to
    every question roughly equally, which is why the useful reading is the
    RANKING — question A is harder to answer than question B — and not the
    absolute rate.
    """
    raw = cache.hgetall(QSTATS_KEY)
    if raw is None:
        raise HTTPException(status_code=503,
                            detail="counters unreadable; nothing to report")

    art = _artifacts()
    live = _live_question_ids(art)
    cold = set(art.get("cold_ids") or ())

    per: dict[str, dict[str, int]] = {}
    for field, value in raw.items():
        qid, _, answer = field.rpartition(":")
        if not qid or answer not in ANSWER_WEIGHT and answer != "unknown":
            continue
        try:
            per.setdefault(qid, {})[answer] = int(value)
        except (TypeError, ValueError):
            continue

    rows = []
    for qid, tally in per.items():
        unknown = tally.get("unknown", 0)
        judged = sum(n for a, n in tally.items() if a != "unknown")
        total = judged + unknown
        if not total:
            continue
        rows.append({
            "question_id": qid,
            "asked": total,
            "unknown": unknown,
            "judged": judged,
            "dk_rate": round(unknown / total, 4),
            "answers": tally,
            # A question nobody ships any more still has a tally, and it is
            # worth seeing rather than hiding — it is the record of why it
            # was retired. Flagged, not filtered.
            "live": (not live) or qid in live,
            "cold": qid in cold,
        })
    rows.sort(key=lambda r: (-r["dk_rate"], -r["asked"]))

    flagged = [r for r in rows
               if r["live"] and r["asked"] >= DK_MIN_SAMPLE
               and r["dk_rate"] >= DK_FLAG_RATE]
    return {"ok": True, "questions": rows, "flagged": flagged,
            "min_sample": DK_MIN_SAMPLE, "flag_rate": DK_FLAG_RATE,
            "note": "\"Don't know\" is evidence about the QUESTION, not the "
                    "answer — the drain excludes it from every cell's "
                    "posterior for that reason, and this is the other half "
                    "of that split. Report only: nothing here retires a "
                    "question. The sample is opt-in (players who pressed "
                    "\"Teach it this book\"), so read the ranking, not the "
                    "absolute rate."}


@admin_router.post("/audit")
def audit_taught():
    """Which hand-set clamps the rebuilt matrix has caught up with."""
    rows, counts = _audit_rows()
    return {"ok": True, "cells": rows, "counts": counts,
            "books": len({r["work_key"] for r in rows}),
            "clearable": [r for r in rows
                          if r["verdict"] in ("redundant", "orphan_lock")],
            "note": "A clamp is redundant only when it equals what the engine "
                    "would answer WITHOUT it, which for an absent cell is "
                    "absence_confidence(richness) and not a flat 0.15. "
                    "Conflicts are shown and never resolved here."}


@admin_router.post("/apply")
def apply_taught(body: TaughtApply) -> dict:
    """Write one cell now, by hand, and hold it against the nightly drain."""
    import re as _re
    if not _re.match(r"^/(?:works|site|fandom)/[A-Za-z0-9_-]{1,200}$", body.work_key):
        raise HTTPException(status_code=400, detail="malformed work key")
    if not _re.match(r"^[a-z]+:[a-z0-9_]{1,40}$", body.question_id):
        raise HTTPException(status_code=400, detail="malformed question id")
    if body.verdict not in ("yes", "no", "clear"):
        raise HTTPException(status_code=400,
                            detail="verdict must be yes, no or clear")

    # THE ID MUST NAME A QUESTION THE GAME ACTUALLY ASKS. The regex above
    # only proves the shape, so `theme:magik` passed it and would have
    # written an override keyed to a question nobody ships — invisible
    # forever, and indistinguishable from a change that simply did not work.
    # Same guard, same reason, as /akinator/admin/display refusing a
    # work_key that is in no shipped row.
    #
    # It matters more now than when only the taught queue reached this
    # endpoint: that queue could only ever offer ids read out of the live
    # question list, and the editor lets a person type one.
    art = _artifacts()
    live_ids = _live_question_ids(art)
    if live_ids and body.question_id not in live_ids:
        raise HTTPException(
            status_code=404,
            detail=f"'{body.question_id}' is not a question the game asks")

    # READ AND WRITE AS ONE TRANSACTION. Both files are read at a single
    # commit and the write is parented on it, so a rapid second click cannot
    # be built on a snapshot that predates the first. Reading `?ref=main`
    # and hoping did lose six cells out of this very file — see `_get_file`.
    def build(head: str):
        raw, _sha = _get_file(OVERRIDES_PATH, head)
        try:
            overrides = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=502,
                                detail="existing overrides.json is unparseable")
        if not isinstance(overrides, dict):
            overrides = {}
        locked = _load_locked(head)

        if body.verdict == "clear":
            overrides.get(body.work_key, {}).pop(body.question_id, None)
            if not overrides.get(body.work_key):
                overrides.pop(body.work_key, None)
            rest = [q for q in locked.get(body.work_key, []) if q != body.question_id]
            if rest:
                locked[body.work_key] = rest
            else:
                locked.pop(body.work_key, None)
            value = None
        else:
            value = CLAMP_HIGH if body.verdict == "yes" else CLAMP_LOW
            overrides.setdefault(body.work_key, {})[body.question_id] = value
            held = locked.setdefault(body.work_key, [])
            if body.question_id not in held:
                held.append(body.question_id)

        return ({
            OVERRIDES_PATH: json.dumps(overrides, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"),
            LOCKED_PATH: json.dumps(locked, ensure_ascii=False,
                                    indent=1).encode("utf-8"),
        }, f"mind reader admin: {body.work_key} {body.question_id} "
           f"-> {'cleared' if value is None else value} (reviewed by hand)",
           value)

    wrote, value = _commit_with_retry(build)
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")

    # The counts have done their job for this cell. Dropping them keeps the
    # review list showing only what is still undecided; a failure here is
    # cosmetic (the cell is locked either way), so it does not fail the call.
    cache.pipeline([["HDEL", COUNTS_PREFIX + body.work_key,
                     f"{body.question_id}:{a}"]
                    for a in ("yes", "probably_yes", "unknown",
                              "probably_no", "no")])

    log.info("admin set %s %s -> %s", body.work_key, body.question_id, value)
    return {"ok": True, "value": value,
            "effect": "instant — the page reads overrides.json on load",
            "note": ("returned to the matrix and to the drain"
                     if value is None else
                     "held against the nightly drain until you clear it")}


@admin_router.post("/apply_batch")
def apply_taught_batch(body: TaughtApplyBatch) -> dict:
    """Write every reviewed cell for ONE book, in ONE commit.

    Same rule and the same two files as `apply_taught` — this is that
    function's body, unrolled over a dict instead of one (question_id,
    verdict) pair, so the Edit-a-book tab can hold N reviewed questions in
    a local draft and only ever commit once, the same "one decision, one
    write" shape already built for the Authors tab's profile editor and
    the Add-a-book review table. `apply_taught` itself is untouched and
    still used by the reader-taught queue, which reviews one cell at a
    time by construction and has no draft to batch.
    """
    import re as _re
    if not _re.match(r"^/(?:works|site|fandom)/[A-Za-z0-9_-]{1,200}$", body.work_key):
        raise HTTPException(status_code=400, detail="malformed work key")
    if not body.answers:
        raise HTTPException(status_code=400, detail="nothing to apply")
    for qid, verdict in body.answers.items():
        if not _re.match(r"^[a-z]+:[a-z0-9_]{1,40}$", qid):
            raise HTTPException(status_code=400, detail=f"malformed question id: {qid!r}")
        if verdict not in ("yes", "no", "clear"):
            raise HTTPException(status_code=400,
                                detail=f"verdict for {qid} must be yes, no or clear")

    art = _artifacts()
    live_ids = _live_question_ids(art)
    bad_ids = sorted(set(body.answers) - live_ids) if live_ids else []
    if bad_ids:
        raise HTTPException(
            status_code=404,
            detail=f"not questions the game asks: {bad_ids}")

    n = len(body.answers)

    # Same transaction as apply_taught, for the same reason. Batching lowers
    # the exposure — N cells in one commit cannot lose each other — but two
    # batches in a row race exactly as two single clicks did.
    def build(head: str):
        raw, _sha = _get_file(OVERRIDES_PATH, head)
        try:
            overrides = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=502,
                                detail="existing overrides.json is unparseable")
        if not isinstance(overrides, dict):
            overrides = {}
        locked = _load_locked(head)

        applied: dict[str, float | None] = {}
        held = locked.get(body.work_key, [])
        for qid, verdict in body.answers.items():
            if verdict == "clear":
                overrides.get(body.work_key, {}).pop(qid, None)
                held = [q for q in held if q != qid]
                applied[qid] = None
            else:
                value = CLAMP_HIGH if verdict == "yes" else CLAMP_LOW
                overrides.setdefault(body.work_key, {})[qid] = value
                if qid not in held:
                    held.append(qid)
                applied[qid] = value
        if held:
            locked[body.work_key] = held
        else:
            locked.pop(body.work_key, None)
        if not overrides.get(body.work_key):
            overrides.pop(body.work_key, None)

        return ({
            OVERRIDES_PATH: json.dumps(overrides, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"),
            LOCKED_PATH: json.dumps(locked, ensure_ascii=False,
                                    indent=1).encode("utf-8"),
        }, f"mind reader admin: {body.work_key} — {n} question(s) reviewed by hand",
           applied)

    wrote, applied = _commit_with_retry(build)
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")

    # Same cosmetic cleanup as apply_taught, over every cell just decided.
    cache.pipeline([["HDEL", COUNTS_PREFIX + body.work_key, f"{qid}:{a}"]
                    for qid in body.answers
                    for a in ("yes", "probably_yes", "unknown", "probably_no", "no")])

    log.info("admin batch-set %s: %d question(s)", body.work_key, n)
    return {"ok": True, "applied": applied,
            "effect": "instant — the page reads overrides.json on load",
            "note": f"{n} question(s) written in one commit and held against "
                    f"the nightly drain until cleared"}


@router.post("/drain")
def drain_endpoint(x_sync_secret: str = Header(default=""),
                   dry_run: bool = Query(default=False)):
    """Secret-protected, like /akinator/sync: it commits to a public repo.

    An unset secret closes the endpoint rather than opening it — a missing
    secret must never be the thing that makes a write endpoint public.
    """
    if not DRAIN_SECRET or not secrets.compare_digest(x_sync_secret.encode("utf-8"),
                                  DRAIN_SECRET.encode("utf-8")):
        raise HTTPException(status_code=403, detail="forbidden")
    result = drain(dry_run=dry_run)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "refused"))
    return result


# ── what the learning loop has actually been doing ───────────────────────
#
# THE DETECTION HALF OF H-06, and it costs nothing to keep because it is
# already being written. The per-book cap raises the price of poisoning the
# table; it does not make it impossible, and an attacker with twenty addresses
# still gets through (see SECURITY_AUDIT.md). What closes the gap between
# "cannot be prevented" and "cannot pass unnoticed" is that every drain
# COMMITS overrides.json to a public repo with its counts in the message. That
# history is permanent, ordered, attributable to a run, and revertible with a
# git revert — everything Redis counters are not, since they expire.
#
# So this endpoint invents no new storage and no new logging. It reads the
# commit log back, parses the numbers the drain already writes, and says which
# runs are unusual. The owner still decides what to do about one; nothing here
# reverts anything on its own.

drain_admin_router = APIRouter(prefix="/akinator/admin/drain", tags=["akinator"],
                               dependencies=[Depends(_require_admin)])

# A run bigger than this is worth a look. Deliberately an ABSOLUTE floor and
# not only a multiple of the recent median: with a handful of runs in history
# a median is easy to drag, and an attacker who ramps up slowly would move it
# with them. Every cell here costs MIN_PLAYS (8) judged answers, so 50 cells is
# already 400+ submissions in one night — far past anything this site's real
# traffic produces today. Tune from the Render env once real volume exists.
ALERT_CELLS_PER_RUN = int(os.environ.get("DRAIN_ALERT_CELLS", 50))

# The concentration signal, and the sharper of the two. Poisoning aims at ONE
# book, so a run that wrote many cells across one or two books looks very
# different from the same number spread over the catalogue — which is why the
# commit message carries the book count and not just the cell count.
ALERT_CELLS_PER_BOOK = int(os.environ.get("DRAIN_ALERT_CELLS_PER_BOOK", 25))

_DRAIN_MSG = re.compile(
    r"(\d+)\s+learned cell\(s\)(?:\s+across\s+(\d+)\s+book\(s\))?")


@drain_admin_router.post("/history")
def drain_history(limit: int = Query(default=30, ge=1, le=100)):
    """Every drain that ever committed, newest first, with the odd ones flagged.

    Reads git, not Redis, on purpose — see the note above this router. A run
    with no parsable counts in its message is returned with `cells: null`
    rather than dropped: the format changed once (the book count was added
    2026-08-27), and silently hiding older runs would make the history look
    like it started then.
    """
    # Reading a PUBLIC repo's log needs no credential, so the token is used
    # when present (5,000 req/hour instead of 60) and simply omitted when it
    # is not. Sending `Authorization: Bearer ` with an empty PAT — which is
    # what the shared _HEADERS builds — is rejected by httpx as an illegal
    # header value, so an unset GITHUB_PAT would otherwise turn a read-only
    # endpoint into a 502 for a reason that has nothing to do with the read.
    headers = {k: v for k, v in _HEADERS.items()
               if k != "Authorization" or GITHUB_PAT}
    try:
        r = httpx.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/commits",
                      headers=headers, timeout=30.0,
                      params={"path": OVERRIDES_PATH, "sha": GITHUB_BRANCH,
                              "per_page": limit})
        r.raise_for_status()
        commits = r.json()
    except Exception as exc:                                  # noqa: BLE001
        # 502, not an empty list: "we could not look" and "nothing happened"
        # must not read the same on a page whose whole job is noticing.
        log.warning("could not read drain history: %s", str(exc)[:120])
        raise HTTPException(status_code=502,
                            detail="could not read the commit history — this is "
                                   "NOT the same as there being no drains")

    runs = []
    for c in commits if isinstance(commits, list) else []:
        message = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
        author = (c.get("commit") or {}).get("author") or {}
        m = _DRAIN_MSG.search(message)
        cells = int(m.group(1)) if m else None
        books = int(m.group(2)) if (m and m.group(2)) else None
        per_book = round(cells / books, 1) if (cells and books) else None

        why = []
        if cells is not None and cells >= ALERT_CELLS_PER_RUN:
            why.append(f"{cells} cells in one run (alert at {ALERT_CELLS_PER_RUN})")
        if per_book is not None and per_book >= ALERT_CELLS_PER_BOOK:
            why.append(f"{per_book} cells per book (alert at {ALERT_CELLS_PER_BOOK})")

        # Both kinds of write land in this file and both belong in the log,
        # but only one of them is a vote. A hand edit is the owner deciding a
        # cell after looking the book up, so it is never "unusual" however
        # many cells it touches; flagging it would train the owner to dismiss
        # the banner, which is the only failure mode a monitor really has.
        runs.append({"sha": c.get("sha", "")[:10], "date": author.get("date", ""),
                     "message": message[:160], "cells": cells, "books": books,
                     "per_book": per_book, "kind": "drain" if m else "manual",
                     "flagged": bool(why), "why": why})

    drains = [r_ for r_ in runs if r_["kind"] == "drain"]
    counted = [r_["cells"] for r_ in drains if r_["cells"] is not None]
    return {"ok": True, "runs": runs,
            "flagged": sum(1 for r_ in runs if r_["flagged"]),
            "total_runs": len(runs), "drains": len(drains),
            "manual": len(runs) - len(drains),
            "largest_run": max(counted) if counted else 0,
            "thresholds": {"cells_per_run": ALERT_CELLS_PER_RUN,
                           "cells_per_book": ALERT_CELLS_PER_BOOK},
            "note": "Learned cells are clamped to [0.15, 0.90] and every run "
                    "here is a commit — a bad one is revertible with git revert."}
