"""
book_data.py — Grounding layer shared by every tool.

This module's only job is to answer: "does this book actually exist, and
what do we reliably know about it?" Nothing downstream (Gemini prompts)
is allowed to run until this module confirms a real match.

Source priority:
  1. Google Books API   — richest metadata (official publisher description,
                           categories, page count, cover, average rating)
  2. Open Library API    — fallback when Google Books has no match
                           (huge catalog via Internet Archive, no API key,
                           strong for older/obscure/non-English titles)

If neither source finds the book, callers MUST surface an honest
"not found" response instead of asking Gemini to invent one.
"""

import os
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("bookhub-api.book_data")

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SEARCH_API = "https://openlibrary.org/search.json"
OPEN_LIBRARY_COVERS_API = "https://covers.openlibrary.org/b"

# Open Library asks integrators to identify their app via User-Agent.
HEADERS = {"User-Agent": "BookHub/1.0 (https://github.com/yourusername/bookhub)"}

GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY")  # optional — works without one at low volume


@dataclass
class BookRecord:
    """Normalized book data, regardless of which source it came from."""
    found: bool
    source: str = ""              # "google_books" | "open_library" | "none"
    title: str = ""
    author: str = ""
    description: str = ""         # official/publisher description — used to ground the AI prompt
    categories: list[str] = field(default_factory=list)
    page_count: Optional[int] = None
    published_year: Optional[str] = None
    cover_url: Optional[str] = None
    average_rating: Optional[float] = None
    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    google_volume_id: Optional[str] = None
    open_library_work_key: Optional[str] = None

    @property
    def primary_category(self) -> str:
        return self.categories[0] if self.categories else ""


def isbn13_to_isbn10(isbn13: str) -> Optional[str]:
    if not isbn13:
        return None
    clean = "".join(c for c in isbn13 if c.isdigit())
    if len(clean) != 13 or not clean.startswith("978"):
        return None
    digits = clean[3:12]
    val = sum((10 - i) * int(d) for i, d in enumerate(digits))
    rem = val % 11
    chk = 11 - rem
    if chk == 10:
        chk_str = "X"
    elif chk == 11:
        chk_str = "0"
    else:
        chk_str = str(chk)
    return digits + chk_str


def _empty_record() -> BookRecord:
    return BookRecord(found=False, source="none")


def _query_google_books(title: str, author: str = "") -> Optional[BookRecord]:
    """
    Query Google Books. This is the PRIMARY source because it returns the
    official publisher/jacket description — the single most important
    grounding signal for the summarizer prompt.
    """
    q_parts = [f'intitle:{title}']
    if author:
        q_parts.append(f'inauthor:{author}')
    query = " ".join(q_parts)

    params = {"q": query, "maxResults": 5, "printType": "books", "langRestrict": "en"}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY

    try:
        resp = httpx.get(GOOGLE_BOOKS_API, params=params, headers=HEADERS, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Google Books query failed: {e}")
        return None

    items = data.get("items", [])
    if not items:
        # Retry without langRestrict in case it's a foreign book only
        params.pop("langRestrict", None)
        try:
            resp = httpx.get(GOOGLE_BOOKS_API, params=params, headers=HEADERS, timeout=8.0)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
        except Exception as e:
            log.warning(f"Google Books fallback query failed: {e}")
            return None

    if not items:
        return None

    # Find the best English item (longest description)
    english_items = [it for it in items if it.get("volumeInfo", {}).get("language", "").lower() == "en"]
    english_best = max(english_items, key=lambda it: len(it.get("volumeInfo", {}).get("description", ""))) if english_items else None

    # Find the absolute best item overall (longest description, regardless of language)
    overall_best = max(items, key=lambda it: len(it.get("volumeInfo", {}).get("description", "")))

    # Determine which item to use for metadata vs description
    if english_best:
        info = english_best.get("volumeInfo", {})
        best_item = english_best
        
        # Decide description: if overall best is significantly richer than the English one, use it
        desc_en = info.get("description", "")
        desc_overall = overall_best.get("volumeInfo", {}).get("description", "")
        if len(desc_overall) > len(desc_en) * 1.5 and len(desc_overall) > 200:
            description = desc_overall
        else:
            description = desc_en
    else:
        info = overall_best.get("volumeInfo", {})
        best_item = overall_best
        description = info.get("description", "")

    if not description:
        return None

    isbn_13 = None
    isbn_10 = None
    for ident in info.get("industryIdentifiers", []):
        if ident.get("type") == "ISBN_13":
            isbn_13 = ident.get("identifier")
        elif ident.get("type") == "ISBN_10":
            isbn_10 = ident.get("identifier")

    if isbn_13 and not isbn_10:
        isbn_10 = isbn13_to_isbn10(isbn_13)

    image_links = info.get("imageLinks", {})
    cover = image_links.get("thumbnail") or image_links.get("smallThumbnail")
    if cover:
        cover = cover.replace("http://", "https://").replace("zoom=1", "zoom=2")

    return BookRecord(
        found=True,
        source="google_books",
        title=info.get("title", title),
        author=", ".join(info.get("authors", [])) or author,
        description=description,
        categories=info.get("categories", []),
        page_count=info.get("pageCount"),
        published_year=(info.get("publishedDate") or "")[:4] or None,
        cover_url=cover,
        average_rating=info.get("averageRating"),
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        google_volume_id=best_item.get("id"),
    )


def _query_open_library(title: str, author: str = "") -> Optional[BookRecord]:
    """
    Fallback source. Open Library's catalog is enormous (backed by the
    Internet Archive) and frequently has titles Google Books misses —
    older books, small-press, non-English, academic texts.

    Trade-off: it rarely has a rich synopsis, so a record sourced from
    here is grounded on title/author/subjects/first-line only — the
    summarizer prompt is told explicitly to lean on general knowledge
    of the WORK (not invent plot specifics) when description is thin.
    """
    params = {
        "title": title,
        "fields": "title,author_name,first_publish_year,subject,"
                   "cover_i,isbn,key,number_of_pages_median,first_sentence,language",
        "limit": 5,
    }
    if author:
        params["author"] = author

    try:
        resp = httpx.get(OPEN_LIBRARY_SEARCH_API, params=params, headers=HEADERS, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Open Library query failed: {e}")
        return None

    docs = data.get("docs", [])
    if not docs:
        return None

    # Prioritize English ("eng" / "en") documents in Open Library
    english_docs = []
    for d in docs:
        langs = d.get("language", [])
        if any(l.lower() in ("eng", "en") for l in langs):
            english_docs.append(d)
            
    candidates = english_docs if english_docs else docs

    # Prefer the doc with the most subjects (richer record) as a quality proxy.
    best = max(candidates, key=lambda d: len(d.get("subject", [])))

    cover_id = best.get("cover_i")
    cover_url = f"{OPEN_LIBRARY_COVERS_API}/id/{cover_id}-L.jpg" if cover_id else None

    first_sentence = best.get("first_sentence")
    if isinstance(first_sentence, list):
        first_sentence = first_sentence[0] if first_sentence else ""

    # Open Library has no synopsis field in search results — we build a
    # minimal "description" from what IS verifiable, and flag it as thin
    # so the prompt layer knows not to treat it like a full jacket blurb.
    description = first_sentence or ""

    isbns = best.get("isbn", [])
    # Prioritize English ISBNs (ISBN-10 starting with 0 or 1, and ISBN-13 starting with 9780 or 9781)
    isbn_13 = next((i for i in isbns if len(i) == 13 and (i.startswith("9780") or i.startswith("9781"))), None)
    isbn_10 = next((i for i in isbns if len(i) == 10 and (i.startswith("0") or i.startswith("1"))), None)

    # Fallback to any ISBN if no English one was found
    if not isbn_13:
        isbn_13 = next((i for i in isbns if len(i) == 13), None)
    if not isbn_10:
        isbn_10 = next((i for i in isbns if len(i) == 10), None)

    if isbn_13 and not isbn_10:
        isbn_10 = isbn13_to_isbn10(isbn_13)

    return BookRecord(
        found=True,
        source="open_library",
        title=best.get("title", title),
        author=", ".join(best.get("author_name", [])) or author,
        description=description,
        categories=(best.get("subject", []) or [])[:8],
        page_count=best.get("number_of_pages_median"),
        published_year=str(best.get("first_publish_year", "")) or None,
        cover_url=cover_url,
        average_rating=None,  # not provided by Open Library search
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        open_library_work_key=best.get("key"),
    )


def resolve_book(title: str, author: str = "") -> BookRecord:
    """
    Main entry point. Tries Google Books first, falls back to Open Library.
    Returns BookRecord(found=False) if neither source has a confident match —
    callers must NOT proceed to AI generation in that case.
    """
    title = (title or "").strip()
    author = (author or "").strip()
    if not title:
        return _empty_record()

    record = _query_google_books(title, author)
    if record:
        log.info(f"Resolved '{title}' via google_books")
        return record

    record = _query_open_library(title, author)
    if record:
        log.info(f"Resolved '{title}' via open_library (fallback)")
        return record

    log.info(f"Could not resolve '{title}' in any source")
    return _empty_record()


def find_similar_by_category(category: str, exclude_title: str = "", limit: int = 4) -> list[dict]:
    """
    Real "similar books" — not Gemini-invented titles. Searches Google
    Books by category/subject and returns actual catalog matches, sorted
    by relevance/rating. Falls back to Open Library subject search.
    """
    if not category:
        return []

    params = {"q": f'subject:"{category}"', "maxResults": limit + 3, "printType": "books",
              "orderBy": "relevance"}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY

    results = []
    try:
        resp = httpx.get(GOOGLE_BOOKS_API, params=params, headers=HEADERS, timeout=8.0)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for it in items:
            info = it.get("volumeInfo", {})
            t = info.get("title", "")
            if not t or t.lower() == exclude_title.lower():
                continue
            results.append({
                "title": t,
                "author": ", ".join(info.get("authors", [])) or "Unknown",
                "cover_url": (info.get("imageLinks", {}) or {}).get("thumbnail", ""),
            })
            if len(results) >= limit:
                break
    except Exception as e:
        log.warning(f"Google Books category search failed: {e}")

    if results:
        return results

    # Fallback: Open Library subject search
    try:
        slug = urllib.parse.quote(category.lower().replace(" ", "_"))
        resp = httpx.get(f"https://openlibrary.org/subjects/{slug}.json",
                          params={"limit": limit + 3}, headers=HEADERS, timeout=8.0)
        resp.raise_for_status()
        works = resp.json().get("works", [])
        for w in works:
            t = w.get("title", "")
            if not t or t.lower() == exclude_title.lower():
                continue
            cover_id = w.get("cover_id")
            results.append({
                "title": t,
                "author": ", ".join(a.get("name", "") for a in w.get("authors", [])) or "Unknown",
                "cover_url": f"{OPEN_LIBRARY_COVERS_API}/id/{cover_id}-M.jpg" if cover_id else "",
            })
            if len(results) >= limit:
                break
    except Exception as e:
        log.warning(f"Open Library subject search failed: {e}")

    return results


def search_books_list(query: str, limit: int = 54, offset: int = 0) -> list[dict]:
    """
    Search books from Google Books (or Open Library fallback) returning a list of matched records.
    Supports offset-based pagination and chunked requests for limit > 40.
    """
    import urllib.parse
    
    items = []
    current_offset = offset
    remaining = limit
    
    # 1. Primary Source: Google Books
    while remaining > 0:
        chunk_size = min(remaining, 40)
        params = {
            "q": query,
            "maxResults": chunk_size,
            "startIndex": current_offset,
            "printType": "books",
            "langRestrict": "en"
        }
        if GOOGLE_BOOKS_API_KEY:
            params["key"] = GOOGLE_BOOKS_API_KEY
            
        try:
            resp = httpx.get(GOOGLE_BOOKS_API, params=params, headers=HEADERS, timeout=8.0)
            resp.raise_for_status()
            chunk_items = resp.json().get("items", [])
            if not chunk_items:
                break
            items.extend(chunk_items)
            if len(chunk_items) < chunk_size:
                break  # No more results
            current_offset += len(chunk_items)
            remaining -= len(chunk_items)
        except Exception as e:
            log.warning(f"Google Books search query failed at offset {current_offset}: {e}")
            break

    # Fallback to Google Books without langRestrict if first attempt returned nothing and offset is 0
    if not items and offset == 0:
        current_offset = offset
        remaining = limit
        while remaining > 0:
            chunk_size = min(remaining, 40)
            params = {
                "q": query,
                "maxResults": chunk_size,
                "startIndex": current_offset,
                "printType": "books"
            }
            if GOOGLE_BOOKS_API_KEY:
                params["key"] = GOOGLE_BOOKS_API_KEY
            try:
                resp = httpx.get(GOOGLE_BOOKS_API, params=params, headers=HEADERS, timeout=8.0)
                resp.raise_for_status()
                chunk_items = resp.json().get("items", [])
                if not chunk_items:
                    break
                items.extend(chunk_items)
                if len(chunk_items) < chunk_size:
                    break
                current_offset += len(chunk_items)
                remaining -= len(chunk_items)
            except Exception as e:
                log.warning(f"Google Books fallback search failed at offset {current_offset}: {e}")
                break

    results = []
    
    if items:
        for it in items:
            info = it.get("volumeInfo", {})
            title = info.get("title", "")
            authors = info.get("authors", [])
            author = ", ".join(authors) if authors else ""
            
            # Extract cover
            image_links = info.get("imageLinks", {})
            cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
            if cover_url and cover_url.startswith("http:"):
                cover_url = cover_url.replace("http:", "https:")
                
            # Extract ISBNs
            isbn_10 = None
            isbn_13 = None
            for ident in info.get("industryIdentifiers", []):
                val = ident.get("identifier", "").replace(" ", "")
                if ident.get("type") == "ISBN_10" and len(val) == 10:
                    isbn_10 = val
                elif ident.get("type") == "ISBN_13" and len(val) == 13:
                    isbn_13 = val

            results.append({
                "title": title,
                "author": author,
                "cover_url": cover_url,
                "isbn_10": isbn_10,
                "isbn_13": isbn_13,
                "published_year": info.get("publishedDate", "")[:4] or None
            })
            
    # 2. Fallback Source: Open Library
    if not results:
        try:
            # Map offset and limit to Open Library page number (1-based index)
            page = (offset // limit) + 1
            resp = httpx.get(f"{OPEN_LIBRARY_SEARCH_API}?q={urllib.parse.quote(query)}&page={page}&limit={limit}", timeout=10.0)
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
            for d in docs:
                title = d.get("title", "")
                authors = d.get("author_name", [])
                author = ", ".join(authors) if authors else ""
                
                cover_id = d.get("cover_i")
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None
                
                isbns = d.get("isbn", [])
                isbn_13 = next((i for i in isbns if len(i) == 13 and (i.startswith("9780") or i.startswith("9781"))), None)
                isbn_10 = next((i for i in isbns if len(i) == 10 and (i.startswith("0") or i.startswith("1"))), None)
                if not isbn_13:
                    isbn_13 = next((i for i in isbns if len(i) == 13), None)
                if not isbn_10:
                    isbn_10 = next((i for i in isbns if len(i) == 10), None)
                
                results.append({
                    "title": title,
                    "author": author,
                    "cover_url": cover_url,
                    "isbn_10": isbn_10,
                    "isbn_13": isbn_13,
                    "published_year": str(d.get("first_publish_year", "")) or None
                })
        except Exception as e:
            log.warning(f"Open Library search fallback failed: {e}")
            
    return results
