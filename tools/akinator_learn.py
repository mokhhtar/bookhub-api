"""
tools/akinator_learn.py — the mind reader learns what readers know.

WHY THIS EXISTS, and it is the only thing that can. Every measurement this
project has taken says the same thing: the engine is not the bottleneck,
the data is. `--miss-rate 0.25` — a player answering from knowledge the
catalogue lacks — costs about twenty points of success rate, and no
harvest fixes it. Measured this month: traits at full coverage bought
nothing (p=0.81), a language question is worth less than an existing one,
and 91% of the works-coverage gap is books Wikidata simply does not have.

Open Library and Wikidata record what cataloguers wrote down. This records
what readers know. It is a different source, not a bigger one.

WHAT IS STORED, per (book, question): how many players gave each of the
five answers. Nothing else — no identity, no session, no free text beyond
a book the player picked from a list we already shipped.

    akin:c:{work_key}   hash, field "{question_id}:{answer}" -> count
    akin:touched        set of work_keys with counts waiting to be drained

Keys are **work keys** (`/works/OL12345W`), never popularity indices. The
monthly rebuild re-sorts every index; a count filed under one would silently
attach to a different book. Same class of error as the column-positional
matrix that `question_hash` guards.

CONSENT IS THE HARD CONSTRAINT. The page says "Nothing is sent anywhere",
and that promise is the trust the whole site trades on — the same rule that
made analytics opt-in rather than Consent-Mode-denied. So there is no
automatic submission. A game reaches this endpoint only when the player
explicitly says which book they meant, and the page copy changes in the
same release. A win with no such act sends nothing, and losing most of the
signal that way is the accepted cost.

WHAT GUARDS IT. Rate limiting per client (reusing quiz_core's limiter, the
same one the expensive Gemini routes use), a question_hash check so a stale
client cannot file counts against a question list that no longer exists,
and a consistency check that drops a submission whose answers contradict
the book it claims. None of that makes the data true — it makes a single
bad actor cheap to absorb. The clamps in the drain are what bound the
damage of anything that gets through.
"""
from __future__ import annotations

import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402
from fastapi import APIRouter, HTTPException, Request  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

log = logging.getLogger("bookhub-api.akinator_learn")

router = APIRouter(prefix="/akinator", tags=["akinator"])

# The five answers the page can send. Anything else is a malformed client.
ANSWERS = ("yes", "probably_yes", "unknown", "probably_no", "no")

COUNTS_PREFIX = "akin:c:"
TOUCHED_SET = "akin:touched"

# Counts outlive any single day but must not outlive a rebuild cycle by so
# much that they describe a question list nobody ships any more. 45 days
# gives the monthly rebuild a wide margin and still expires abandoned data.
COUNTS_TTL = 60 * 60 * 24 * 45

# Per-client daily cap. Generous — a real player finishing twenty games in a
# day is enthusiasm, not abuse — but bounded, because every submission costs
# Redis commands on a free tier.
DAILY_SUBMISSIONS = 40

# A submission whose answers contradict its claimed book this badly is not
# evidence about that book. Someone answering an all-"no" game and then
# naming Harry Potter is either confused or testing us; either way the
# counts would be noise. Deliberately loose — real players disagree with
# the catalogue constantly, and that disagreement is the ENTIRE point of
# this endpoint, so this rejects only the incoherent.
MAX_CONTRADICTION_RATE = 0.75
MIN_ANSWERS_TO_JUDGE = 8

_WORK_KEY = re.compile(r"^/(?:works|site)/[A-Za-z0-9_-]{1,64}$")
_QUESTION_ID = re.compile(r"^[a-z]+:[a-z0-9_]{1,40}$")


class Submission(BaseModel):
    """One finished game the player chose to share."""
    book: str = Field(..., max_length=80)
    question_hash: str = Field(..., max_length=64)
    answers: list[tuple[str, str]] = Field(..., max_length=60)


def _client_id(request: Request) -> str:
    """Server-derived identity. Rightmost X-Forwarded-For entry, as Render
    appends the real client to whatever the caller sent — taking the first
    would let anyone spoof their own bucket."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _contradiction_rate(answers: list[tuple[str, str]], states: dict) -> float | None:
    """How much of this game disagrees with what the matrix holds.

    None when there is too little to judge. `unknown` answers and cells the
    matrix does not know are skipped: neither can contradict anything.
    """
    judged = clashes = 0
    for qid, ans in answers:
        cell = states.get(qid)
        if cell is None or ans == "unknown":
            continue
        said_yes = ans in ("yes", "probably_yes")
        judged += 1
        if said_yes != cell:
            clashes += 1
    if judged < MIN_ANSWERS_TO_JUDGE:
        return None
    return clashes / judged


@router.post("/submit")
def submit(body: Submission, request: Request):
    """Record one game's answers against the book the player named.

    Returns 202 whether or not the counts were stored. That is deliberate:
    the page must never show a player an error for a courtesy, and a
    submission is worth exactly nothing to them. Reasons are logged, not
    returned.
    """
    from tools.quiz_core import _rate_limit          # noqa: E402

    try:
        _rate_limit("akinator_submit", DAILY_SUBMISSIONS,
                    _client_id(request), namespace="akin")
    except HTTPException:
        log.info("submission rate-limited")
        return {"ok": False, "reason": "rate_limited"}

    if not _WORK_KEY.match(body.book):
        log.info("rejected: malformed work key %r", body.book[:40])
        return {"ok": False, "reason": "bad_key"}

    pairs = [(q, a) for q, a in body.answers
             if _QUESTION_ID.match(q) and a in ANSWERS]
    if not pairs:
        return {"ok": False, "reason": "no_usable_answers"}

    # Guard: the client must be playing the question list we ship now. A
    # stale tab could otherwise file counts against retired question ids —
    # the same reason /akinator/sync refuses on a question_hash mismatch.
    live = _live_question_hash()
    if live and body.question_hash != live:
        log.info("rejected: stale question_hash %s (live %s)",
                 body.question_hash[:12], live[:12])
        return {"ok": False, "reason": "stale_client"}

    # Drop a submission that contradicts its own claimed book. This has to
    # happen HERE, not at drain time: once counts are aggregated the
    # individual game is gone and cannot be judged.
    rate = _contradiction_rate(pairs, _book_states(body.book))
    if rate is not None and rate > MAX_CONTRADICTION_RATE:
        log.info("rejected: %.0f%% of answers contradict %s",
                 rate * 100, body.book)
        return {"ok": False, "reason": "inconsistent"}

    commands = [["HINCRBY", COUNTS_PREFIX + body.book, f"{q}:{a}", 1]
                for q, a in pairs]
    commands.append(["EXPIRE", COUNTS_PREFIX + body.book, COUNTS_TTL])
    commands.append(["SADD", TOUCHED_SET, body.book])
    commands.append(["EXPIRE", TOUCHED_SET, COUNTS_TTL])

    if cache.pipeline(commands) is None:
        log.warning("submission not stored: Redis unavailable")
        return {"ok": False, "reason": "unavailable"}

    log.info("recorded %d answers for %s", len(pairs), body.book)
    return {"ok": True, "recorded": len(pairs)}


ARTIFACTS = "https://litheca.com/games/data/akinator/"

# The shipped artifacts, loaded once per process and kept: meta, the
# question ids in column order, a work_key -> row index map, and the raw
# packed matrix. ~75 KB of bytes plus a 5,000-entry dict — small enough to
# hold, and it saves a fetch per submission on a free-tier instance.
_ART: dict = {}


def _artifacts() -> dict:
    """Lazily fetch what the consistency check and hash guard need.

    Failure leaves the cache EMPTY rather than half-filled, so the next
    request retries instead of inheriting a broken state. Every caller
    treats an empty artifact set as "cannot check", which skips the guard
    rather than rejecting the submission — an outage here must not look
    like a wave of bad clients.
    """
    if _ART:
        return _ART
    try:
        import httpx
        meta = httpx.get(ARTIFACTS + "meta.json", timeout=8.0).json()
        questions = httpx.get(ARTIFACTS + "questions.json", timeout=8.0).json()
        books = httpx.get(ARTIFACTS + "books.json", timeout=15.0).json()
        matrix = httpx.get(ARTIFACTS + "matrix.bin", timeout=15.0).content
        if len(matrix) != meta["books"] * meta["bytes_per_row"]:
            log.warning("artifact size mismatch; consistency check disabled")
            return {}
        _ART.update({
            "meta": meta,
            "qids": [q["id"] for q in questions],
            "index": {b.get("k"): i for i, b in enumerate(books) if b.get("k")},
            "matrix": matrix,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("could not load artifacts: %s", str(exc)[:90])
        _ART.clear()
    return _ART


def _live_question_hash() -> str:
    """question_hash of the artifacts currently published, or "" if unknown.

    An unknown hash skips the staleness guard rather than rejecting
    everything — the same fail-open shape as the rest of this module.
    """
    return (_artifacts().get("meta") or {}).get("question_hash", "") or ""


def _book_states(work_key: str) -> dict:
    """{question_id: True/False} for one book — present vs. absent.

    `unknown` cells are OMITTED rather than returned as a third value: the
    consistency check compares a player's answer against what the table
    ASSERTS, and a cell holding no assertion cannot be contradicted. That
    is the same distinction the engine draws everywhere, and getting it
    wrong here would punish players for our gaps — exactly the failure this
    endpoint exists to fix.
    """
    art = _artifacts()
    if not art:
        return {}
    i = art["index"].get(work_key)
    if i is None:
        return {}
    nq = art["meta"]["questions"]
    bpr = art["meta"]["bytes_per_row"]
    off = i * bpr
    row = art["matrix"]
    out = {}
    for k in range(nq):
        state = (row[off + (k >> 2)] >> ((k & 3) * 2)) & 3
        if state == 1:
            out[art["qids"][k]] = True
        elif state == 0:
            out[art["qids"][k]] = False
    return out
