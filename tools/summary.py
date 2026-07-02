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
from typing import Optional
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
    isbn: Optional[str] = Field(default=None, max_length=50)
    google_id: Optional[str] = Field(default=None, max_length=50)
    openlibrary_id: Optional[str] = Field(default=None, max_length=100)
    bookwyrm_id: Optional[str] = Field(default=None, max_length=255)


class SearchResponseItem(BaseModel):
    title: str
    author: str
    cover_url: Optional[str] = None
    isbn_10: Optional[str] = None
    isbn_13: Optional[str] = None
    published_year: Optional[str] = None
    google_id: Optional[str] = None
    openlibrary_id: Optional[str] = None
    bookwyrm_id: Optional[str] = None


@router.get("/search", response_model=list[SearchResponseItem])
def search_books(q: str, offset: int = 0):
    query_clean = q.strip().lower()
    if not query_clean:
        return []
    cache_key = ("search", query_clean, str(offset))
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached
    results = book_data.search_books_list(q, limit=54, offset=offset)
    if results:
        cache.set(results, *cache_key, ttl=86400 * 7) # Cache search results for 7 days
    return results


# ── Prompt (kept local to this tool — not shared) ──────────
def _build_prompt(record: book_data.BookRecord, depth: str = "deep") -> str:
    """
    The prompt embeds VERIFIED data as mandatory context and explicitly
    forbids the model from adding plot details, quotes, or facts that
    are not present in — or directly inferable from — that context.
    """
    description_block = (
        record.description
        if len(record.description) > 200
        else f"{record.description}\n\n(Note: only a short excerpt is available for this book — "
             f"summarize cautiously and avoid inventing specific plot details, characters, or "
             f"quotes that are not implied by this excerpt or the category below.)"
    )

    return f"""You are a senior literary analyst and book researcher. Write a comprehensive, detailed, and high-quality study guide and summary for the following book.

Your goal is to produce a rich, informative, and engaging guide of approximately 500-800 words. It must be highly structured with clear HTML sections (using h2, h3, p, ul, li) to make it extremely valuable for readers and optimized for search engine indexing (SEO).

VERIFIED BOOK DATA (source: {record.source}):
Title: {record.title}
Author: {record.author}
Category: {record.primary_category or "unspecified"}
Official description / excerpt:
\"\"\"
{description_block}
\"\"\"

TASK:
Write a comprehensive study guide structured with the following HTML sections:
- A main section header `<h2>1. Core Premise & Overview</h2>` followed by a detailed 150-200 word introduction of the book's main theme, its central thesis, and the problem it attempts to solve inside `<p>` paragraph tags.
- A main section header `<h2>2. Key Concepts & Core Ideas</h2>` followed by 3-4 subheadings using `<h3>` tags for each concept (e.g. `<h3>The Power of Habit</h3>`) and a detailed paragraph (`<p>`) of 3-5 sentences explaining it.
- A main section header `<h2>3. Key Takeaways & Lessons</h2>` followed by a `<ul>` list containing 5-7 detailed, actionable `<li>` bullet points outlining the main lessons, rules, or practical applications. Use `<strong>` inside the list item for the lesson title (e.g. `<li><strong>Start Small:</strong> ...</li>`).
- A main section header `<h2>4. Who Should Read This</h2>` followed by a paragraph (`<p>`) of 2-3 sentences explaining the target audience.
- A main section header `<h2>5. Reader Reviews & Reception</h2>` followed by 3 realistic, synthesized reader reviews based on common Goodreads/BookWyrm critical consensus. Each review must be wrapped EXACTLY in:
  <div class="user-review">
    <div class="user-review-header">
      <span class="user-review-author">Reviewer: [Username/Alias]</span>
      <span class="user-review-stars">★★★★★ [or rating matching sentiment]</span>
    </div>
    <p class="user-review-text">"[Review text content summarizing a key reader praise or critique]"</p>
  </div>
- A main section header `<h2>6. Critical Evaluation & Conclusion</h2>` followed by a concluding analysis paragraph (`<p>`) of the book's impact, style, and contribution.

RULES:
- Base the core summary sections (1, 2, 3, 4, 6) strictly on the verified data above.
- For section 5 (Reviews), you may draw on your broad training knowledge of this specific, verified book's real-world reader consensus (e.g. from Goodreads, BookWyrm).
- Do not contradict the description.
- No preamble like "Here is a summary" — start directly with the HTML content of the first section.
- Output clean, valid, semantic HTML tags ONLY. Do NOT wrap the output in markdown code blocks like ```html ```. Start directly with `<h2>1. Core Premise & Overview</h2>`.
- Do not use markdown syntax (like #, ** or *). Use HTML tags (`<h2>`, `<h3>`, `<p>`, `<ul>`, `<li>`, `<strong>`, `<div>`, `<span>`).
- Ensure the output is detailed, substantial, and reads like a premium-quality study guide."""


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


def _build_awards_prompt(record: book_data.BookRecord) -> str:
    """
    Like chapters, awards are not available from book APIs as structured data.
    We let Gemini use its training knowledge to return a structured JSON list
    of real awards won by this specific, verified book. It must return empty
    rather than fabricating awards it is not confident about.
    """
    return f"""You are a literary reference assistant.

The book "{record.title}" by {record.author} has been verified to exist via {record.source}.

TASK: List any real, verifiable literary awards, prizes, or major honors this book has won, if you reliably know them from your training knowledge (e.g. Pulitzer Prize, Hugo Award, Booker Prize, etc.).

RULES:
- Only list awards you are 100% CONFIDENT this book has actually won. Do not guess, do not invent, and do not include nominations (only winners).
- Include the year the award was won if you know it, otherwise use null.
- Set "logo_url" to null for all items.
- If you have no reliable, verifiable knowledge of this book winning any formal awards, return an empty list — do not invent any.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format: {{"confident": true_or_false, "awards": [{{"name": "Award Name", "year": "2001", "logo_url": null}}, ...]}}
- Maximum 6 awards."""


def _get_amazon_url_from_api(title: str, author: str = "") -> Optional[str]:
    import os
    credential_id = os.environ.get("AMAZON_CREDENTIAL_ID")
    credential_secret = os.environ.get("AMAZON_CREDENTIAL_SECRET")
    partner_tag = os.environ.get("AMAZON_PARTNER_TAG") or os.environ.get("AMAZON_TAG") or "oceansidehair-20"

    if not credential_id or not credential_secret:
        return None

    try:
        from amazon_creatorsapi import AmazonCreatorsApi, Country
        api = AmazonCreatorsApi(
            credential_id=credential_id,
            credential_secret=credential_secret,
            version="3.1",
            tag=partner_tag,
            country=Country.US,
        )
        q = f"{title} {author}".strip()
        res = api.search_items(keywords=q, search_index="Books", item_count=1)
        if res and res.items:
            return res.items[0].detail_page_url
    except Exception as e:
        log.warning(f"Amazon API query failed for '{title}': {e}")

    return None


def _fetch_wikidata_qid(record: book_data.BookRecord) -> Optional[str]:
    import httpx
    url = "https://www.wikidata.org/w/api.php"
    headers = {
        "User-Agent": "BookHubApp/1.0 (https://github.com/mokhhtar/bookhub; mokhhtar@gmail.com) httpx/0.24",
        "Accept": "application/json"
    }
    
    # 1. Try search by Open Library Work Key
    if record.open_library_work_key:
        ol_clean = record.open_library_work_key.replace("/works/", "").replace("/books/", "")
        params = {
            "action": "query",
            "list": "search",
            "srsearch": ol_clean,
            "format": "json"
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                search_results = r.json().get("query", {}).get("search", [])
                if search_results:
                    return search_results[0].get("title")
        except Exception:
            pass

    # 2. Try search by ISBN-13
    if record.isbn_13:
        isbn_clean = record.isbn_13.replace("-", "").strip()
        params = {
            "action": "query",
            "list": "search",
            "srsearch": isbn_clean,
            "format": "json"
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                search_results = r.json().get("query", {}).get("search", [])
                if search_results:
                    return search_results[0].get("title")
        except Exception:
            pass

    # 3. Try search by Title
    params = {
        "action": "wbsearchentities",
        "search": record.title,
        "language": "en",
        "format": "json",
        "limit": 8
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=5.0)
        if r.status_code == 200:
            search_results = r.json().get("search", [])
            book_keywords = {"novel", "book", "play", "story", "literary", "writing", "work", "poem", "biography", "memoir"}
            for res in search_results:
                desc = res.get("description", "").lower()
                if any(kw in desc for kw in book_keywords):
                    return res.get("id")
            if search_results:
                return search_results[0].get("id")
    except Exception:
        pass
        
    return None


def _fetch_wikidata_awards(qid: str) -> list[dict]:
    import httpx
    import urllib.parse
    url = "https://www.wikidata.org/w/api.php"
    headers = {
        "User-Agent": "BookHubApp/1.0 (https://github.com/mokhhtar/bookhub; mokhhtar@gmail.com) httpx/0.24",
        "Accept": "application/json"
    }
    
    params = {
        "action": "wbgetentities",
        "ids": qid,
        "languages": "en",
        "format": "json"
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=5.0)
        if r.status_code != 200:
            return []
            
        entity = r.json().get("entities", {}).get(qid, {})
        claims = entity.get("claims", {})
        
        # P166 is award received
        awards_claims = claims.get("P166", [])
        if not awards_claims:
            return []
            
        award_ids = []
        award_years = {}
        
        for c in awards_claims:
            mainsnak = c.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            value = datavalue.get("value", {})
            if isinstance(value, dict) and "id" in value:
                aid = value["id"]
                award_ids.append(aid)
                
                qualifiers = c.get("qualifiers", {})
                date_claims = qualifiers.get("P585", [])
                year = None
                if date_claims:
                    try:
                        time_val = date_claims[0].get("datavalue", {}).get("value", {}).get("time")
                        if time_val and isinstance(time_val, str):
                            year = time_val.lstrip("+").split("-")[0]
                    except Exception:
                        year = None
                award_years[aid] = year
                
        if not award_ids:
            return []
            
        award_ids = award_ids[:15]
        params2 = {
            "action": "wbgetentities",
            "ids": "|".join(award_ids),
            "props": "labels|claims",
            "languages": "en",
            "format": "json"
        }
        r2 = httpx.get(url, params=params2, headers=headers, timeout=5.0)
        if r2.status_code != 200:
            return []
            
        entities2 = r2.json().get("entities", {})
        results = []
        for aid in award_ids:
            ent = entities2.get(aid, {})
            label = ent.get("labels", {}).get("en", {}).get("value")
            if not label:
                continue
                
            claims2 = ent.get("claims", {})
            logo_url = None
            logo_claims = claims2.get("P154") or claims2.get("P18")
            if logo_claims:
                try:
                    filename = logo_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value")
                    if filename and isinstance(filename, str):
                        logo_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(filename)}"
                except Exception:
                    logo_url = None
                    
            results.append({
                "name": label,
                "year": award_years.get(aid),
                "logo_url": logo_url
            })
        return results
    except Exception as e:
        log.warning(f"Error fetching Wikidata awards for qid {qid}: {e}")
        return []


def resolve_factual_awards(record: book_data.BookRecord) -> list[dict]:
    qid = _fetch_wikidata_qid(record)
    if qid:
        return _fetch_wikidata_awards(qid)
    return []


# ── Route ───────────────────────────────────────────────────
@router.post("/summary")
def summary(req: SummaryRequest):
    cache_key = ("summary_v3", req.title, req.author, req.depth, req.isbn, req.google_id, req.openlibrary_id, req.bookwyrm_id)
    cached = cache.get(*cache_key)
    if cached:
        # Self-healing cache migration: verify if the cached amazon_url is valid and English,
        # or if we can upgrade it now using the Amazon Creators API.
        amazon_url = cached.get("amazon_url", "")
        is_bad_url = False
        if "/dp/" in amazon_url:
            parts = amazon_url.split("/dp/")
            if len(parts) > 1:
                asin = parts[1].split("?")[0]
                # If ASIN doesn't start with 0, 1, or B, it's a foreign/bad print ISBN that will 404 on Amazon US
                if not (asin.startswith("0") or asin.startswith("1") or asin.startswith("B")):
                    is_bad_url = True
        else:
            # Upgrade search fallback URLs to direct product URLs if Amazon API is now configured
            import os
            if not amazon_url or ("s?k=" in amazon_url and os.environ.get("AMAZON_CREDENTIAL_ID")):
                is_bad_url = True

        if isinstance(cached, dict) and cached.get("found") and ("amazon_url" not in cached or is_bad_url):
            import os
            import urllib.parse
            amazon_url = _get_amazon_url_from_api(cached.get("title", req.title), cached.get("author", req.author))
            if not amazon_url:
                tag = os.environ.get("AMAZON_TAG", "oceansidehair-20")
                isbn_10 = cached.get("isbn_10")
                if not isbn_10 and cached.get("isbn_13"):
                    from book_data import isbn13_to_isbn10
                    isbn_10 = isbn13_to_isbn10(cached["isbn_13"])
                    cached["isbn_10"] = isbn_10
                
                if isbn_10 and (isbn_10.startswith("0") or isbn_10.startswith("1")):
                    amazon_url = f"https://www.amazon.com/dp/{isbn_10}?tag={tag}"
                else:
                    q = urllib.parse.quote(f"{cached.get('title', req.title)} {cached.get('author', req.author)}".strip())
                    amazon_url = f"https://www.amazon.com/s?k={q}&tag={tag}"
            cached["amazon_url"] = amazon_url
            cache.set(cached, *cache_key)
        return cached

    record = book_data.resolve_book(req.title, req.author, req.isbn, req.google_id, req.openlibrary_id, req.bookwyrm_id)

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
        cache.set(result, *cache_key, ttl=3600)
        return result

    import concurrent.futures

    def get_summary_text():
        prompt = _build_prompt(record, req.depth)
        return gemini_client.generate(prompt)

    def get_chapters():
        chapters_cache_key = ("chapters", record.title, record.author)
        chapters_cached = cache.get(*chapters_cache_key)
        if chapters_cached is not None:
            return chapters_cached
        try:
            chapters_raw = gemini_client.generate(_build_chapters_prompt(record))
            chapters_data = gemini_client.parse_json_response(chapters_raw)
            chapters = chapters_data.get("chapters", []) if chapters_data.get("confident") else []
        except Exception as e:
            log.warning(f"Chapter extraction failed for '{record.title}': {e}")
            chapters = []
        cache.set(chapters, *chapters_cache_key)
        return chapters

    def get_awards():
        awards_cache_key = ("awards", record.title, record.author)
        awards_cached = cache.get(*awards_cache_key)
        if awards_cached is not None:
            return awards_cached
        
        try:
            awards = resolve_factual_awards(record)
        except Exception as e:
            log.warning(f"Wikidata awards query failed for '{record.title}': {e}")
            awards = []

        cache.set(awards, *awards_cache_key)
        return awards

    def get_similar():
        try:
            return book_data.find_similar_by_category(
                record.primary_category, exclude_title=record.title, limit=4
            )
        except Exception as e:
            log.warning(f"Similar books search failed for '{record.title}': {e}")
            return []

    def get_amazon():
        import os
        import urllib.parse
        amazon_url = _get_amazon_url_from_api(record.title, record.author)
        if not amazon_url:
            tag = os.environ.get("AMAZON_TAG", "oceansidehair-20")
            if record.isbn_10 and (record.isbn_10.startswith("0") or record.isbn_10.startswith("1")):
                amazon_url = f"https://www.amazon.com/dp/{record.isbn_10}?tag={tag}"
            else:
                q = urllib.parse.quote(f"{record.title} {record.author}".strip())
                amazon_url = f"https://www.amazon.com/s?k={q}&tag={tag}"
        return amazon_url

    # Execute all 5 tasks concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_summary = executor.submit(get_summary_text)
        future_chapters = executor.submit(get_chapters)
        future_awards = executor.submit(get_awards)
        future_similar = executor.submit(get_similar)
        future_amazon = executor.submit(get_amazon)

        summary_text = future_summary.result()
        chapters = future_chapters.result()
        awards = future_awards.result()
        similar = future_similar.result()
        amazon_url = future_amazon.result()

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
        "awards": awards,
        "google_volume_id": record.google_volume_id,
        "open_library_work_key": record.open_library_work_key,
    }
    cache.set(result, *cache_key)
    return result


class ChatRequest(BaseModel):
    title: str
    author: str
    summary: str
    question: str
    history: list[dict] = []


@router.post("/summary/chat")
def chat_with_book(req: ChatRequest):
    history_formatted = []
    for h in req.history:
        role = "User" if h.get("role") == "user" else "Assistant"
        history_formatted.append(f"{role}: {h.get('content')}")
    history_str = "\n".join(history_formatted)

    prompt = f"""You are an expert tutor and AI assistant answering questions about the book "{req.title}" by "{req.author}".
Here is the book's verified summary:
---
{req.summary}
---

Answer the user's question as accurately and insightfully as possible. If the question is not related to this book or general knowledge, gently remind them that you are here to discuss "{req.title}".
Format your response using clean markdown. Keep it conversational but concise.

Conversation History:
{history_str}

User Question: {req.question}
Assistant:"""
    
    response = gemini_client.generate(prompt)
    return {"answer": response}