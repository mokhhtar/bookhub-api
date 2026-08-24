"""
tools/akinator_book_search.py — resolve a book before typing it, and
suggest answers before adding it.

READ THIS BEFORE ADDING ANYTHING HERE. The owner's complaint about
`/akinator/admin/book` (in `akinator_admin.py`) was not that it worked
wrong — it verified a typed title/author against `book_data.resolve_book()`
before ever adding a row, exactly as its own docstring says. It was that
the admin typed everything blind: no search across the sources this
project already trusts, no visibility into what the automatic pipeline
would label, and no chance to review before the row went live.

TWO ENDPOINTS, AND NEITHER ONE INVENTS A SOURCE.

    POST /akinator/admin/book/preview   read-only. Runs the SAME pipeline
                                        `_build_book_row` runs at creation
                                        — extract() on subjects, the trait
                                        model on the supplied prose — plus
                                        ONE thing that pipeline does not
                                        do: a single-work Wikidata lookup
                                        for `book:film` / `form:series` /
                                        `char:femalelead`. Returns
                                        suggestions, commits nothing.

Search itself is not reimplemented here — `book_data.search_books_list()`
already runs the exact cascade this session's owner asked for (hand-
verified Fandom catalog first, then a fuzzy Fandom subdomain guess mixed
with Open Library, then Google Books as the final fallback; see that
function's own docstring), and it is already production code behind the
public `GET /search` in `tools/summary.py` — battle-tested by the site's
real book search, not a new cascade built to match a description. The
admin worker calls that endpoint directly (through a thin CORS relay, see
`worker/akinator-admin/index.js`); nothing new was needed on this side for
search itself.

WHY THE WORK-LEVEL LOOKUP NEEDS THE AUTHOR'S QID FIRST, and why it is
allowed to come back with nothing. `harvest_works.py` reaches a book
through its author (`?work wdt:P50 ?author`) because Open Library's own
Wikidata id sits on only 6% of books — going by title alone would reopen
that dead end. So this endpoint accepts an OPTIONAL `author_ol_key`: given
one, it repeats harvest_works.py's join for a single author and matches by
normalised title; given none (a brand-new or unmatched author), it skips
the lookup entirely rather than guess. `book:film`/`form:series` staying
`unknown` costs the game almost nothing — three questions out of the ~47
shipped, and every one of them already reads `unknown` for half the
corpus. A wrong guess here would cost far more.

TRAIT EXTRACTION IS THE SAME CALL `_label_traits` MAKES AT CREATION TIME,
imported rather than re-implemented — `build_prompt`/`parse_response` are
the exact functions the offline extractor and the live sync both use, so
what this preview shows is genuinely what creating the book would produce,
not an approximation of it.
"""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tools.akinator_admin import _require_admin              # noqa: E402
from tools.akinator_authors import _fetch, _wikidata_one       # noqa: E402

log = logging.getLogger("bookhub-api.akinator_book_search")

admin_router = APIRouter(prefix="/akinator/admin/book", tags=["akinator"],
                         dependencies=[Depends(_require_admin)])

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
    "Accept": "application/sparql-results+json",
}

# What THIS query can answer, and it is deliberately not work_traits.
# WORK_QUESTIONS — that dict holds book:film and char:femalelead (its
# wording only), but char:femalelead comes from harvest_protagonist.py's
# CHARACTER-NAME join, not from this author+title query, and this preview
# collects no character names to join against. form:series conversely IS
# one of this query's own OPTIONAL clauses (P179) but is not a
# WORK_QUESTIONS key at all — features.py owns its wording as a
# structural "repair" question. Trusting WORK_QUESTIONS's key set here
# silently promised char:femalelead (which this can never answer) and
# silently dropped form:series (which every row below actually sets).
_WORK_FACT_IDS = ("book:film", "form:series")

# Mirrors harvest_works.py's QUERY exactly (P50 author->work, P144 screen
# adaptation restricted to film/TV, P179 series) — one author instead of a
# batch. Kept identical on purpose: a second copy that drifted by one
# OPTIONAL clause is a second chance to get the fold wrong, and this is the
# one place a wrong answer here would be applied by hand and trusted.
QUERY = """SELECT ?work ?workLabel ?adapted ?series WHERE {
  wd:%s ^wdt:P50 ?work .
  OPTIONAL {
    ?adapted wdt:P144 ?work .
    ?adapted wdt:P31/wdt:P279* ?kind .
    VALUES ?kind { wd:Q11424 wd:Q5398426 }
  }
  OPTIONAL { ?work wdt:P179 ?series }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}"""


def _author_qid(ol_key: str) -> str | None:
    """The single-author P648 lookup, reusing /authors/resolve's own call
    rather than a second copy of it — see that function's docstring for why
    a second Wikidata join here would be a second chance to get it wrong.
    """
    rec = _wikidata_one(ol_key)
    return rec["qid"] if rec else None


def _work_facts(author_qid: str, title: str) -> dict[str, bool | None]:
    """book:film / form:series, matched by author QID + normalised title.

    Returns all-unknown, never raises, if the query fails or nothing
    matches — a Wikidata outage or an unmatched title must read exactly
    like an author Wikidata never heard of, not like a computed "no".
    """
    from features import normalize                            # noqa: E402

    out: dict[str, bool | None] = {k: None for k in _WORK_FACT_IDS}
    wanted = normalize(title)
    if not wanted:
        return out
    try:
        url = ENDPOINT + "?" + urllib.parse.urlencode(
            {"query": QUERY % author_qid, "format": "json"})
        rows = _fetch(url, 45, headers=HEADERS)["results"]["bindings"]
    except Exception as exc:                                  # noqa: BLE001
        log.warning("work lookup failed for %s: %s", author_qid, str(exc)[:120])
        return out

    matched = False
    for row in rows:
        label = row.get("workLabel", {}).get("value", "")
        if normalize(label) != wanted:
            continue
        matched = True
        if "adapted" in row:
            out["book:film"] = True
        if "series" in row:
            out["form:series"] = True
    if matched:
        # A matched work with neither OPTIONAL bound is a real "no" — the
        # same distinction traits_for() draws for a computed False: this
        # work IS documented, and documented-with-no-adaptation is
        # evidence, unlike a book Wikidata never heard of at all.
        if out["book:film"] is None:
            out["book:film"] = False
        if out["form:series"] is None:
            out["form:series"] = False
    return out


# ── POST /akinator/admin/book/preview ────────────────────────────────────

class PreviewRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: str = Field(default="", max_length=120)
    summary: str = Field(default="", max_length=4000)
    subjects: list[str] = Field(default_factory=list, max_length=40)
    author_ol_key: str | None = Field(default=None, max_length=24)
    # Set when the admin picked a /search candidate. The catalogue's own
    # description/categories are fetched server-side (same resolve_book()
    # /book itself calls) rather than making the browser copy text out of a
    # search result by hand — and `summary`/`subjects` above still WIN when
    # supplied, same precedence /book's own docstring states: "the admin's
    # own summary first, the catalogue's description behind it."
    google_id: str | None = Field(default=None, max_length=40)
    openlibrary_id: str | None = Field(default=None, max_length=40)


def _question_text(qid: str) -> str:
    from author_traits import AUTHOR_QUESTIONS                # noqa: E402
    from features import QUESTION_TEXT, STRUCTURAL_QUESTIONS  # noqa: E402
    from traits import TRAIT_QUESTIONS                        # noqa: E402
    from work_traits import WORK_QUESTIONS                    # noqa: E402
    return (QUESTION_TEXT.get(qid) or STRUCTURAL_QUESTIONS.get(qid)
            or AUTHOR_QUESTIONS.get(qid) or WORK_QUESTIONS.get(qid)
            or TRAIT_QUESTIONS.get(qid, qid))


@admin_router.post("/preview")
def preview(body: PreviewRequest):
    """What the game would say about this book, before it is added.

    RUNS THE REAL PIPELINE, not a mirror of it. `extract()` here is the
    exact function `_build_book_row` calls, on the exact doc shape it
    builds — a subject list a picked Open Library/Google Books candidate
    supplies, or empty for a from-scratch manual entry, in which case
    every subject-derived question honestly reads unknown, same as any
    other book with no metadata.
    """
    from book_data import resolve_book                        # noqa: E402
    from characters import extract_characters                 # noqa: E402
    from features import extract                               # noqa: E402
    from traits import TRAIT_QUESTIONS, build_prompt, parse_response  # noqa: E402

    fetched_summary, fetched_year = "", None
    subjects = list(body.subjects)
    if body.google_id or body.openlibrary_id:
        try:
            record = resolve_book(body.title, body.author,
                                  google_id=body.google_id,
                                  openlibrary_id=body.openlibrary_id)
            if record.found:
                fetched_summary = record.description or ""
                fetched_year = (int(record.published_year)
                               if record.published_year and
                               str(record.published_year).isdigit() else None)
                if not subjects:
                    subjects = list(record.categories or [])
        except Exception as exc:                              # noqa: BLE001
            log.warning("preview resolve_book failed for %r: %s",
                       body.title[:60], str(exc)[:120])

    doc = {
        "key": "/site/preview", "title": body.title,
        "author_name": [body.author] if body.author else [],
        "author_key": [], "first_publish_year": None,
        "subject": subjects, "person": [], "language": ["eng"],
        "readinglog_count": 0, "ebook_access": "",
    }
    book = extract(doc, 0, 1)
    _, _ = extract_characters(book.pop("persons"))
    present = set(book["present"])
    unknown = set(book["unknown"])
    known_false = set(book["known_false"])
    source: dict[str, str] = {q: "subject" for q in present}

    # The trait model, on whatever prose was supplied — same prompt, same
    # parser as _label_traits, so what this shows is what creating the
    # book would actually label. Same precedence /book's own docstring
    # states: the admin's own summary first, the catalogue's behind it.
    summary = body.summary.strip() or fetched_summary.strip()
    trait_error = None
    if summary:
        try:
            from gemini_client import generate
            labels = set(parse_response(generate(
                build_prompt(body.title, body.author, summary[:4000]))))
        except Exception as exc:                              # noqa: BLE001
            trait_error = str(exc)[:120]
            log.warning("preview trait extraction failed for %r: %s",
                       body.title[:60], trait_error)
            labels = set()
        for qid in TRAIT_QUESTIONS:
            if qid in labels:
                present.add(qid)
                source[qid] = "trait model"
            else:
                unknown.add(qid)

    # The one thing extract() and the trait model cannot reach: work-level
    # Wikidata facts, and only when the author is already identified.
    if body.author_ol_key:
        qid = _author_qid(body.author_ol_key)
        if qid:
            for wqid, value in _work_facts(qid, body.title).items():
                if value is None:
                    unknown.add(wqid)
                elif value:
                    present.add(wqid)
                    source[wqid] = "wikidata"
                else:
                    known_false.add(wqid)
                    source[wqid] = "wikidata"
        else:
            unknown |= set(_WORK_FACT_IDS)
    else:
        unknown |= set(_WORK_FACT_IDS)

    all_ids = sorted(present | unknown | known_false)
    return {
        "ok": True,
        "questions": [
            {"id": q, "text": _question_text(q),
             "suggested": True if q in present
                         else False if q in known_false else None,
             "source": source.get(q)}
            for q in all_ids
        ],
        # Fetched only, never returned if the admin already supplied one —
        # the form fills a GAP, it never overwrites text someone typed.
        "fetched_summary": fetched_summary if not body.summary.strip() else None,
        "fetched_year": fetched_year,
        "fetched_subjects": subjects if not body.subjects else None,
        "trait_error": trait_error,
        "note": "read-only — nothing is written until /book is called with "
                "the reviewed answers",
    }
