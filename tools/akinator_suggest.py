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
    ARTIFACT_DIR,
    BookRequest,
    CorrectionRequest,
    DisplayRequest,
    ExcludeRequest,
    _commit_files,
    _dump,
    _get_json,
    _require_admin,
    book as admin_book,
    correction as admin_correction,
    display as admin_display,
    exclude as admin_exclude,
)

log = logging.getLogger("bookhub-api.akinator_suggest")

SUGGEST_PREFIX = "akin:sg:"
PENDING_SET = "akin:sg:pending"

# Tick counts, kept SEPARATELY from the queue and deliberately outliving it.
#
# Aggregating the pending entries would have been free, and wrong: approving
# or rejecting deletes the entry, so the moment the owner works through the
# backlog the signal disappears — and the backlog is the one population that
# says nothing about what readers ask for, only about what has not been
# handled yet. The question is about the FLOW of reports over time, so the
# counter is incremented at intake and never touched again.
#
# `__reports` is the denominator: how many themed reports there have been.
# A double underscore cannot collide with a feature id, which always
# contains a colon.
THEME_STATS = "akin:sg:themestats"
REPORTS_FIELD = "__reports"

# Dimensions readers asked for that the tick-list does not offer.
# {canonical Open Library subject: "asks:ol_work_count"}
THEME_ASKS = "akin:sg:themeasks"

# An Open Library subject with three works behind it is a typo somebody
# else made, not a dimension. Fifty is low enough to admit a genuinely
# niche subject (OL puts "dystopia" at 144) and high enough that noise
# does not reach the owner. It is a floor on REALITY, not on demand — how
# many readers want it is counted separately.
MIN_SUBJECT_WORKS = 50

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
    # Feature ids the reader ticked ("theme:magic", "genre:fantasy"), NOT
    # words they typed. A closed vocabulary is the only way to accept "what
    # is this book about?" without accepting free text, and the list is
    # validated against features.SUBJECT_RULES server-side — the client's
    # copy decides what is OFFERED, never what is accepted.
    themes: list[str] = Field(default_factory=list, max_length=12)
    # A dimension the tick-list does not offer. Typed, therefore a QUERY:
    # resolved against Open Library's subject index and stored as the
    # canonical subject name it matched, never as the words that were typed.
    # See `_resolve_subject`.
    new_theme: str = Field(default="", max_length=60)


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


def _resolve_or_404(title: str, author: str) -> tuple[str, str, str]:
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
    return record.title, (record.author or ""), (record.open_library_work_key or "")


_FEATURE_ID = re.compile(r"^[a-z]+:[a-z0-9_]{1,40}$")


def _subject_words(feature_ids: list[str]) -> list[str]:
    """Feature ids a reader ticked -> subject strings the pipeline reads.

    THE VOCABULARY IS DERIVED, NOT DUPLICATED. `features.SUBJECT_RULES` is
    the single place that decides which subject words imply which feature,
    so this reads the first keyword of the matching rule rather than keeping
    a second table that would silently drift from it. An id with no rule is
    dropped, which is what makes the whole field safe: nothing a reader
    sends can become a subject string that `extract()` does not already
    recognise, so there is no path from this input to arbitrary text landing
    in `books.json`.

    Trailing `*` is a glob in the rule's matcher ("magic*" catches
    "magical"); the literal stem is what belongs in a subject list.
    """
    try:
        from features import SUBJECT_RULES                # noqa: E402
    except Exception as exc:                              # noqa: BLE001
        log.warning("SUBJECT_RULES unavailable: %s", str(exc)[:90])
        return []
    rules = {fid: kws for fid, _text, kws in SUBJECT_RULES}
    out = []
    for fid in feature_ids:
        kws = rules.get(fid)
        if kws:
            word = kws[0].replace("*", "").strip()
            if word and word not in out:
                out.append(word)
    return out


def _clean_themes(raw: list[str]) -> list[str]:
    """The subset of what was ticked that names a real feature rule."""
    try:
        from features import SUBJECT_RULES                # noqa: E402
        known = {fid for fid, _t, _k in SUBJECT_RULES}
    except Exception:                                     # noqa: BLE001
        return []
    seen, out = set(), []
    for fid in raw:
        fid = (fid or "").strip()
        if _FEATURE_ID.match(fid) and fid in known and fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out[:12]


def _resolve_subject(query: str) -> tuple[str, int]:
    """A typed theme -> a real Open Library subject, or a refusal.

    THE SAME MOVE AS `_resolve_or_404`, one level down. A reader may name a
    dimension the tick-list does not offer, and that has to be typed — but
    what is typed is a QUERY. Open Library's subject index answers with a
    canonical name and a work count, and it is the canonical name that is
    stored. So the promise holds unchanged: nothing a stranger wrote reaches
    Redis, the admin page, or a commit.

    A nonsense string is not a subject: OL answers 200 with `work_count: 0`
    and a name echoing the slug, which is why the count is checked rather
    than the status. Verified live against five queries including one made
    up on the spot.

    WHAT THIS COUNT IS NOT. It is Open Library's whole catalogue, not our
    5,000. Whether a subject clears the game's 5% question floor — 250 of
    our books — cannot be known from here at all: the corpus is gitignored
    and Render has never held it. The admin page says so rather than letting
    a big number imply an answer it does not have.
    """
    import httpx                                          # noqa: E402
    slug = re.sub(r"[^a-z0-9]+", "_", query.strip().lower()).strip("_")
    if not slug:
        raise HTTPException(status_code=400, detail="that is not a theme")
    try:
        r = httpx.get(f"https://openlibrary.org/subjects/{slug}.json",
                      params={"limit": 0}, timeout=15.0,
                      headers={"User-Agent": "Litheca/1.0 (https://litheca.com; "
                                             "hello@litheca.com)"})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:                              # noqa: BLE001
        log.warning("subject lookup failed for %r: %s", query[:40], str(exc)[:110])
        raise HTTPException(status_code=503,
                            detail="could not check that theme just now — "
                                   "please try again in a minute")
    works = data.get("work_count") or 0
    name = (data.get("name") or "").strip()
    if not isinstance(works, int) or works < MIN_SUBJECT_WORKS or not name:
        raise HTTPException(
            status_code=404,
            detail="that is not a subject books are catalogued under — try "
                   "the wording a library would use")
    return name, works


def _missing_book_hints(title: str, author: str, work_key: str = "") -> dict:
    """Year and Open Library work key for a book being proposed as new.

    WHY THE CARD NEEDS THESE, measured. Approving a `missing` suggestion is
    a decision about a book the reviewer can see two facts about — a title
    and an author — and that is not enough to judge either question they
    actually have: is this the same book we already hold under another name,
    and will the row be worth anything.

    The year is the answer to the second: six era questions read it, and
    `/akinator/admin/book` looks it up at approval time anyway, so gathering
    it here costs one call on a request that is already talking to Open
    Library and turns an invisible fact into a visible one. Work-level, via
    the same `_first_publish_year` the add-book endpoint uses — never
    `record.published_year`, which is the edition date that put 2008 on a
    1995 novel.

    The work key is a PARTIAL answer to the first, and its limits are
    measured rather than assumed: over eight books that are genuinely in the
    game, resolving them the way a reader would typed them produced a work
    key every time but matched the shipped row only 4 times of 8 — Open
    Library holds several work entities per book (Crime and Punishment came
    back as OL21062236W, which is not the row we ship). So a match here is
    conclusive and a miss means nothing at all, and the review page says so
    rather than presenting absence as evidence.
    """
    out: dict = {}
    try:
        from tools.akinator_admin import _first_publish_year   # noqa: E402
        year = _first_publish_year(title, author)
        if year:
            out["year_hint"] = year
    except Exception as exc:                                   # noqa: BLE001
        log.warning("year hint failed for %r: %s", title[:60], str(exc)[:90])
    if work_key:
        key = work_key if work_key.startswith("/works/") else f"/works/{work_key}"
        if _WORK_KEY.match(key):
            out["ol_key"] = key
    return out


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
    ticked_now: list = []          # only a "missing" report carries themes
    asked_subject, asked_works = "", 0
    if body.reason == "missing":
        title, author, ol_key = _resolve_or_404(body.title, body.author)
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
        # Two hints for the reviewer, gathered here because the resolve has
        # already happened and the review page must not have to make network
        # calls of its own.
        #
        # DELIBERATELY OUTSIDE `_entry_id`'s key tuple. Both are looked up
        # live, so either can come back None on one request and populated on
        # the next — folding them into the content hash would split one book
        # into two queue entries the moment Open Library hiccupped, which is
        # exactly the deduplication this feature relies on.
        fields.update(_missing_book_hints(title, author, ol_key))
        ticked_now = _clean_themes(body.themes)
        if ticked_now:
            fields["themes"] = ",".join(ticked_now)
        if body.new_theme.strip():
            asked_subject, asked_works = _resolve_subject(body.new_theme)
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
            title, author, _ = _resolve_or_404(body.title, body.author)
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

    # Themes are UNIONED across reports of the same book, not overwritten.
    # `_entry_id` deliberately ignores them (two readers ticking different
    # boxes must not become two entries for one book), and the consequence
    # of that is this: the second report would otherwise erase the first
    # reader's picks. Union is also the honest reading — each reader is
    # asserting something true about the book, not replacing an opinion.
    if fields.get("themes"):
        existing = cache.hgetall(key) or {}
        merged = _clean_themes(
            (existing.get("themes", "").split(",") if existing.get("themes") else [])
            + fields["themes"].split(","))
        if merged:
            fields["themes"] = ",".join(merged)

    flat: list = []
    for name, value in fields.items():
        flat += [name, str(value)]

    # THIS reader's ticks, not the merged set. `fields["themes"]` has just
    # been unioned with everything earlier reporters chose, so counting from
    # it would re-count their picks on every repeat and inflate whichever
    # theme happened to be ticked first. Counted per REPORT rather than per
    # book, because two readers asking for the same kind of book is exactly
    # the agreement this is meant to surface.
    stats_cmds: list = []
    if ticked_now:
        for fid in ticked_now:
            stats_cmds.append(["HINCRBY", THEME_STATS, fid, 1])
        stats_cmds.append(["HINCRBY", THEME_STATS, REPORTS_FIELD, 1])
        stats_cmds.append(["EXPIRE", THEME_STATS, SUGGEST_TTL])
    if asked_subject:
        # HSET rather than HINCRBY: the value carries the OL work count too,
        # and the ask count is read-modify-written just above the pipeline so
        # both numbers stay in one field. Two readers naming the same subject
        # converge on the same field, which is the whole point of storing the
        # CANONICAL name rather than what either of them typed.
        prior_asks = 0
        existing = cache.hgetall(THEME_ASKS) or {}
        try:
            prior_asks = int(str(existing.get(asked_subject, "0:0")).split(":")[0])
        except (TypeError, ValueError):
            prior_asks = 0
        stats_cmds.append(["HSET", THEME_ASKS, asked_subject,
                           f"{prior_asks + 1}:{asked_works}"])
        stats_cmds.append(["EXPIRE", THEME_ASKS, SUGGEST_TTL])

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
    ] + stats_cmds)
    if stored is None:
        log.warning("suggestion not stored: Redis unavailable")
        raise HTTPException(status_code=503,
                            detail="could not record that right now — "
                                   "please try again later")

    log.info("queued suggestion %s (%s)", entry_id, body.reason)
    return {"ok": True, "id": entry_id, "reason": body.reason}


# ── what readers keep asking for ─────────────────────────────────────────

_CORPUS_SHARE: dict = {}


def _corpus_share() -> dict:
    """{feature_id: share of shipped books that ANSWER YES to it}.

    THE BASELINE IS THE WHOLE POINT. A raw tick count answers "what are books
    usually about?" and will rank `form:fiction` first forever — true, and
    useless. What the owner asked for is which dimension readers ask for that
    the game does not already have, and that is a COMPARISON: 60% of reports
    ticking "web novel" means nothing until you know the corpus is 2%.

    Read straight out of the shipped matrix, two bits per cell, counting only
    state 1 (present). `unknown` is not absence and is excluded from the
    numerator — the same distinction the engine draws everywhere, and getting
    it wrong here would understate every sparse feature and so overstate how
    unusual a reader asking for one is.
    """
    if _CORPUS_SHARE:
        return _CORPUS_SHARE
    from tools.akinator_learn import _artifacts            # noqa: E402
    art = _artifacts()
    if not art:
        return {}
    # A half-filled artifact set must leave the baseline EMPTY rather than
    # half-computed: a share derived from a truncated matrix is a wrong
    # number, and this whole panel exists to compare against it. Empty means
    # "no baseline", which the page prints as such. Seen for real — the
    # local network drops Open Library mid-fetch often enough that this ran
    # against a partial `meta` once.
    try:
        meta, qids, matrix = art["meta"], art["qids"], art["matrix"]
        nq, bpr, nbooks = meta["questions"], meta["bytes_per_row"], meta["books"]
        if not (nq and bpr and nbooks) or len(matrix) != nbooks * bpr:
            log.warning("artifacts incomplete; no corpus baseline")
            return {}
        present = [0] * nq
        for i in range(nbooks):
            off = i * bpr
            for k in range(nq):
                if ((matrix[off + (k >> 2)] >> ((k & 3) * 2)) & 3) == 1:
                    present[k] += 1
        _CORPUS_SHARE.update({qids[k]: present[k] / nbooks for k in range(nq)})
    except Exception as exc:                              # noqa: BLE001
        log.warning("corpus baseline failed: %s", str(exc)[:110])
        _CORPUS_SHARE.clear()
        return {}
    return _CORPUS_SHARE


def _wilson_low(hits: int, n: int) -> float:
    """Wilson 95% lower bound on a proportion.

    Small-sample overconfidence is failure shape #5 in this project's tally,
    and it has already reversed two results that were quoted early. A ratio
    computed from three ticks would read "13x over-represented" and mean
    nothing. This is the cheapest honest guard: compare the LOWER BOUND of
    what readers asked for against the corpus share, so a theme is only ever
    called over-represented when the sample can carry the claim.
    """
    if n <= 0:
        return 0.0
    z = 1.96
    p = hits / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / d)


# THREE GUARDS, and the first two exist because the Wilson bound alone was
# measured to be not enough. A theme ticked by 3 of 3 reporters has a 95%
# lower bound of 0.207 — genuinely wide — and that still clears a corpus
# share of 0.008, so `form:webnovel` printed "128x over-represented" off
# three reports. The bound protects against a COMMON baseline; nothing but a
# sample-size floor protects against a rare one.
#
# 20 and 5 are a judgement, not a derivation: at 20 reports the bound on a
# quarter-share is about 0.11, still far above a 1% baseline, and 20 reports
# is an amount of evidence a person would not feel silly acting on. Failure
# shape #5 has cost this project two reversed results already.
MIN_REPORTS_FOR_CLAIM = 20
MIN_TICKS_FOR_CLAIM = 5

# And a minimum effect. `form:fiction` at 85% of reports against a 55%
# corpus is statistically over-represented and tells the owner nothing —
# it is what books are. Only a doubling is a dimension being ASKED FOR
# rather than merely present.
MIN_RATIO_FOR_CLAIM = 2.0


def _theme_stats() -> dict:
    """Tick counts against the corpus baseline, ranked by what stands out."""
    raw = cache.hgetall(THEME_STATS)
    if raw is None:
        return {"available": False, "reason": "counters unreadable"}
    if not raw:
        return {"available": True, "reports": 0, "themes": []}

    try:
        reports = int(raw.get(REPORTS_FIELD, 0))
    except (TypeError, ValueError):
        reports = 0

    share = _corpus_share()
    rows = []
    for fid, count in raw.items():
        if fid == REPORTS_FIELD:
            continue
        try:
            ticks = int(count)
        except (TypeError, ValueError):
            continue
        if ticks <= 0:
            continue
        corpus = share.get(fid)
        reader = ticks / reports if reports else 0.0
        low = _wilson_low(ticks, reports) if reports else 0.0
        ratio = (reader / corpus) if corpus else None
        # Only a claim the sample can carry AND that means something. None
        # means "not enough to say", which the page prints rather than hides.
        over = None
        if (corpus and ratio
                and reports >= MIN_REPORTS_FOR_CLAIM
                and ticks >= MIN_TICKS_FOR_CLAIM
                and ratio >= MIN_RATIO_FOR_CLAIM
                and low > corpus):
            over = round(ratio, 1)
        rows.append({
            "id": fid,
            "subject": (_subject_words([fid]) or [""])[0],
            "ticks": ticks,
            "reader_share": round(reader, 4),
            "corpus_share": round(corpus, 4) if corpus is not None else None,
            "over": over,
        })
    # Standouts first, then the merely frequent. A theme with no corpus
    # figure (a retired question) sorts last rather than being dropped —
    # readers ticking it is still information.
    rows.sort(key=lambda r: (-(r["over"] or 0), -r["ticks"]))
    return {"available": True, "reports": reports, "themes": rows}


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
        if entry.get("themes"):
            # A list for the page, plus the subject strings each one becomes,
            # so the reviewer sees what approving would actually write rather
            # than an opaque feature id.
            ids = entry["themes"].split(",")
            entry["themes"] = ids
            entry["theme_subjects"] = _subject_words(ids)
        for numeric in ("votes", "year", "year_hint", "first_seen", "last_seen"):
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
    # Counted over every themed report ever filed, not over `entries` — see
    # THEME_STATS. Resolving the queue must not erase what it taught us.
    return {"ok": True, "pending": entries, "count": len(entries),
            "theme_stats": _theme_stats(), "theme_asks": _theme_asks()}


def _theme_asks() -> list:
    """Dimensions readers named that the tick-list does not offer."""
    raw = cache.hgetall(THEME_ASKS)
    if not raw:
        return []
    out = []
    for subject, packed in raw.items():
        asks, _, works = str(packed).partition(":")
        try:
            out.append({"subject": subject, "asks": int(asks),
                        "ol_works": int(works or 0)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: (-r["asks"], -r["ol_works"]))
    return out


THEME_REQUESTS_PATH = f"{ARTIFACT_DIR}/theme_requests.json"


class ThemeDecision(BaseModel):
    subject: str = Field(..., max_length=80)
    accept: bool


@admin_router.post("/theme")
def decide_theme(body: ThemeDecision):
    """Accept a requested dimension into the permanent record, or drop it.

    WHAT ACCEPTING DOES, STATED HONESTLY, because the temptation is to imply
    more. It does NOT create a question. A question needs a column in
    `matrix.bin`, which needs a full `build_matrix.py` run, which needs the
    corpus — gitignored, and never on Render. And it would then have to clear
    `MIN_FREQ`: **5% of the corpus, 250 books**, or the build retires it
    again the same day.

    So what this writes is the thing that IS durable and IS useful: a
    reviewed list, committed to the bookhub repo, of subjects readers asked
    for and the evidence behind each. `features.SUBJECT_RULES` is a
    hand-maintained table, and whoever next edits it should be editing it
    against this file rather than against a hunch.
    """
    subject = body.subject.strip()
    if not subject or len(subject) > 80:
        raise HTTPException(status_code=400, detail="malformed subject")

    asks = {r["subject"]: r for r in _theme_asks()}
    row = asks.get(subject)
    if row is None:
        raise HTTPException(status_code=404, detail="no such requested theme")

    if body.accept:
        current, _ = _get_json(THEME_REQUESTS_PATH, [])
        if not isinstance(current, list):
            current = []
        current = [c for c in current
                   if isinstance(c, dict) and c.get("subject") != subject]
        current.append({"subject": subject, "readers_asked": row["asks"],
                        "open_library_works": row["ol_works"],
                        "accepted": time.strftime("%Y-%m-%d")})
        current.sort(key=lambda c: -(c.get("readers_asked") or 0))
        wrote = _commit_files(
            {THEME_REQUESTS_PATH: _dump(current)},
            f"mind reader admin: readers want a '{subject}' question "
            f"({row['asks']} asked)")
        if not wrote:
            raise HTTPException(status_code=502, detail="commit failed")

    cache.pipeline([["HDEL", THEME_ASKS, subject]])
    return {
        "ok": True, "accepted": body.accept, "subject": subject,
        "effect": ("recorded for the next rebuild — this does NOT create a "
                   "question today" if body.accept else "dropped"),
        "note": ("a new question needs a full build_matrix.py run with the "
                 "corpus, and must then cover 5% of it (250 books) to survive "
                 "the frequency floor" if body.accept else ""),
    }


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
        # Feature ids -> the subject strings `features.extract()` reads. A
        # row built from the catalogue alone answers whatever Open Library
        # happened to record; these are the questions a reader knew the
        # answer to and the catalogue did not.
        subjects = _subject_words(
            entry["themes"].split(",") if entry.get("themes") else [])
        result = admin_book(BookRequest(title=title, author=author,
                                        summary="", themes=subjects))
        result = {**(result or {}), "subjects_added": subjects}
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
