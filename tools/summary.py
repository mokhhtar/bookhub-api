"""
tools/summary.py — Book Summarizer (PRIORITY TOOL #1)

Self-contained module: route, prompt, and request/response models all
live here. No other tool module is imported. This isolation means you
can delete, rewrite, or A/B test this tool without touching anything else.

Pipeline:
  1. Resolve the book via book_data.resolve_book() — Google Books first,
     Open Library fallback. If not found, return found=False immediately.
     Gemini is NEVER called for a book we can't verify exists.
  2. Build a grounded prompt that embeds the REAL title, author, official
     description, and category as mandatory context.
  3. Call Gemini 3.1 Flash-Lite at low temperature to summarize FROM that
     context, not from memory.
  4. In the same response, fetch real "similar books" from the same
     category via Google Books / Open Library — not Gemini-invented titles.
  5. Cache the whole assembled response for 30 days per (title, author, depth).
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import cache
import book_data
import gemini_client

log = logging.getLogger("bookhub-api.tools.summary")

router = APIRouter()


# ── Request / response models ──────────────────────────────
class SummaryRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)
    depth: str = Field(default="quick", pattern="^(quick|medium|deep)$")


# ── Prompt (kept local to this tool — not shared) ──────────
DEPTH_INSTRUCTIONS = {
    "quick": "Write ONE concise paragraph (70-100 words) covering the core premise and main takeaway.",
    "medium": (
        "Write 3 short sections using markdown bold headers: **Premise**, **Key Ideas**, **Takeaway**. "
        "Each section 2-4 sentences."
    ),
    "deep": (
        "Write a detailed summary with these markdown sections: **Overview** (2-3 sentences), "
        "**Main Arguments** (4-6 bullet points using markdown -), **Who Should Read This** (1-2 sentences), "
        "**Critical Takeaway** (1-2 sentences). 250-400 words total."
    ),
}


def _build_prompt(record: book_data.BookRecord, depth: str) -> str:
    """
    The prompt embeds VERIFIED data as mandatory context and explicitly
    forbids the model from adding plot details, quotes, or facts that
    are not present in — or directly inferable from — that context.
    """
    instruction = DEPTH_INSTRUCTIONS.get(depth, DEPTH_INSTRUCTIONS["quick"])

    description_block = (
        record.description
        if len(record.description) > 200
        else f"{record.description}\n\n(Note: only a short excerpt is available for this book — "
             f"summarize cautiously and avoid inventing specific plot details, characters, or "
             f"quotes that are not implied by this excerpt or the category below.)"
    )

    return f"""You are a literary analyst. Summarize the following book using ONLY the verified information given below. Do not use outside knowledge to add plot points, character names, statistics, or quotes that are not present in or directly implied by this context.

VERIFIED BOOK DATA (source: {record.source}):
Title: {record.title}
Author: {record.author}
Category: {record.primary_category or "unspecified"}
Official description / excerpt:
\"\"\"
{description_block}
\"\"\"

TASK:
{instruction}

RULES:
- Base your summary strictly on the verified data above.
- Do not contradict the description.
- Do not invent named characters, events, or statistics absent from the description.
- No preamble like "Here is a summary" — start directly with the content.
- Use markdown **bold** for section headers if the depth requires them. No # headers.
- Sentence case, clear prose, no filler phrases."""


def _build_chapters_prompt(record: book_data.BookRecord) -> str:
    """
    Chapter titles are NOT available as structured data from Google Books
    or Open Library search responses — there is no reliable API field for
    a table of contents. So this is the one place we let Gemini use its
    own training knowledge of this SPECIFIC, already-verified book, with
    an explicit instruction to admit uncertainty rather than invent a
    plausible-looking but fake chapter list.
    """
    return f"""You are a literary reference assistant.

The book "{record.title}" by {record.author} (category: {record.primary_category or "unspecified"}) has been verified to exist via {record.source}.

TASK: List this book's actual chapter titles or part/section titles, if you reliably know them from your training knowledge of this specific, real book.

RULES:
- Only list titles you are confident are accurate for THIS book — do not guess or generate plausible-sounding generic chapter names.
- If you do not have reliable knowledge of this book's actual chapter structure, return an empty list — do not fabricate one.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format: {{"confident": true_or_false, "chapters": ["Chapter title 1", "Chapter title 2", ...]}}
- Maximum 25 chapters. If the book has parts AND chapters, prefix with the part, e.g. "Part One: Chapter title"."""


# ── Route ───────────────────────────────────────────────────
@router.post("/summary")
def summary(req: SummaryRequest):
    cache_key = ("summary", req.title, req.author, req.depth)
    cached = cache.get(*cache_key)
    if cached:
        if isinstance(cached, dict) and cached.get("found") and "amazon_url" not in cached:
            import os
            import urllib.parse
            tag = os.environ.get("AMAZON_TAG", "oceansidehair-20")
            isbn_10 = cached.get("isbn_10")
            if not isbn_10 and cached.get("isbn_13"):
                from book_data import isbn13_to_isbn10
                isbn_10 = isbn13_to_isbn10(cached["isbn_13"])
                cached["isbn_10"] = isbn_10
            
            if isbn_10:
                cached["amazon_url"] = f"https://www.amazon.com/dp/{isbn_10}?tag={tag}"
            else:
                q = urllib.parse.quote(cached.get("title", req.title))
                cached["amazon_url"] = f"https://www.amazon.com/s?k={q}&tag={tag}"
            
            cache.set(cached, *cache_key)
        return cached

    record = book_data.resolve_book(req.title, req.author)

    if not record.found:
        result = {
            "found": False,
            "title": req.title,
            "author": req.author,
            "message": (
                f"We couldn't verify \"{req.title}\" in our book sources (Google Books "
                f"or Open Library). Please check the spelling, or try adding the author's name."
            ),
        }
        # Cache "not found" too, but briefly (1 hour) — avoids hammering both
        # APIs for the same typo repeatedly within a session, while letting
        # a corrected/retried query through quickly rather than waiting 30 days.
        cache.set(result, *cache_key, ttl=3600)
        return result

    prompt = _build_prompt(record, req.depth)
    summary_text = gemini_client.generate(prompt)

    similar = book_data.find_similar_by_category(
        record.primary_category, exclude_title=record.title, limit=4
    )

    # Chapters are best-effort and cached separately by (title, author) only
    # — independent of depth — since the chapter list never changes with
    # summary depth, no need to regenerate it per depth variant.
    chapters_cache_key = ("chapters", record.title, record.author)
    chapters_cached = cache.get(*chapters_cache_key)
    if chapters_cached is not None:
        chapters = chapters_cached
    else:
        try:
            chapters_raw = gemini_client.generate(_build_chapters_prompt(record))
            chapters_data = gemini_client.parse_json_response(chapters_raw)
            chapters = chapters_data.get("chapters", []) if chapters_data.get("confident") else []
        except Exception as e:
            log.warning(f"Chapter extraction failed for '{record.title}': {e}")
            chapters = []
        cache.set(chapters, *chapters_cache_key)

    import os
    import urllib.parse
    tag = os.environ.get("AMAZON_TAG", "oceansidehair-20")
    if record.isbn_10:
        amazon_url = f"https://www.amazon.com/dp/{record.isbn_10}?tag={tag}"
    else:
        q = urllib.parse.quote(record.title)
        amazon_url = f"https://www.amazon.com/s?k={q}&tag={tag}"

    result = {
        "found": True,
        "source": record.source,
        "title": record.title,
        "author": record.author,
        "depth": req.depth,
        "summary": summary_text,
        "category": record.primary_category,
        "page_count": record.page_count,
        "published_year": record.published_year,
        "cover_url": record.cover_url,
        "average_rating": record.average_rating,
        "isbn_13": record.isbn_13,
        "isbn_10": record.isbn_10,
        "amazon_url": amazon_url,
        "similar_books": similar,
        "chapters": chapters,
    }
    cache.set(result, *cache_key)
    return result