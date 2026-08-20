"""
tools/akinator_suggest.py — what a reader can tell the mind reader that the
existing learning loop structurally cannot carry.

READ THIS BEFORE ADDING ANYTHING HERE. Half of this loop already exists and
is NOT this module: "Teach it this book" -> `POST /akinator/submit`
(`akinator_learn.py`) -> nightly drain -> `overrides.json`, rate-limited,
clamped, and fully automatic with no human in the path. That path is the
right one for "the table is wrong about a book that is in the game", and it
scales because a count needs no review.

THE GAP THIS FILLS is the one thing that path cannot express. It sends
`books[bookIndex].k` — a key the reader PICKED FROM THE SHIPPED LIST. So a
reader who was thinking of a book that is not in the game at all has no way
to say so, and that is the single most valuable thing they could tell us.
Same for a row whose year is wrong (a count against a question cannot say
"1948, not 2024") and a row whose title is in a script the reader cannot
read (the 人間失格 / "No Longer Human" case, which cost a real exclusion of
the BETTER of the two rows).

None of those three is a count. Each is a fact that has to be checked by a
person, which is why this one gets a queue and the other does not.

    THREE READER REASONS                        ADMIN RESOLVES IT AS
    missing     "the book I meant isn't here"   /book
    wrong_year  "the year is wrong"             /correction
    unreadable  "this title is unreadable"      /display, or /exclude

The admin picks the action; the reason only decides which ones are offered.
`unreadable` maps to two on purpose: the owner's first instinct on 人間失格
was to drop the row, and sometimes that IS right (a duplicate of a row the
game already has). Renaming and excluding are the same judgement made two
ways, so both are one click from the same queue entry.

NO STRANGER-WRITTEN TEXT IS EVER STORED. This is the hard constraint, and
the trick that satisfies it completely: what a reader types is used as a
SEARCH QUERY and then discarded. `missing` and `unreadable` store the title
and author that `book_data.resolve_book()` (Google Books -> Open Library,
the same grounding every tool here uses) returned for that query, not the
query. `wrong_year` stores a bounded integer. Everything else in a queue
entry is a `work_key` we shipped ourselves. So there is no field in Redis,
in the admin page, or in any commit that a stranger authored — an
unverifiable book is refused at INTAKE, not left for review.

WHERE IT LIVES, AND WHY NOT ANYWHERE ELSE. Redis, with a TTL, holding only
the pending queue. Firestore was rejected: the web key is public by design
and the account is what makes it harmless, so `allow create: if true` means
anyone writes via REST bypassing the page, Firestore has no built-in rate
limit, and the free Spark plan's 20,000 daily writes are SHARED with
comments, ratings, likes and every user's library — draining them breaks
all of those (`H-03` in bookhub/SECURITY_AUDIT.md). The bookhub repo was
rejected: public, permanent, and a commit per suggestion. Anonymous auth
was rejected: unlimited identities limit nothing.

APPROVAL IS WHAT BECOMES PERMANENT, not the suggestion. An approved entry
is replayed through the EXISTING `/akinator/admin/*` handler, which commits
to the bookhub repo exactly as it does when the owner types the same thing
by hand. A rejected one is deleted. Redis holds nothing that outlives a
decision.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
from typing import Literal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache                                                      # noqa: E402
from fastapi import APIRouter, Depends, HTTPException, Request    # noqa: E402
from pydantic import BaseModel, Field                             # noqa: E402

from tools.akinator_admin import (                                # noqa: E402
    BookRequest,
    CorrectionRequest,
    DisplayRequest,
    ExcludeRequest,
    _require_admin,
    book as admin_book,
    correction as admin_correction,
    display as admin_display,
    exclude as admin_exclude,
)

log = logging.getLogger("bookhub-api.akinator_suggest")

SUGGEST_PREFIX = "akin:sg:"
PENDING_SET = "akin:sg:pending"

# Ninety days. Longer than akinator_learn's 45-day counts on purpose: a count
# is one game's worth of evidence and there will be more next week, but a
# reader who noticed the one wrong year in five thousand rows is not going to
# notice it again. Losing that to a TTL because the owner did not open the
# admin page for seven weeks is the wrong trade. Still bounded — nothing here
# is permanent until it is approved, and a cold cache must break nothing.
SUGGEST_TTL = 60 * 60 * 24 * 90

# Per-client daily cap. Tighter than /submit's 40, because every request
# that gets this far costs a live Google Books / Open Library resolve.
# Fifteen rather than the eight first written here: the smoke test showed
# eight is not much once a reader retypes a title they are unsure of, and
# the limiter is charged only for requests that reach an external call, so
# an honest reader hunting for the right spelling is not the thing being
# bounded. Reuses quiz_core's limiter — the same one the expensive Gemini
# routes and /akinator/submit already share.
DAILY_SUGGESTIONS = 15

# A review queue nobody can get through is a queue nobody opens. Past this,
# intake refuses and says so: the answer is for the owner to review, not for
# Redis to keep absorbing. Also bounds the free tier's command budget, since
# listing the queue reads every entry.
MAX_PENDING = 300

REASONS = ("missing", "wrong_year", "unreadable")

# Same shape as akinator_admin's, and for the same measured reason: the
# longest /site/ key that actually ships is 85 characters, so the 64 that
# looks obviously safe would refuse to act on rows the game already serves.
_WORK_KEY = re.compile(r"^/(?:works|site|fandom)/[A-Za-z0-9_-]{1,200}$")
_ID = re.compile(r"^[0-9a-f]{16}$")


# ── intake ───────────────────────────────────────────────────────────────

class SuggestRequest(BaseModel):
    """One reader's report. Note what is NOT here: any free-text field.

    `title` and `author` are search queries, not content — they are resolved
    and thrown away (see `_resolve_or_404`). There is deliberately no
    "anything else you'd like to tell us" box, and adding one would break
    this module's central promise.
    """
    reason: Literal["missing", "wrong_year", "unreadable"]
    work_key: str = Field(default="", max_length=220)
    title: str = Field(default="", max_length=200)
    author: str = Field(default="", max_length=120)
    year: int | None = Field(default=None, ge=1, le=2100)


def _client_id(request: Request) -> str:
    """Server-derived identity. RIGHTMOST X-Forwarded-For entry, because
    Render appends the real client to whatever the caller sent — taking the
    first would let anyone rotate their own rate-limit bucket at will. Same
    reasoning, same code, as akinator_learn._client_id."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _shipped_index() -> dict:
    """{work_key: row} for the game as currently published, or {} if unknown.

    Borrows akinator_learn's already-cached artifact fetch rather than
    pulling books.json a second time — it is the same file, loaded once per
    process, and this endpoint needs only the key set out of it.
    """
    from tools.akinator_learn import _artifacts          # noqa: E402
    return (_artifacts() or {}).get("index") or {}


_TITLES: dict = {}


def _shipped_titles() -> set:
    """Normalised titles of every shipped row, or an empty set if unknown.

    An empty set SKIPS the "already in the game" check rather than blocking
    the suggestion — the worst case is one queue entry the owner closes in a
    second, against turning an artifact hiccup into a refusal that tells a
    reader their real book does not exist.
    """
    from features import normalize                       # noqa: E402
    if _TITLES:
        return _TITLES["set"]
    try:
        import httpx
        from tools.akinator_learn import ARTIFACTS       # noqa: E402
        books = httpx.get(ARTIFACTS + "books.json", timeout=15.0).json()
    except Exception as exc:                             # noqa: BLE001
        log.warning("could not load books.json for the title check: %s",
                    str(exc)[:90])
        return set()
    _TITLES["set"] = {normalize(b.get("t") or "") for b in books}
    return _TITLES["set"]


def _catalogue_reachable() -> bool:
    """Did we LOOK and find nothing, or could we not look at all?

    THIS EXISTS BECAUSE `resolve_book` CANNOT TELL THE TWO APART. It answers
    `found=False` for a book that does not exist and for a run where every
    provider errored, and both were observed in one smoke-test session:
    Google Books returned 429 for the whole run while Open Library timed out
    intermittently, so the SAME query for "No Longer Human" came back
    found=False and then found=True fourteen seconds later.

    Which one it was decides what a reader is told. Without this check the
    endpoint answers "no book by that title could be found — check the
    spelling" to someone who typed a real book correctly during an outage —
    asserting a fact we do not have, at the exact moment they were doing us
    a favour. That is failure shape #4 in this project's tally ("unavailable
    is not absent") and the standing rule that a network failure must never
    be read as "no data".

    One extra request, only ever on the failure path, against a query that
    must return something. If even that comes back empty we are blind, and
    saying so is the honest answer.
    """
    import httpx                                          # noqa: E402
    from book_data import OPEN_LIBRARY_SEARCH_API         # noqa: E402
    try:
        r = httpx.get(OPEN_LIBRARY_SEARCH_API, timeout=10.0,
                      params={"q": "dune frank herbert",
                              "fields": "key", "limit": 1},
                      headers={"User-Agent": "Litheca/1.0 (https://litheca.com; "
                                             "hello@litheca.com)"})
        r.raise_for_status()
        return bool(r.json().get("docs"))
    except Exception as exc:                              # noqa: BLE001
        log.warning("catalogue canary failed: %s", str(exc)[:120])
        return False


def _resolve_or_404(title: str, author: str) -> tuple[str, str]:
    """A real book's catalogue title and author, or a refusal that is true.

    THIS IS THE LINE THE READER'S TEXT DOES NOT CROSS. What comes back is
    what a catalogue holds; what went in is not kept anywhere. A book nobody
    can verify is refused here, at intake — not queued for the owner to
    discover it was unverifiable, which would put the burden of the check on
    the one person the queue exists to save time for.

    404 means we looked. 503 means we could not. See `_catalogue_reachable`.
    """
    from book_data import resolve_book                   # noqa: E402
    try:
        record = resolve_book(title, author)
    except Exception as exc:                             # noqa: BLE001
        log.warning("resolve_book failed for %r: %s", title[:60], str(exc)[:120])
        record = None
    if record is None or not record.found:
        if not _catalogue_reachable():
            raise HTTPException(
                status_code=503,
                detail="could not reach the book catalogue just now, so this "
                       "was not checked — please try again in a minute")
        raise HTTPException(
            status_code=404,
            detail="no book by that title and author could be found — "
                   "check the spelling, or add the author")
    return record.title, (record.author or "")


def _entry_id(fields: dict) -> str:
    """A content hash, so the same report filed twice is ONE queue entry.

    Two readers hitting the same wrong year must not become two things to
    review — and the fact that they both did is worth more than either
    report alone, which is what `votes` records. Derived from the STORED
    fields (already resolved and sanitised), never from raw input, so two
    different queries that resolve to the same book converge correctly.
    """
    parts = "|".join(str(fields.get(k, "")) for k in
                     ("reason", "work_key", "title", "author", "year"))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


router = APIRouter(prefix="/akinator", tags=["akinator"])


@router.post("/suggest")
def suggest(body: SuggestRequest, request: Request):
    """Queue one reader report, after verifying everything in it.

    UNLIKE `/akinator/submit`, THIS RETURNS REAL ERRORS. That endpoint
    answers 200 whatever happens, because a submission is a courtesy whose
    result changes nothing for the player and an error would be noise. This
    is a form the reader filled in and is waiting on: "we could not find
    that book" is the single most useful thing to tell them, and swallowing
    it would leave them believing a typo had been recorded.
    """
    fields: dict = {"reason": body.reason}

    # ── phase 1: everything that can be checked for free ─────────────────
    #
    # THE LIMITER DELIBERATELY COMES AFTER THIS, and the smoke test is why.
    # Charging a reader for a malformed request means a handful of typos
    # locks out the one person who cared enough to report something — and a
    # request that never reaches an external API costs nothing to refuse.
    # So the cheap refusals are free and the quota buys what is actually
    # expensive: a Google Books / Open Library resolve, and a queue slot.
    if body.reason == "missing":
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="a title is required")
    else:
        if not _WORK_KEY.match(body.work_key):
            raise HTTPException(status_code=400, detail="malformed work key")
        if body.reason == "wrong_year" and body.year is None:
            raise HTTPException(status_code=400, detail="a year is required")
        if body.reason == "unreadable" and not body.title.strip():
            raise HTTPException(status_code=400,
                                detail="a readable title is required")

    from tools.quiz_core import _rate_limit              # noqa: E402

    try:
        _rate_limit("akinator_suggest", DAILY_SUGGESTIONS,
                    _client_id(request), namespace="akin")
    except HTTPException:
        raise HTTPException(status_code=429,
                            detail=f"that's {DAILY_SUGGESTIONS} suggestions "
                                   "today — thank you, come back tomorrow")

    # ── phase 2: the checks that cost something ──────────────────────────
    if body.reason == "missing":
        title, author = _resolve_or_404(body.title, body.author)
        # It resolved to a book the game already ships. That is not a
        # rejection of the reader — it is the answer to their question, and
        # a far better one than a queue entry: the search box matches raw
        # titles, so "Crime & Punishment" finds nothing while
        # "Crime and Punishment" is row 40. Mirrors /akinator/admin/book's
        # own 409 rather than inventing a second behaviour for the case.
        from features import normalize                   # noqa: E402
        already = _shipped_titles()

        if already and normalize(title) in already:
            raise HTTPException(
                status_code=409,
                detail=f"good news — the game does know it, as “{title}”")
        fields["title"] = title
        fields["author"] = author
    else:
        index = _shipped_index()
        if not index:
            # Fail CLOSED, unlike akinator_learn's consistency check, which
            # skips itself when artifacts are unreachable. That guard failing
            # open costs a slightly worse count; this one failing open costs
            # a queue entry pointing at a row that may not exist, and the
            # whole value of the queue is that everything in it is real.
            raise HTTPException(status_code=503,
                               detail="cannot check the book list right now "
                                      "— please try again later")
        if body.work_key not in index:
            raise HTTPException(status_code=404,
                                detail="that book is not in the shipped game")
        fields["work_key"] = body.work_key

        if body.reason == "wrong_year":
            fields["year"] = body.year
        else:                                            # unreadable
            title, author = _resolve_or_404(body.title, body.author)
            fields["title"] = title
            fields["author"] = author

    pending = cache.set_members(PENDING_SET)
    if pending is not None and len(pending) >= MAX_PENDING:
        log.warning("suggestion queue full (%d) — refusing intake", len(pending))
        raise HTTPException(status_code=503,
                            detail="the suggestion queue is full — it is "
                                   "reviewed by hand, please try again later")

    entry_id = _entry_id(fields)
    key = SUGGEST_PREFIX + entry_id
    flat: list = []
    for name, value in fields.items():
        flat += [name, str(value)]

    # `first_seen` uses HSETNX so a second report of the same thing bumps the
    # vote without rewriting when it was first noticed — the two dates
    # together are what tell the owner whether this is one persistent reader
    # or a steady trickle.
    stored = cache.pipeline([
        ["HSET", key, *flat, "last_seen", str(int(time.time()))],
        ["HSETNX", key, "first_seen", str(int(time.time()))],
        ["HINCRBY", key, "votes", 1],
        ["EXPIRE", key, SUGGEST_TTL],
        ["SADD", PENDING_SET, entry_id],
        ["EXPIRE", PENDING_SET, SUGGEST_TTL],
    ])
    if stored is None:
        log.warning("suggestion not stored: Redis unavailable")
        raise HTTPException(status_code=503,
                            detail="could not record that right now — "
                                   "please try again later")

    log.info("queued suggestion %s (%s)", entry_id, body.reason)
    return {"ok": True, "id": entry_id, "reason": body.reason}


# ── the review queue ─────────────────────────────────────────────────────

# `_require_admin` as a ROUTER dependency, not a line inside each handler.
# FastAPI resolves dependencies BEFORE it validates the body, so a caller
# without the secret gets 403 rather than a 422 listing every field the
# endpoint expects — which is exactly the schema disclosure that was found
# and fixed on the live deploy in 24ff1c0. It also cannot be forgotten on
# the next endpoint added here.
admin_router = APIRouter(prefix="/akinator/admin/suggestions",
                         tags=["akinator"],
                         dependencies=[Depends(_require_admin)])


def _read_queue() -> tuple[list[dict], list[str]]:
    """(entries, stale ids). One round trip, not one request per entry.

    An id can outlive its hash: every suggestion refreshes the SET's TTL,
    while an individual entry's TTL only moves when that same suggestion is
    filed again. So the set slowly accumulates ids whose hash has expired.
    They read back as empty hashes and are swept here.
    """
    ids = cache.set_members(PENDING_SET)
    if ids is None:
        raise HTTPException(status_code=503,
                            detail="the queue is unreadable right now — "
                                   "this is not the same as it being empty")
    ids = sorted(i for i in ids if _ID.match(i or ""))
    if not ids:
        return [], []

    replies = cache.pipeline([["HGETALL", SUGGEST_PREFIX + i] for i in ids])
    if replies is None:
        raise HTTPException(status_code=503, detail="the queue is unreadable right now")

    entries, stale = [], []
    for entry_id, reply in zip(ids, replies):
        flat = reply.get("result") if isinstance(reply, dict) else None
        if not flat:
            stale.append(entry_id)
            continue
        # Upstash answers HGETALL with the raw RESP [k1,v1,k2,v2,…] array,
        # the same shape cache.hgetall folds; folded here because the
        # pipeline endpoint hands back the array unfolded.
        entry = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        entry["id"] = entry_id
        for numeric in ("votes", "year", "first_seen", "last_seen"):
            if numeric in entry:
                try:
                    entry[numeric] = int(entry[numeric])
                except (TypeError, ValueError):
                    entry.pop(numeric)
        entries.append(entry)

    entries.sort(key=lambda e: (-(e.get("votes") or 1), e.get("first_seen") or 0))
    return entries, stale


@admin_router.post("")
def list_suggestions():
    """The pending queue, richest first.

    POST rather than GET only because the Worker in front of this is a
    POST-only relay and giving it a second verb buys nothing. Nothing here
    mutates except the sweep of ids whose entry has already expired.
    """
    entries, stale = _read_queue()
    if stale:
        cache.pipeline([["SREM", PENDING_SET, i] for i in stale])
        log.info("swept %d expired suggestion ids", len(stale))
    return {"ok": True, "pending": entries, "count": len(entries)}


class ResolveRequest(BaseModel):
    id: str = Field(..., max_length=32)
    action: Literal["book", "correction", "display", "exclude", "reject"]


# Which resolutions a reason may be closed with. `unreadable` has two
# because renaming and excluding are the same judgement made two ways; the
# owner is the one who can tell which row of a duplicate pair is the better
# one, and that is a look at the list, not a rule.
_ALLOWED = {
    "missing": {"book", "reject"},
    "wrong_year": {"correction", "reject"},
    "unreadable": {"display", "exclude", "reject"},
}


@admin_router.post("/resolve")
def resolve(body: ResolveRequest):
    """Approve one suggestion into a real admin action, or drop it.

    THE ORDER HERE IS THE WHOLE DESIGN. The underlying commit happens
    FIRST; the queue entry is deleted only after it succeeded. A failed
    commit therefore leaves the suggestion exactly where it was, to be
    tried again — the reverse order would silently swallow a report the
    moment GitHub had a bad minute.
    """
    if not _ID.match(body.id):
        raise HTTPException(status_code=400, detail="malformed id")

    entry = cache.hgetall(SUGGEST_PREFIX + body.id)
    if entry is None:
        raise HTTPException(status_code=503, detail="queue unreadable right now")
    if not entry:
        raise HTTPException(status_code=404,
                            detail="no such suggestion — it may have expired "
                                   "or already been resolved")

    reason = entry.get("reason", "")
    if body.action not in _ALLOWED.get(reason, {"reject"}):
        raise HTTPException(
            status_code=400,
            detail=f"a '{reason}' suggestion cannot be resolved as "
                   f"'{body.action}' — allowed: "
                   f"{sorted(_ALLOWED.get(reason, {'reject'}))}")

    work_key = entry.get("work_key", "")
    title = entry.get("title", "")
    author = entry.get("author", "")
    result: dict = {}

    if body.action == "book":
        if not title:
            raise HTTPException(status_code=400, detail="entry has no title")
        # Straight through /akinator/admin/book, which re-resolves the title
        # itself and reads the year from Open Library's work-level field.
        # Re-resolving is a second API call and worth it: it is the code path
        # the owner's own "Add a book" tab uses, so an approved suggestion and
        # a hand-typed book cannot land as different rows.
        result = admin_book(BookRequest(title=title, author=author,
                                        summary="", themes=[]))
    elif body.action == "correction":
        year = entry.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="entry has no year")
        result = admin_correction(CorrectionRequest(
            work_key=work_key, field="first_publish_year", value=year,
            note="reader suggestion"))
    elif body.action == "display":
        if not title:
            raise HTTPException(status_code=400, detail="entry has no title")
        result = admin_display(DisplayRequest(work_key=work_key, title=title,
                                              author=author or None))
    elif body.action == "exclude":
        result = admin_exclude(ExcludeRequest(work_key=work_key, excluded=True,
                                              reason="reader suggestion"))

    removed = cache.pipeline([
        ["SREM", PENDING_SET, body.id],
        ["DEL", SUGGEST_PREFIX + body.id],
    ]) is not None
    if not removed:
        # Honest rather than tidy: the commit landed, so saying "done" is
        # true, but the entry will come back on the next listing and the
        # owner needs to know why rather than thinking they misclicked.
        log.warning("resolved %s but could not clear it from Redis", body.id)

    log.info("resolved suggestion %s as %s", body.id, body.action)
    return {"ok": True, "action": body.action, "removed": removed,
            "result": result or None}
