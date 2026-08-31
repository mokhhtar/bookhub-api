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
import time

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

# The same answers again, tallied per QUESTION instead of per (book,
# question) — one hash for the whole game, fields "{question_id}:{answer}".
#
# WHY A SECOND COUNTER RATHER THAN A SUM OF THE FIRST. The per-book hashes
# are cumulative and the drain never deletes them; it only removes members
# from `akin:touched`. So a book drained on three nights has been read three
# times, and adding up what any one drain saw would count the same answers
# again on every run. There is also no set of "every book with counts" left
# to walk once the queue is emptied. A counter incremented at the source is
# exact, needs no scan, and cannot double-count.
#
# WHAT IT IS FOR, and the distinction is the whole point: `unknown` is
# excluded from the per-cell posterior because it says nothing about the
# ANSWER — but it says a great deal about the QUESTION. Four questions were
# already cut by hand for being unanswerable ("how many editions is it in",
# "is it well rated") after a person noticed. This is that same judgement,
# measured, and it is the only way to notice the next one without waiting
# for someone to meet it in a real game.
#
# ~50 extra pipeline commands per submission, in the pipeline that already
# runs. At this game's traffic that is nowhere near the free tier's daily
# budget, and it buys a number no scan could reconstruct afterwards.
QSTATS_KEY = "akin:q"

# Counts outlive any single day but must not outlive a rebuild cycle by so
# much that they describe a question list nobody ships any more. 45 days
# gives the monthly rebuild a wide margin and still expires abandoned data.
COUNTS_TTL = 60 * 60 * 24 * 45

# Per-client daily cap. Generous — a real player finishing twenty games in a
# day is enthusiasm, not abuse — but bounded, because every submission costs
# Redis commands on a free tier.
DAILY_SUBMISSIONS = 40

# Per-client, per-BOOK daily cap, and the arithmetic is why it exists. One
# submission carries every answer of one game, so it moves ~47 cells of ONE
# book at once. Against the 40 above, that made a single IP with no account
# enough to decide a book outright in a single day:
#
#   8 submissions  -> every cell clears MIN_PLAYS and starts being written
#   40 submissions -> p = (40 + 10*0.5) / (40 + 10) = 0.90 = CLAMP_HIGH
#
# 0.90 is PRESENCE_CONFIDENCE — what the game says about a VERIFIED fact. The
# drain's own docstring says play data must never outrank verified data; at 40
# plays it tied it, for free. The contradiction guard does not help against
# the surgical version: answering honestly on 46 of 47 questions and lying on
# the 47th is a 2% contradiction rate against a 75% threshold.
#
# At 2/day the same 40 plays need 20 distinct IPs in a day, or one IP for 20
# days. Chosen over a distinct-client set (which would mean storing a per-book
# fingerprint of every player, contradicting this module's "no identity, no
# session" promise) and over Turnstile (worth adding only if abuse is actually
# observed): this reuses the limiter already here, and its stored key is the
# same (kind, day, client) counter shape that already exists, so it adds no
# new privacy surface at all. A reader replaying a favourite a third time in
# one day simply stops contributing, which costs nothing.
MAX_PER_BOOK_DAILY = 2

# A submission whose answers contradict its claimed book this badly is not
# evidence about that book. Someone answering an all-"no" game and then
# naming Harry Potter is either confused or testing us; either way the
# counts would be noise. Deliberately loose — real players disagree with
# the catalogue constantly, and that disagreement is the ENTIRE point of
# this endpoint, so this rejects only the incoherent.
MAX_CONTRADICTION_RATE = 0.75
MIN_ANSWERS_TO_JUDGE = 8

# Must match akinator_admin.py and akinator_suggest.py, which were both
# widened away from an earlier `(works|site)` / 64-char version that this copy
# kept. What that cost, measured against the live catalogue: 76 shipped books
# could not be taught AT ALL — every /fandom/ row (Worm, Solo Leveling, Lord of
# the Mysteries, Mushoku Tensei, the whole web-novel catalogue) plus three
# /site/ keys longer than 64 characters. The page sends books[i].k verbatim and
# this endpoint answers 202 either way, so a player teaching one of them saw
# success and nothing was recorded, for as long as those rows have shipped.
#
# This regex is deliberately NOT the security boundary — the index-membership
# check in submit() is, so being a little too broad here costs nothing while
# being too narrow silently deletes a feature.
_WORK_KEY = re.compile(r"^/(?:works|site|fandom)/[A-Za-z0-9_-]{1,200}$")
_QUESTION_ID = re.compile(r"^[a-z]+:[a-z0-9_]{1,40}$")


class Submission(BaseModel):
    """One finished game the player chose to share."""
    # 220, matching akinator_suggest.SuggestRequest.work_key, and NOT the 80
    # this used to be: the longest /site/ key that actually ships is 85
    # characters, so 80 rejected three real rows here with a 422 before the
    # handler could even answer its usual soft 202. Same lesson, same three
    # rows, as the _WORK_KEY width below — a limit that looks obviously
    # generous is still a guess until it is measured against the catalogue.
    book: str = Field(..., max_length=220)
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

    # A WELL-FORMED KEY IS NOT A BOOK. Without this, any string matching the
    # regex above is accepted, stored, and eventually written into
    # overrides.json by the drain — a file every player downloads — for a book
    # that does not exist. `_book_states` cannot catch it: it answers {} both
    # for "no such book" and "artifacts unreachable", so the contradiction
    # check below skips rather than rejects.
    #
    # Fails OPEN when the artifacts are unreachable, unlike the same check in
    # akinator_suggest.py (which fails closed): a reader there is waiting on a
    # form and can be told to retry, while this is a silent courtesy at the end
    # of a game, and refusing a whole outage's worth of real submissions costs
    # more than the counts a fail-open lets through. The drain-side check is
    # the authoritative one and CAN fail closed, because drain() already
    # refuses to run at all without artifacts.
    art = _artifacts()
    if art and body.book not in art["index"]:
        log.info("rejected: %r is not a shipped book", body.book[:60])
        return {"ok": False, "reason": "unknown_book"}

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

    # Charged LAST, so only a submission that is actually about to be stored
    # spends the book's budget — a stale tab or an incoherent game must not
    # consume the quota that real plays of this book need. See
    # MAX_PER_BOOK_DAILY for what this bounds and why it is a counter rather
    # than a set of client fingerprints.
    try:
        _rate_limit(f"akinator_book:{body.book}", MAX_PER_BOOK_DAILY,
                    _client_id(request), namespace="akin")
    except HTTPException:
        log.info("per-book cap reached for %s", body.book[:60])
        return {"ok": False, "reason": "rate_limited"}

    commands = [["HINCRBY", COUNTS_PREFIX + body.book, f"{q}:{a}", 1]
                for q, a in pairs]
    # The same answers again, per question rather than per cell. Refreshed
    # on every write, so the TTL only ever expires it if the game goes 45
    # days with nobody playing — self-cleaning without a retired question's
    # tally lingering for a year.
    commands += [["HINCRBY", QSTATS_KEY, f"{q}:{a}", 1] for q, a in pairs]
    commands.append(["EXPIRE", QSTATS_KEY, COUNTS_TTL])
    commands.append(["EXPIRE", COUNTS_PREFIX + body.book, COUNTS_TTL])
    commands.append(["SADD", TOUCHED_SET, body.book])
    commands.append(["EXPIRE", TOUCHED_SET, COUNTS_TTL])

    if cache.pipeline(commands) is None:
        log.warning("submission not stored: Redis unavailable")
        return {"ok": False, "reason": "unavailable"}

    log.info("recorded %d answers for %s", len(pairs), body.book)
    return {"ok": True, "recorded": len(pairs)}


ARTIFACTS = "https://litheca.com/games/data/akinator/"

# The shipped artifacts: meta, the question ids in column order, a work_key ->
# row index map, and the raw packed matrix. ~75 KB of bytes plus a 5,000-entry
# dict — small enough to hold, and it saves a fetch per submission on a
# free-tier instance.
_ART: dict = {}
_ART_AT = 0.0

# An hour, the same figure tools/fandom.py uses for wikis.json, and for the
# same reason: these are published artifacts that change on a deploy, not
# constants. Held for the life of the PROCESS originally, which had a silent
# failure mode — after a rebuild changed the question list, the server kept
# answering with the old question_hash and rejected EVERY submission as
# stale_client until Render happened to restart. Nothing surfaced that; the
# endpoint answers 202 regardless.
_ART_TTL = 3600


def _artifacts() -> dict:
    """Lazily fetch what the consistency check and hash guard need.

    Failure leaves the PREVIOUS artifacts in place rather than clearing them
    (fail-open to stale, like fandom.py's loader) and, on the very first fetch
    ever failing, leaves the cache empty rather than half-filled. Every caller
    treats an empty artifact set as "cannot check", which skips the guard
    rather than rejecting the submission — an outage here must not look
    like a wave of bad clients.
    """
    global _ART_AT
    now = time.time()
    if _ART and now - _ART_AT < _ART_TTL:
        return _ART
    try:
        import httpx
        meta = httpx.get(ARTIFACTS + "meta.json", timeout=8.0).json()
        questions = httpx.get(ARTIFACTS + "questions.json", timeout=8.0).json()
        books = httpx.get(ARTIFACTS + "books.json", timeout=15.0).json()
        matrix = httpx.get(ARTIFACTS + "matrix.bin", timeout=15.0).content
        # The cold questions — asked during play, but with no column in
        # matrix.bin and no entry in questions.json, because they carry no
        # build-time data at all. Kept in a SEPARATE key rather than appended
        # to `qids`: that list is indexed BY COLUMN in _book_states, so
        # anything added to it is a positional claim about the packed matrix.
        # A missing file is the normal case; failing soft to [] costs only
        # the drain's ability to write cold cells that run.
        try:
            cold = httpx.get(ARTIFACTS + "cold_questions.json", timeout=8.0).json()
        except Exception:  # noqa: BLE001
            cold = []
        if len(matrix) != meta["books"] * meta["bytes_per_row"]:
            log.warning("artifact size mismatch; consistency check disabled")
            return _ART          # stale if we have it, empty if we never did
        fresh = {
            "meta": meta,
            "qids": [q["id"] for q in questions],
            "cold_ids": [q["id"] for q in cold
                         if isinstance(q, dict) and isinstance(q.get("id"), str)],
            "index": {b.get("k"): i for i, b in enumerate(books) if b.get("k")},
            # Richness drives absence_confidence, which the drain needs to
            # anchor an absent cell's prior at the right strength: 0.45 for
            # a bare record, 0.15 for a rich one.
            "richness": {b.get("k"): (b.get("r") or 0)
                         for b in books if b.get("k")},
            "matrix": matrix,
        }
        # clear()+update() rather than a rebind, the same shape fandom.py uses:
        # a rebuild can REMOVE a row, and merging over the old dict would keep
        # a work_key the game no longer ships — which the membership check in
        # submit() would then read as a real book.
        _ART.clear()
        _ART.update(fresh)
        _ART_AT = now
    except Exception as exc:  # noqa: BLE001
        # Keep whatever we already had: a refresh failing is not evidence that
        # the artifacts changed, and going empty here would disable the
        # membership and hash guards for everyone until the next success.
        log.warning("could not refresh artifacts: %s", str(exc)[:90])
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
