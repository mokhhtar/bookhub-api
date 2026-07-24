"""
tools/quiz.py — POST /quiz/book: on-demand, quote-verified quizzes for books
BookHub actually has real text grounding for.

Two sources, strictly gated (a book with only a thin blurb NEVER gets a
quiz — same "no data beats wrong data" rule as every other resolver):
  1. gutenberg_text  — the ORIGINAL full text, via reader.py's stored
                       page bundles (public-domain books only).
  2. fandom_summary  — the community wiki's chapter-recap prose (web
                       novels). Disclosed to the reader as such: questions
                       test the wiki's recap, not the original prose.

Generation itself is tools/quiz_core._generate_quiz — the same
verification pipeline PDF-chat uses (every supporting_quote is substring-
verified against the source chunks; unverified questions are dropped).

Quizzes are cached per BOOK (30 days) — unlike pdfchat's ephemeral
per-upload model, one book's quiz serves every visitor.
"""
import logging
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

import book_data
import cache
from tools.quiz_core import _chunk_text, _generate_quiz, _rate_limit, _client_ip

log = logging.getLogger("bookhub-api.tools.quiz")

router = APIRouter()

LIMIT_BOOK_QUIZ_DAILY = int(os.environ.get("BOOK_QUIZ_DAILY", 15))
QUIZ_TTL = 60 * 60 * 24 * 30


class BookQuizRequest(BaseModel):
    title: str
    author: str = ""
    isbn: str | None = None
    google_id: str | None = None
    openlibrary_id: str | None = None
    count: int = Field(default=5, ge=3, le=10)
    # Accepted for backward compat but NO LONGER trusted for rate limiting —
    # a body value is trivially rotated to mint fresh quota. Limits key on the
    # real client IP instead (see _client_ip).
    client_id: str | None = None


def _quiz_from_gutenberg(record: book_data.BookRecord, count: int) -> list[dict] | None:
    """Original-text quiz for public-domain books; None when not free."""
    from tools.summary import _cached_free_ebook
    from tools.reader import get_full_text_pages

    fe = _cached_free_ebook(record)
    if not fe or fe.get("source") != "project_gutenberg" or not fe.get("gutenberg_id"):
        return None
    try:
        gid = int(fe["gutenberg_id"])
    except (TypeError, ValueError):
        return None
    pages = get_full_text_pages(gid)
    if not pages:
        return None
    # Re-chunk at paragraph/sentence boundaries — reader pagination is a
    # reading-UX boundary, not a semantic one, and quote verification is
    # more reliable against clean chunk edges.
    chunks = _chunk_text("\n\n".join(pages))
    digest = f'"{record.title}" by {record.author or "Unknown"} — original full text (Project Gutenberg).'
    questions = _generate_quiz(digest, chunks, count)
    if not questions:  # one retry, like pdfchat's route
        questions = _generate_quiz(digest, chunks, count)
    return questions or None


def generate_static_quiz(title: str, free_ebook: dict | None, count: int = 6,
                          categories: list[str] | None = None) -> dict | None:
    """
    Publish-time quiz generation for the STATIC book page — same verified
    pipeline as POST /quiz/book above, but decoupled from book_data.BookRecord:
    github_publisher._book_markdown() already has `free_ebook` resolved (it's
    already a front-matter field) and has no BookRecord on hand, so this takes
    the plain values it already has instead of re-resolving one.

    `categories` (optional): the site-taxonomy categories already computed by
    _book_markdown() a couple lines before this is called — passed through so
    Fandom resolution can skip non-fiction titles entirely (no legitimate
    dedicated wiki exists for those; see fandom.is_confidently_nonfiction).

    Runs inside publish_book(), itself a FastAPI BackgroundTask — the extra
    Gemini round-trip here does not add latency to the user-facing request.

    Gutenberg-first, Fandom-fallback — same order/logic as quiz_book(). No
    grounding text found in either → None (no data beats wrong data; the
    static page simply omits the Quiz section, same as it omits any other
    ungrounded field).
    """
    from tools.reader import get_full_text_pages

    if free_ebook and free_ebook.get("source") == "project_gutenberg":
        try:
            gid = int(free_ebook.get("gutenberg_id"))
        except (TypeError, ValueError):
            gid = None
        if gid:
            pages = get_full_text_pages(gid)
            if pages:
                chunks = _chunk_text("\n\n".join(pages))
                digest = f'"{title}" — original full text (Project Gutenberg).'
                questions = _generate_quiz(digest, chunks, count) or _generate_quiz(digest, chunks, count)
                if questions:
                    return {"source": "gutenberg_text", "questions": questions}

    from tools.fandom import (
        resolve_series_config_first,
        resolve_fandom_subdomain,
        fetch_fandom_quiz_text,
    )
    series_config = resolve_series_config_first(title, categories=categories)
    subdomain = series_config.subdomain if series_config else resolve_fandom_subdomain(title, categories=categories)
    if subdomain:
        text = fetch_fandom_quiz_text(subdomain, title)
        if text:
            chunks = _chunk_text(text)
            if len(chunks) >= 2:  # a stub page can't ground a fair quiz
                digest = (f'"{title}" — story-wiki chapter summaries (fan-written recaps, '
                          f'not the original prose).')
                questions = _generate_quiz(digest, chunks, count) or _generate_quiz(digest, chunks, count)
                if questions:
                    return {"source": "fandom_summary", "questions": questions}

    return None


@router.post("/quiz/book")
def quiz_book(req: BookQuizRequest, request: Request):
    # Own quota surface (shared per-book cache, many users per book) —
    # distinct from pdfchat's per-upload LIMIT_QUIZ_DAILY. Keyed on the real
    # client IP so the limit can't be bypassed by rotating a body client_id.
    _rate_limit("bookquiz", LIMIT_BOOK_QUIZ_DAILY, _client_ip(request), namespace="bq")

    # v3: gated on fandom.py's title-relevance check — v2 could cache a
    # quiz built from an unrelated book's wiki text for up to 30 days (the
    # exact live bug this fixes: verified-but-wrong-book questions).
    # v2: self-contained-question prompt + main-story-first fandom text.
    cache_key = ("book_quiz_v3", req.title, req.author, str(req.count))
    cached = cache.get(*cache_key)
    if cached:
        return cached

    record = book_data.resolve_book(req.title, req.author, req.isbn,
                                    req.google_id, req.openlibrary_id, None)
    if not record.found:
        return {"available": False, "reason": "book_not_found"}

    result = None
    try:
        questions = _quiz_from_gutenberg(record, req.count)
        if questions:
            result = {"available": True, "source": "gutenberg_text",
                      "title": record.title, "questions": questions}
    except Exception as e:
        log.warning(f"Gutenberg quiz failed for '{record.title}': {e}")

    if result is None:
        try:
            questions = _quiz_from_fandom(record, req.count)
            if questions:
                result = {"available": True, "source": "fandom_summary",
                          "title": record.title, "questions": questions}
        except Exception as e:
            log.warning(f"Fandom quiz failed for '{record.title}': {e}")

    if result is None:
        # Not cached long: availability can change (e.g. text ingested later).
        result = {"available": False, "reason": "no_grounding_text"}
        cache.set(result, *cache_key, ttl=3600)
        return result

    cache.set(result, *cache_key, ttl=QUIZ_TTL)
    return result


def _quiz_from_fandom(record: book_data.BookRecord, count: int) -> list[dict] | None:
    """Wiki chapter-recap quiz for Fandom-backed web novels (B4)."""
    from tools.fandom import (
        resolve_series_config_first,
        resolve_fandom_subdomain,
        fetch_fandom_quiz_text,
    )
    series_config = resolve_series_config_first(record.title, categories=record.categories)
    subdomain = series_config.subdomain if series_config else resolve_fandom_subdomain(record.title, categories=record.categories)
    if not subdomain:
        return None
    text = fetch_fandom_quiz_text(subdomain, record.title)
    if not text:
        return None
    chunks = _chunk_text(text)
    if len(chunks) < 2:  # a stub page can't ground a fair quiz
        return None
    digest = (f'"{record.title}" — story-wiki chapter summaries (fan-written recaps, '
              f'not the original prose).')
    questions = _generate_quiz(digest, chunks, count)
    if not questions:
        questions = _generate_quiz(digest, chunks, count)
    return questions or None
