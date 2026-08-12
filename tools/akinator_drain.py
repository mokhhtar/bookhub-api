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
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402
from fastapi import APIRouter, Header, HTTPException, Query  # noqa: E402
from tools.akinator_learn import (ARTIFACTS, COUNTS_PREFIX,  # noqa: E402
                                  TOUCHED_SET, _artifacts, _book_states)
from tools.akinator_sync import _commit_files, _get_file  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "akinator"))
from features import absence_confidence as _absence_confidence  # noqa: E402

log = logging.getLogger("bookhub-api.akinator_drain")

router = APIRouter(prefix="/akinator", tags=["akinator"])

ARTIFACT_DIR = "games/data/akinator"
OVERRIDES_PATH = f"{ARTIFACT_DIR}/overrides.json"

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

    raw, _sha = _get_file(OVERRIDES_PATH)
    try:
        overrides = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "reason": "existing overrides.json is unparseable"}

    written = skipped = 0
    for work_key in touched:
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

        for qid, tally in per_question.items():
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

    if not written:
        return {"ok": True, "books": len(touched), "cells": 0,
                "reason": f"{skipped} cells below the {MIN_PLAYS}-play floor"}

    if dry_run:
        return {"ok": True, "dry_run": True, "books": len(overrides),
                "cells": written, "skipped": skipped}

    payload = json.dumps(overrides, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    ok = _commit_files({OVERRIDES_PATH: payload},
                       f"mind reader: {written} learned cell(s) from play")
    if not ok:
        # Counts are left in Redis on purpose — an uncommitted drain must be
        # retryable, and these counters are idempotent to re-read.
        return {"ok": False, "reason": "commit failed; counts kept for retry"}

    # Remove exactly the members processed, never the whole set. A
    # submission arriving between the SMEMBERS above and this line would
    # otherwise be dropped from the queue while its counts sat unread —
    # a silent partial loss, which is this project's most repeated bug.
    cache.pipeline([["SREM", TOUCHED_SET, k] for k in touched])
    log.info("drained %d cells across %d books", written, len(touched))
    return {"ok": True, "books": len(touched), "cells": written,
            "skipped": skipped}


@router.post("/drain")
def drain_endpoint(x_sync_secret: str = Header(default=""),
                   dry_run: bool = Query(default=False)):
    """Secret-protected, like /akinator/sync: it commits to a public repo.

    An unset secret closes the endpoint rather than opening it — a missing
    secret must never be the thing that makes a write endpoint public.
    """
    if not DRAIN_SECRET or x_sync_secret != DRAIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    result = drain(dry_run=dry_run)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "refused"))
    return result
