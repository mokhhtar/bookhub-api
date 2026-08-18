"""
tools/akinator_admin.py — the mind-reader admin page's write API.

Behind Cloudflare Access at the edge (`worker/akinator-admin/`, a new
Worker on an Access-protected custom domain — the same shape as
`worker/seo-status/`), and behind its OWN shared secret on this hop
(`AKINATOR_ADMIN_SECRET`, checked exactly like `X-Sync-Secret` gates
`/akinator/sync` and `/akinator/drain`). Access authenticates the
browser; it says nothing about the Worker-to-Render call, so that hop
needs its own credential regardless of who is sitting at the browser.

FOUR ACTIONS, FOUR DIFFERENT LANDING TIMES — stated in every response,
never left for the admin to infer:

    exclude     instant     excluded.json; the client reads it at load
                            and zeroes that book's prior. Never touches
                            matrix.bin, so no row-count risk.
    book        instant     `akinator_sync.append_book_row()` — the same
                            append-one-row mechanism `/akinator/sync`
                            already uses for newly published pages.
    question    instant,    patches the live questions.json's wording
                and stays   directly, AND writes question_overrides.json
                            so `build_matrix.py`'s `question_text()`
                            keeps using it after the next full rebuild.
    correction  next full   same as every hand-written entry in
                rebuild     corrections.py today — a fact that changes a
                only        matrix bit (an era question's ladder, say)
                            needs that bit recomputed, and only a local
                            `build_matrix.py` run does that.

NOTHING HERE IS AUTOMATIC. Every one of these four endpoints is a direct
admin action taken by a human who is already Access-authenticated. There
is no reader-facing suggestion capture anywhere in this codebase yet —
that is planned, not built (see the architecture note in this session's
diagram) — so there is deliberately no "approve a pending suggestion"
endpoint here to approve nothing.

A NEW BOOK IS NEVER INVENTED. `POST /book` verifies the admin's title and
author against `book_data.resolve_book()` (Google Books -> Open Library,
the same grounding every other tool in this codebase uses) before it will
add a row — the admin can misremember a title, but the row that lands is
always a real, resolvable book.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "akinator"))

from fastapi import APIRouter, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, Field                  # noqa: E402

from tools.akinator_sync import (                       # noqa: E402
    ARTIFACT_DIR,
    _commit_files,
    _get_file,
    append_book_row,
)

log = logging.getLogger("bookhub-api.akinator_admin")

router = APIRouter(prefix="/akinator/admin", tags=["akinator"])

ADMIN_SECRET = os.environ.get("AKINATOR_ADMIN_SECRET", "")

EXCLUDED_PATH = f"{ARTIFACT_DIR}/excluded.json"
ADMIN_CORRECTIONS_PATH = f"{ARTIFACT_DIR}/admin_corrections.json"
QUESTION_OVERRIDES_PATH = f"{ARTIFACT_DIR}/question_overrides.json"
QUESTIONS_PATH = f"{ARTIFACT_DIR}/questions.json"

# Broader than akinator_learn.py's _WORK_KEY (works|site only) — the admin
# page routinely acts on /fandom/ rows too.
#
# THE SLUG LENGTH IS MEASURED, NOT GUESSED. A 64-character cap was the first
# thing written here and it rejected a book that actually ships: the longest
# /site/ key in books.json is 82 characters ("resumen-de-mas-agudo-mas-
# rapido-y-mejor-…", a Spanish summary page whose title became its slug).
# A validator that refuses to act on a row the game already serves is worse
# than no validator — the whole point of this page is fixing what playing
# the game turns up. 200 clears every current key with room for a longer
# one, and still bounds the field.
_WORK_KEY = re.compile(r"^/(?:works|site|fandom)/[A-Za-z0-9_-]{1,200}$")
_QUESTION_ID = re.compile(r"^[a-z]+:[a-z0-9_]{1,40}$")

# Fields a correction is allowed to touch. Deliberately small — mirrors
# corrections.py's own bar ("the field must be one the game actually asks
# about"). Extend this set only when a new field genuinely needs it, the
# same discipline that file's docstring asks of a human editor.
_CORRECTABLE_FIELDS = {"first_publish_year"}


def _require_admin(x_admin_secret: str) -> None:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")


def _get_json(path: str, default):
    raw, sha = _get_file(path)
    if not raw:
        return default, sha
    try:
        return json.loads(raw), sha
    except json.JSONDecodeError:
        return default, sha


def _dump(data) -> bytes:
    """Readable JSON, for the three small files only the admin ever reads."""
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def _dump_shipped(data) -> bytes:
    """Compact JSON, for a file every player downloads.

    `questions.json` is the one file this module rewrites that is on the
    play path, and the build writes it with these exact separators. Saving
    it pretty-printed would roughly double it (3.7 KB -> ~8 KB) on every
    single page load, for whitespace nobody reads — and the next full
    rebuild would silently compact it again, making every admin reword show
    up as a whole-file diff. Same writer, same format.
    """
    return json.dumps(data, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


# ── POST /akinator/admin/exclude ────────────────────────────────────────

class ExcludeRequest(BaseModel):
    work_key: str = Field(..., max_length=220)
    excluded: bool
    reason: str = Field(default="", max_length=200)


@router.post("/exclude")
def exclude(body: ExcludeRequest, x_admin_secret: str = Header(default="")):
    _require_admin(x_admin_secret)
    if not _WORK_KEY.match(body.work_key):
        raise HTTPException(status_code=400, detail="malformed work key")

    current, _ = _get_json(EXCLUDED_PATH, [])
    current = set(k for k in current if isinstance(k, str))
    was_excluded = body.work_key in current
    if body.excluded:
        current.add(body.work_key)
    else:
        current.discard(body.work_key)

    if was_excluded == body.excluded:
        return {"ok": True, "changed": False, "effect": "instant"}

    verb = "excluded" if body.excluded else "restored"
    reason_note = f" ({body.reason})" if body.reason else ""
    wrote = _commit_files(
        {EXCLUDED_PATH: _dump(sorted(current))},
        f"mind reader admin: {verb} {body.work_key}{reason_note}")
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "changed": True, "effect": "instant"}


# ── POST /akinator/admin/correction ─────────────────────────────────────

class CorrectionRequest(BaseModel):
    work_key: str = Field(..., max_length=220)
    field: str = Field(..., max_length=40)
    value: int = Field(...)  # only first_publish_year is correctable today
    note: str = Field(default="", max_length=300)


@router.post("/correction")
def correction(body: CorrectionRequest, x_admin_secret: str = Header(default="")):
    _require_admin(x_admin_secret)
    if not _WORK_KEY.match(body.work_key):
        raise HTTPException(status_code=400, detail="malformed work key")
    if body.field not in _CORRECTABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"field must be one of {sorted(_CORRECTABLE_FIELDS)}")

    current, _ = _get_json(ADMIN_CORRECTIONS_PATH, {})
    if not isinstance(current, dict):
        current = {}
    entry = dict(current.get(body.work_key) or {})
    entry[body.field] = body.value
    current[body.work_key] = entry

    note = f" — {body.note}" if body.note else ""
    wrote = _commit_files(
        {ADMIN_CORRECTIONS_PATH: _dump(current)},
        f"mind reader admin: correct {body.work_key}.{body.field} "
        f"-> {body.value}{note}")
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "effect": "next full rebuild only",
            "note": "this does not change the live game — the fact feeds "
                    "a matrix bit that only a local build recomputes"}


# ── POST /akinator/admin/question ───────────────────────────────────────

class QuestionRequest(BaseModel):
    question_id: str = Field(..., max_length=42)
    text: str = Field(..., min_length=1, max_length=140)


@router.post("/question")
def question(body: QuestionRequest, x_admin_secret: str = Header(default="")):
    _require_admin(x_admin_secret)
    if not _QUESTION_ID.match(body.question_id):
        raise HTTPException(status_code=400, detail="malformed question id")

    live, _ = _get_json(QUESTIONS_PATH, None)
    if not live:
        raise HTTPException(status_code=502, detail="live questions.json unreadable")
    match = next((q for q in live if q.get("id") == body.question_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="no such live question id")
    match["text"] = body.text

    overrides, _ = _get_json(QUESTION_OVERRIDES_PATH, {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides[body.question_id] = body.text

    # ONE commit, both files — a wording patch that lands in questions.json
    # but not the overlay would be silently reverted by the next rebuild
    # with nobody the wiser.
    wrote = _commit_files({
        QUESTIONS_PATH: _dump_shipped(live),
        QUESTION_OVERRIDES_PATH: _dump(overrides),
    }, f"mind reader admin: reword {body.question_id}")
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "effect": "instant, and survives the next rebuild"}


# ── POST /akinator/admin/book ────────────────────────────────────────────

class BookRequest(BaseModel):
    title: str = Field(..., max_length=200)
    author: str = Field(..., max_length=120)
    year: int | None = Field(default=None, ge=1, le=2100)
    summary: str = Field(default="", max_length=4000)
    themes: list[str] = Field(default_factory=list, max_length=20)


def _first_publish_year(title: str, author: str) -> int | None:
    """The year the WORK appeared — never the edition in front of us.

    THE BUG THIS EXISTS TO AVOID, caught adding a real book through this
    endpoint. `resolve_book()` answers from Google Books first, whose
    `publishedDate` is the date of one *edition*: it returned 2008 for
    "A Fine Balance", a novel published in 1995. Six questions read this
    field (`fact:veryold` .. `fact:verrecent`), and `features.py` is
    explicit that a missing year must answer `unknown` to all six. So the
    edition date is not a small inaccuracy — it flips FIVE of those six
    from an honest "we don't know" to a confident wrong answer, against a
    player who knows perfectly well when the book came out.

    Same lesson the repo already learned twice: a page count is a fact
    about an edition, and a Wikidata id is usually an edition too. Open
    Library's `first_publish_year` is a work-level field, and it is the
    field every one of the 5,000 shipped rows already got its year from —
    so this reads the year off the same scale as everyone else's, for the
    same reason `sync()` takes the popularity floor rather than inventing
    a number on a different one.

    None on any doubt. Six unknowns cost the game far less than five lies.
    """
    import httpx                                              # noqa: E402
    from book_data import OPEN_LIBRARY_SEARCH_API             # noqa: E402
    try:
        r = httpx.get(OPEN_LIBRARY_SEARCH_API, timeout=15.0,
                      params={"title": title, "author": author,
                              "fields": "first_publish_year", "limit": 1},
                      headers={"User-Agent": "Litheca/1.0 (https://litheca.com; "
                                             "hello@litheca.com)"})
        r.raise_for_status()
        docs = r.json().get("docs") or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("first_publish_year lookup failed for %r: %s", title, str(exc)[:120])
        return None
    year = (docs[0] if docs else {}).get("first_publish_year")
    return year if isinstance(year, int) and 1 <= year <= 2100 else None


@router.post("/book")
def book(body: BookRequest, x_admin_secret: str = Header(default="")):
    _require_admin(x_admin_secret)

    from book_data import resolve_book                    # noqa: E402
    from features import normalize                         # noqa: E402

    record = resolve_book(body.title, body.author)
    if not record.found:
        raise HTTPException(
            status_code=404,
            detail="no matching book found via Google Books/Open Library "
                   "— check the title and author")

    live, _ = _get_json(QUESTIONS_PATH, None)
    if not live:
        raise HTTPException(status_code=502, detail="live questions.json unreadable")
    books, _ = _get_json(f"{ARTIFACT_DIR}/books.json", None)
    if books is None:
        raise HTTPException(status_code=502, detail="live books.json unreadable")

    have_titles = {normalize(b.get("t") or "") for b in books}
    if normalize(record.title) in have_titles:
        raise HTTPException(status_code=409, detail="already in the shipped game")

    floor = min((b.get("p") or 0) for b in books) if books else 1
    # The admin's own year wins — they are a human who can check a fact the
    # catalogues get wrong, which is the entire premise of this page. Open
    # Library's work-level year behind it. `record.published_year` is
    # deliberately NOT a third fallback: see `_first_publish_year`.
    year = body.year or _first_publish_year(record.title, record.author)

    doc = {
        "key": "/site/admin-" + normalize(record.title).replace(" ", "-"),
        "title": record.title,
        "author_name": [record.author] if record.author else [],
        "author_key": [],
        "first_publish_year": year,
        "subject": list(record.categories or []) + list(body.themes or []),
        "person": [],
        "language": ["eng"],
        "readinglog_count": floor,
        "ebook_access": "",
    }

    # The admin's own summary first, the catalogue's description behind it.
    # Both are real text about this book, and `_label_traits` can only ever
    # turn `unknown` into `present` from what the text actually supports —
    # so the fallback adds signal and cannot add a wrong answer. Without it,
    # an admin who adds a book without typing a summary gets a row that
    # answers `unknown` to every trait question, which is the thin-row
    # problem `_mark_uncomputed_unknown` documents, not a fix for it.
    result = append_book_row(
        doc, prose=(body.summary or record.description or ""),
        commit_message=f"mind reader admin: +1 book ({record.title})")
    if not result.get("ok"):
        raise HTTPException(status_code=502,
                            detail=result.get("reason", "append failed"))
    return {"ok": True, "effect": "instant", "key": doc["key"],
            "title": record.title, "author": record.author, "year": year}
