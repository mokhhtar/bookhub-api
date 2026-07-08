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
import re
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

import cache
import book_data
import gemini_client
import taxonomy
import slug as slug_mod
import github_publisher

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
    source: Optional[str] = None


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
- A main section header `<h2>5. Critical Evaluation & Conclusion</h2>` followed by a concluding analysis paragraph (`<p>`) of the book's impact, style, and contribution.

RULES:
- Base ALL sections (1–5) strictly on the verified data above. Do NOT use your own training knowledge about this book or series — only what is stated in the description block.
- Never invent reader reviews, reviewer names/usernames, review quotes, or ratings — real rating/review data is sourced and displayed separately, from verified providers, elsewhere on the page. Do not simulate or synthesize this content under any section.
- Do not contradict the description.
- No preamble like "Here is a summary" — start directly with the HTML content of the first section.
- Output clean, valid, semantic HTML tags ONLY. Do NOT wrap the output in markdown code blocks like ```html ```. Start directly with `<h2>1. Core Premise & Overview</h2>`.
- Do not use markdown syntax (like #, ** or *). Use HTML tags (`<h2>`, `<h3>`, `<p>`, `<ul>`, `<li>`, `<strong>`, `<div>`, `<span>`).
- Ensure the output is detailed, substantial, and reads like a premium-quality study guide."""



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


# Wikidata P31 (instance-of) values that identify a written work. Used to
# verify a candidate entity really IS a book before its awards are shown —
# a band/film/game sharing the book's name must never pass.
_BOOK_CLASS_QIDS = {
    "Q571",       # book
    "Q7725634",   # literary work
    "Q47461344",  # written work
    "Q8261",      # novel
    "Q277759",    # book series
    "Q1667921",   # novel series
    "Q747381",    # light novel
    "Q49084",     # short story
    "Q1004",      # comics
    "Q21198342",  # manga series
    "Q25379",     # play
    "Q5185279",   # poem
}

_WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
_WIKIDATA_HEADERS = {
    "User-Agent": "BookHubApp/1.0 (https://github.com/mokhhtar/bookhub; mokhhtar@gmail.com) httpx/0.24",
    "Accept": "application/json",
}


def _first_book_qid(qids: list) -> Optional[str]:
    """
    Returns the first candidate QID that is verifiably a written work:
    P31 in the book-class set, OR carries a P50 (author) claim — P50 is a
    generic strong book signal that also covers specific P31 subclasses
    ("heroic fantasy novel", etc.) missing from the set. Bands, films and
    games have neither. No candidate passes → None (no awards is better
    than the wrong entity's awards).
    """
    import httpx
    qids = [q for q in qids if isinstance(q, str) and re.match(r"^Q\d+$", q)][:8]
    if not qids:
        return None
    try:
        r = httpx.get(_WIKIDATA_URL, params={
            "action": "wbgetentities", "ids": "|".join(qids),
            "props": "claims", "format": "json",
        }, headers=_WIKIDATA_HEADERS, timeout=6.0)
        if r.status_code != 200:
            return None
        entities = r.json().get("entities", {})
        for qid in qids:
            claims = (entities.get(qid) or {}).get("claims", {})
            for claim in claims.get("P31", []):
                value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
                if value.get("id") in _BOOK_CLASS_QIDS:
                    return qid
            if "P50" in claims:  # has an author → written work
                return qid
    except Exception as e:
        log.warning(f"Wikidata P31 verification failed: {e}")
    return None


def _fetch_wikidata_qid(record: book_data.BookRecord) -> Optional[str]:
    import httpx

    # 1. Candidates from Open Library work-key search
    if record.open_library_work_key:
        ol_clean = record.open_library_work_key.replace("/works/", "").replace("/books/", "")
        try:
            r = httpx.get(_WIKIDATA_URL, params={
                "action": "query", "list": "search", "srsearch": ol_clean, "format": "json",
            }, headers=_WIKIDATA_HEADERS, timeout=5.0)
            if r.status_code == 200:
                candidates = [res.get("title") for res in r.json().get("query", {}).get("search", [])[:5]]
                qid = _first_book_qid(candidates)
                if qid:
                    return qid
        except Exception:
            pass

    # 2. Candidates from ISBN-13 search
    if record.isbn_13:
        isbn_clean = record.isbn_13.replace("-", "").strip()
        try:
            r = httpx.get(_WIKIDATA_URL, params={
                "action": "query", "list": "search", "srsearch": isbn_clean, "format": "json",
            }, headers=_WIKIDATA_HEADERS, timeout=5.0)
            if r.status_code == 200:
                candidates = [res.get("title") for res in r.json().get("query", {}).get("search", [])[:5]]
                qid = _first_book_qid(candidates)
                if qid:
                    return qid
        except Exception:
            pass

    # 3. Candidates from title search — entities whose description mentions
    #    the author's surname rank first, then everything else; the P31/P50
    #    gate makes the final call either way.
    try:
        r = httpx.get(_WIKIDATA_URL, params={
            "action": "wbsearchentities", "search": record.title,
            "language": "en", "format": "json", "limit": 8,
        }, headers=_WIKIDATA_HEADERS, timeout=5.0)
        if r.status_code == 200:
            search_results = r.json().get("search", [])
            surname = ""
            if record.author:
                parts = record.author.split(",")[0].strip().split()
                surname = parts[-1].lower() if parts else ""
            by_author = [res for res in search_results
                         if surname and surname in (res.get("description", "") or "").lower()]
            others = [res for res in search_results if res not in by_author]
            candidates = [res.get("id") for res in by_author + others]
            return _first_book_qid(candidates)
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


# ── Ratings (Goodreads-first, Open Library fallback) ─────────
def _normalize_title_for_match(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _fetch_goodreads_rating(title: str, author: str) -> Optional[dict]:
    """
    Goodreads killed their public API (2020); their site's own autocomplete
    endpoint returns clean JSON (avgRating, ratingsCount, bookUrl, numPages)
    without HTML parsing. Unofficial — treated as best-effort with a strict
    title-match guard and full fallback to Open Library when it breaks.
    """
    import httpx

    def _build(cand):
        out = {
            "source": "goodreads",
            "average": round(float(cand.get("avgRating")), 2),
            "count": int(cand.get("ratingsCount")),
            "url": "https://www.goodreads.com" + (cand.get("bookUrl") or ""),
        }
        pages = cand.get("numPages")
        if isinstance(pages, int) and pages > 0:
            out["pages"] = pages
        return out

    surname = ""
    if author:
        parts = author.split(",")[0].strip().split()
        surname = parts[-1].lower() if parts else ""

    knockoff = re.compile(r"^\s*(a\s+|the\s+)?(summary|workbook|study guide|analysis|key takeaways|conversations?)\b", re.IGNORECASE)
    req_vol = book_data.extract_volume_number(title)

    def _fetch(query):
        """One autocomplete call with one retry (GR throttles datacenter IPs)."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.goodreads.com/",
        }
        for attempt in range(2):
            try:
                r = httpx.get(
                    "https://www.goodreads.com/book/auto_complete",
                    params={"format": "json", "q": (query or "")[:120]},
                    headers=headers, timeout=7.0, follow_redirects=True,
                )
                if r.status_code == 200:
                    return r.json() or []
                log.warning(f"Goodreads returned {r.status_code} for '{query}' (attempt {attempt + 1})")
            except Exception as e:
                log.warning(f"Goodreads request error for '{query}' (attempt {attempt + 1}): {e}")
        return None

    def _match_volume(candidates):
        try:
            want_vol_int = int(req_vol)
        except (TypeError, ValueError):
            return None
        want_base = _normalize_title_for_match(book_data.get_base_title(title))
        best = None
        for cand in candidates:
            raw_title = cand.get("bookTitleBare") or cand.get("title", "")
            avg = float(cand.get("avgRating") or 0)
            count = int(cand.get("ratingsCount") or 0)
            if not avg or not count or knockoff.match(raw_title):
                continue
            cand_vol = book_data.extract_volume_number(raw_title)
            # Integer compare — GR often zero-pads ("Vol. 02" vs our "Volume 5").
            if not cand_vol or int(cand_vol) != want_vol_int:
                continue
            cand_base = _normalize_title_for_match(book_data.get_base_title(raw_title))
            if not (want_base and cand_base and (want_base in cand_base or cand_base in want_base)):
                continue
            if best is None or count > int(best.get("ratingsCount") or 0):
                best = cand
        return best

    def _match_title(candidates):
        want = _normalize_title_for_match(title)
        if not want:
            return None
        exact, loose = None, None
        for cand in candidates:
            raw_title = cand.get("bookTitleBare") or cand.get("title", "")
            got = _normalize_title_for_match(raw_title)
            avg = float(cand.get("avgRating") or 0)
            count = int(cand.get("ratingsCount") or 0)
            cand_author = ((cand.get("author") or {}).get("name") or "").lower()
            if not got or not avg or not count:
                continue
            if (surname and surname not in cand_author) or knockoff.match(raw_title):
                continue
            if got == want or got.startswith(want):
                exact = cand
                break
            if want in got or got in want:  # subtitle diffs either direction
                if loose is None or count > int(loose.get("ratingsCount") or 0):
                    loose = cand
        return exact or loose

    # Query order: title-only first (cleanest — appending the author derails GR
    # into knock-off "Summary of X" listings for popular books). Fall back to
    # a title+author query only when title-only found nothing — obscure books
    # with generic titles (e.g. "The Social Studies Curriculum") return junk
    # "Packet …" results on a bare-title search but surface correctly with the
    # author appended.
    if req_vol:
        base = book_data.get_base_title(title)
        queries = [f"{base} volume {req_vol}"]
        if surname:
            queries.append(f"{base} volume {req_vol} {surname}")
    else:
        queries = [title]
        if surname:
            queries.append(f"{title} {surname}")

    for query in queries:
        candidates = _fetch(query)
        if not candidates:
            continue
        match = _match_volume(candidates) if req_vol else _match_title(candidates)
        if match:
            return _build(match)
    return None


def _fetch_ol_ratings(record: book_data.BookRecord) -> Optional[dict]:
    """Open Library ratings: average + count + per-star distribution. Free, stable."""
    import httpx
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    work_key = record.open_library_work_key
    try:
        if not work_key:
            for isbn in (record.isbn_13, record.isbn_10):
                if not isbn:
                    continue
                r = httpx.get(f"https://openlibrary.org/isbn/{isbn}.json",
                              headers=headers, timeout=6.0, follow_redirects=True)
                if r.status_code == 200:
                    works = r.json().get("works") or []
                    if works:
                        work_key = works[0].get("key")
                        break
        if not work_key:
            return None
        key = work_key.strip("/").replace("works/", "")
        r = httpx.get(f"https://openlibrary.org/works/{key}/ratings.json",
                      headers=headers, timeout=6.0, follow_redirects=True)
        if r.status_code != 200:
            return None
        data = r.json()
        summary_d = data.get("summary") or {}
        counts = data.get("counts") or {}
        if not summary_d.get("count"):
            return None
        return {
            "source": "open_library",
            "average": round(float(summary_d.get("average") or 0), 2),
            "count": int(summary_d.get("count") or 0),
            "distribution": {str(s): int(counts.get(str(s)) or 0) for s in range(1, 6)},
        }
    except Exception as e:
        log.warning(f"Open Library ratings lookup failed for '{record.title}': {e}")
    return None


# A per-star distribution is only shown when it has at least this many ratings.
# Open Library often holds a handful (sometimes ONE) rating for a book that
# Goodreads has thousands of — merging that 1-rating distribution under a
# "1,559 ratings" Goodreads headline produced an absurd, broken-looking
# breakdown (a single 4★ bar at 100%). Below the floor, we show the Goodreads
# headline alone and omit the breakdown.
MIN_DISTRIBUTION_RATINGS = 30


def resolve_ratings(record: book_data.BookRecord) -> Optional[dict]:
    """
    Goodreads first (user preference; far larger rating pool), Open Library as
    fallback. Open Library also supplies the per-star DISTRIBUTION (GR's endpoint
    has none) — but only when it's a meaningful sample, and always labelled with
    its OWN source/count so it's never implied to match the GR headline count.
    Fail-open: None means the frontend falls back to Google Books' average.
    """
    cache_key = ("ratings_v3", record.title, record.author)
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached or None  # {} sentinel → None

    ratings = _fetch_goodreads_rating(record.title, record.author)
    ol = _fetch_ol_ratings(record)

    def _dist_total(d):
        return sum(int(v) for v in (d or {}).values())

    if ratings:
        # Attach OL's distribution to the GR headline ONLY if it's substantial.
        if ol and ol.get("distribution") and _dist_total(ol["distribution"]) >= MIN_DISTRIBUTION_RATINGS:
            ratings["distribution"] = ol["distribution"]
            ratings["distribution_source"] = "open_library"
            ratings["distribution_count"] = _dist_total(ol["distribution"])
    elif ol:
        # Pure Open Library result — count and distribution are the same
        # population, so they're inherently consistent. Still gate the breakdown.
        ratings = ol
        if ratings.get("distribution") and _dist_total(ratings["distribution"]) >= MIN_DISTRIBUTION_RATINGS:
            ratings["distribution_source"] = "open_library"
            ratings["distribution_count"] = _dist_total(ratings["distribution"])
        else:
            ratings.pop("distribution", None)

    if ratings:
        cache.set(ratings, *cache_key)          # good result → cache 30 days
    else:
        # Goodreads throttles datacenter IPs intermittently, so a miss is often
        # transient — cache the negative only briefly so the next visit retries
        # instead of hiding ratings for 30 days.
        cache.set({}, *cache_key, ttl=3600)
    return ratings


# ── Similar books: AI proposes, catalog verifies ────────────
def _propose_similar_titles(record: book_data.BookRecord, n: int = 8) -> list[tuple]:
    """
    Ask Gemini for real, same-genre books a fan of this one would enjoy. Only
    TITLES are AI-sourced — each is verified against the real catalog before
    being shown (see _similar_books), so nothing hallucinated ever surfaces.
    This is the only approach that works for web/light novels, whose genre isn't
    captured in Google Books / Open Library subject data.
    """
    ctx = ""
    if record.primary_category:
        ctx += f"Category: {record.primary_category}\n"
    if record.description:
        ctx += f"About: {record.description[:400]}\n"

    prompt = f"""You recommend books to a reader who just finished one and wants MORE LIKE IT.

BOOK: "{record.title}" by {record.author or "Unknown"}
{ctx}
List {n} real, actually-published books a fan of this one would enjoy — same genre, themes, tone, and audience. If this is a web novel / light novel, recommend other novels in that same space (e.g. progression fantasy, xianxia, LitRPG, isekai, cultivation), NOT unrelated literary classics.

RULES:
- Real, published books ONLY. Never invent titles or authors.
- Do NOT include "{record.title}" itself or another volume of the SAME series.
- Prefer DIFFERENT authors and a variety of works.
- Order from most to least similar.
- Return ONLY JSON: {{"books": [{{"title": "...", "author": "..."}}, ...]}}"""

    raw = gemini_client.generate(prompt)
    data = gemini_client.parse_json_response(raw)
    out = []
    for b in (data.get("books") or []):
        if isinstance(b, dict):
            t = (b.get("title") or "").strip()
            a = (b.get("author") or "").strip()
            if t:
                out.append((t, a))
    return out


def _build_themes_reading_level_prompt(record: book_data.BookRecord) -> str:
    """
    Themes and reading level are subjective judgments, not verifiable facts
    (unlike awards/ratings), so no external verification step is needed —
    but they're still grounded in the same verified description/category
    used by the main summary, to avoid drifting into invented plot details
    for lesser-known books.
    """
    return f"""BOOK: "{record.title}" by {record.author or "Unknown"}
Category: {record.primary_category or "unspecified"}
Description: {(record.description or "")[:600]}

TASK 1 — Themes: List 3-5 major themes present in this book. Short noun phrases, 1-4 words each (e.g. "Redemption", "Coming of age", "Power and corruption").
TASK 2 — Reading level: A short, general-audience label estimating who this book is written for (e.g. "Middle Grade (ages 8-12)", "Young Adult", "Adult / General Fiction", "Academic / Advanced Reader"). This is a rough estimate, not a certified score — pick the closest label a bookstore shelf tag would use.

Return ONLY JSON: {{"themes": ["...", ...], "reading_level": "..."}}"""


def _themes_and_reading_level(record: book_data.BookRecord) -> dict:
    try:
        raw = gemini_client.generate(_build_themes_reading_level_prompt(record))
        data = gemini_client.parse_json_response(raw)
        themes = [t.strip() for t in (data.get("themes") or []) if isinstance(t, str) and t.strip()][:5]
        reading_level = (data.get("reading_level") or "").strip()[:60]
        return {"themes": themes, "reading_level": reading_level or None}
    except Exception as e:
        log.warning(f"Themes/reading-level generation failed for '{record.title}': {e}")
        return {"themes": [], "reading_level": None}


def _similar_books(record: book_data.BookRecord, limit: int = 4) -> list[dict]:
    """
    Same-genre "similar books": Gemini proposes real titles, each is VERIFIED
    against the real catalog (book_data.verify_book_exists) so every suggestion
    exists and carries the identifiers needed to open its summary directly.
    Tops up from the catalog subject-search fallback if the AI path is thin.
    """
    import concurrent.futures

    exclude_base = book_data.get_base_title(record.title)
    results: list[dict] = []
    seen_base: set[str] = set()

    def _add(item: dict) -> None:
        if not item or not item.get("google_id"):
            return
        title = item.get("title", "")
        if book_data.is_companion_material(title):
            return
        base = book_data.get_base_title(title)
        if not base or base == exclude_base or base in seen_base:
            return
        seen_base.add(base)
        results.append(item)

    # 1. AI proposes → verify each against the real catalog (in parallel).
    try:
        proposed = _propose_similar_titles(record, n=max(8, limit * 2))
    except Exception as e:
        log.warning(f"AI similar-book proposal failed for '{record.title}': {e}")
        proposed = []

    if proposed:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            verified = list(ex.map(lambda ta: book_data.verify_book_exists(ta[0], ta[1]), proposed))
        for item in verified:
            if len(results) >= limit:
                break
            _add(item)

    # 2. Catalog subject-search fallback tops up if the AI path came up short.
    if len(results) < limit:
        try:
            for item in book_data.find_similar_by_subject(record, limit=limit * 2):
                if len(results) >= limit:
                    break
                _add(item)
        except Exception as e:
            log.warning(f"Subject-based similar fallback failed for '{record.title}': {e}")

    return results[:limit]


# ── Free public-domain ebook (Project Gutenberg via Gutendex) ─
GUTENDEX_API = "https://gutendex.com/books/"  # trailing slash — /books 301s

_UA_HEADERS = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}


def _norm_match(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_free_ebook(record: book_data.BookRecord) -> Optional[dict]:
    """
    Public-domain full text via Gutendex (Project Gutenberg's free JSON API,
    no key needed). Only an exact-ish title match with a matching author
    surname is accepted — linking the WRONG free ebook is worse than none.
    """
    import httpx
    # Two attempts with a generous timeout — gutendex.com is noticeably
    # slower from Render's datacenter IP than from residential connections,
    # and a missed lookup here costs a "read free" button on the page.
    results = None
    for attempt in (1, 2):
        try:
            r = httpx.get(GUTENDEX_API, params={"search": record.title},
                          headers=_UA_HEADERS, timeout=10.0, follow_redirects=True)
            if r.status_code == 200:
                results = r.json().get("results") or []
                break
            log.warning(f"Gutendex returned {r.status_code} for '{record.title}' (attempt {attempt})")
        except Exception as e:
            log.warning(f"Gutendex lookup failed for '{record.title}' (attempt {attempt}): {e}")
    if results is None:
        return None

    want_title = _norm_match(record.title)
    author_last = _norm_match(record.author).split(" ")[-1] if record.author else ""
    for item in results[:10]:
        if item.get("copyright"):  # still under copyright → not free to read
            continue
        got_title = _norm_match(item.get("title", ""))
        if not (want_title == got_title or want_title in got_title or got_title in want_title):
            continue
        authors = " ".join(a.get("name", "") for a in item.get("authors") or []).lower()
        if author_last and author_last not in authors:
            continue
        fmts = item.get("formats") or {}
        read_url = next((u for k, u in fmts.items() if k.startswith("text/html")), None)
        epub_url = fmts.get("application/epub+zip")
        if not (read_url or epub_url):
            continue
        return {
            "source": "project_gutenberg",
            "gutenberg_id": item.get("id"),
            "page_url": f"https://www.gutenberg.org/ebooks/{item.get('id')}",
            "read_url": read_url,
            "epub_url": epub_url,
            "txt_url": next((u for k, u in fmts.items() if k.startswith("text/plain")), None),
        }
    return None


# ── Real attributed quotes (Wikiquote, CC BY-SA) ─────────────
WIKIQUOTE_API = "https://en.wikiquote.org/w/api.php"

# Sections whose bullets are NOT quotes from the book itself.
_WQ_SKIP_SECTIONS = ("about", "see also", "external links", "cast", "criticism", "reviews")


def _clean_wikitext(line: str) -> str:
    line = re.sub(r"<ref[^>]*>.*?</ref>", "", line)
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"\{\{[^}]*\}\}", "", line)
    line = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", line)  # [[target|label]] → label
    line = line.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", line).strip()


def resolve_wikiquote_quotes(record: book_data.BookRecord, limit: int = 5) -> Optional[dict]:
    """
    Real quotes from the book's own Wikiquote page. Same sourcing policy as
    ratings/awards: quotes are never generated, only fetched — and only when
    a page clearly matching THIS book's title exists (author pages and
    near-miss titles are rejected). Returns None when there's no page.
    """
    import httpx
    import urllib.parse
    try:
        r = httpx.get(WIKIQUOTE_API, params={
            "action": "opensearch", "search": record.title, "limit": 5, "format": "json",
        }, headers=_UA_HEADERS, timeout=6.0)
        titles = (r.json() or [None, []])[1]
    except Exception as e:
        log.warning(f"Wikiquote search failed for '{record.title}': {e}")
        return None

    want = _norm_match(record.title)
    page = next((t for t in titles if _norm_match(t) == want), None)
    if not page:
        return None

    try:
        r = httpx.get(WIKIQUOTE_API, params={
            "action": "parse", "page": page, "prop": "wikitext",
            "format": "json", "redirects": 1,
        }, headers=_UA_HEADERS, timeout=8.0)
        wikitext = r.json()["parse"]["wikitext"]["*"]
    except Exception as e:
        log.warning(f"Wikiquote parse failed for '{page}': {e}")
        return None

    quotes, skip = [], False
    for raw in wikitext.splitlines():
        line = raw.strip()
        if line.startswith("=="):
            heading = line.strip("= ").lower()
            skip = any(s in heading for s in _WQ_SKIP_SECTIONS)
            continue
        # Top-level bullets are the quotes; ** sub-bullets are attribution notes.
        if skip or not line.startswith("*") or line.startswith("**"):
            continue
        text = _clean_wikitext(line.lstrip("*").strip())
        if 40 <= len(text) <= 300:
            quotes.append(text)
        if len(quotes) >= limit:
            break

    if not quotes:
        return None
    return {
        "texts": quotes,
        "source": "wikiquote",
        "source_url": f"https://en.wikiquote.org/wiki/{urllib.parse.quote(page.replace(' ', '_'))}",
        "license": "CC BY-SA",
    }


# Cached wrappers shared by the fresh-build path and the cached-read
# self-heal below. Positives keep the default 30-day TTL; negatives ({})
# expire after 1h so a transient Gutendex/Wikiquote failure at generation
# time doesn't hide a "read free" button or the quotes card for a month.
def _cached_free_ebook(record: book_data.BookRecord) -> Optional[dict]:
    # v2: v1 negatives were written with the 30-day default TTL (pre-fix
    # code), permanently blocking the self-heal — bump past them.
    key = ("free_ebook_v2", record.title, record.author)
    hit = cache.get(*key)
    if hit is not None:
        return hit or None  # {} negative marker → None
    ebook = resolve_free_ebook(record)
    cache.set(ebook or {}, *key, ttl=None if ebook else 3600)
    return ebook


def _cached_quotes(record: book_data.BookRecord) -> Optional[dict]:
    # v2: same 30-day-negative poisoning as free_ebook_v1 above.
    key = ("wikiquote_v2", record.title, record.author)
    hit = cache.get(*key)
    if hit is not None:
        return hit or None
    quotes = resolve_wikiquote_quotes(record)
    cache.set(quotes or {}, *key, ttl=None if quotes else 3600)
    return quotes


# ── Route ───────────────────────────────────────────────────
@router.post("/summary")
def summary(req: SummaryRequest, background_tasks: BackgroundTasks):
    # v5: response gained categories/slug/static_page — never serve stale v4 shapes.
    # v6: volume page_count/ratings/consistent-categories fixes — stale v5
    # entries held page_count=None / wrong values for series volumes.
    # v7: removed the fabricated "Reader Reviews & Reception" section (fake
    # reviewer usernames/quotes/ratings) from the prompt — stale v6 entries
    # still carry invented reviews baked into the summary HTML.
    # v8: ratings distribution now gated + author-fallback GR query — stale v7
    # entries carry the 1-rating OL distribution / missing ratings.
    # v9: added themes/reading_level — stale v8 entries have neither field.
    # v10: added free_ebook (Gutenberg) + quotes (Wikiquote) — stale v9
    # entries have neither field.
    cache_key = ("summary_v10", req.title, req.author, req.depth, req.isbn, req.google_id, req.openlibrary_id, req.bookwyrm_id)
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

        # Backfill slug/static_page/author_slug on the way out (cheap Redis
        # lookups) so repeat visitors get the clean static URL once the page
        # is built, even for responses cached before these fields existed.
        if isinstance(cached, dict) and cached.get("found"):
            c_slug = cached.get("slug") or slug_mod.book_slug(cached.get("title", ""))
            ready, actual_slug = github_publisher.resolve_published(
                c_slug, cached.get("google_volume_id") or cached.get("isbn_13") or ""
            )
            cached["slug"] = actual_slug
            cached["static_page"] = ready
            if not cached.get("author_slug"):
                cached["author_slug"] = slug_mod.author_slug(cached.get("author", ""))

            # Self-heal ratings/page_count: a past Goodreads throttle (common
            # from Render's datacenter IP) can leave these empty in an otherwise
            # good cached summary. Retry on read — resolve_ratings has its own
            # short negative cache, so this is cheap and recovers automatically.
            if not cached.get("ratings") or not cached.get("page_count"):
                try:
                    tmp = book_data.BookRecord(
                        found=True, title=cached.get("title", ""), author=cached.get("author", ""),
                        isbn_13=cached.get("isbn_13"), isbn_10=cached.get("isbn_10"),
                        open_library_work_key=cached.get("open_library_work_key"),
                    )
                    fresh = resolve_ratings(tmp)
                    if fresh:
                        cached["ratings"] = fresh
                        if not cached.get("page_count") and fresh.get("pages"):
                            cached["page_count"] = fresh["pages"]
                        cache.set(cached, *cache_key)  # persist the heal
                except Exception as e:
                    log.warning(f"Ratings self-heal failed for '{cached.get('title')}': {e}")

            # Self-heal free_ebook/quotes the same way: a transient
            # Gutendex/Wikiquote failure at generation time bakes None into
            # this 30-day cached response. Both wrappers sit behind their own
            # 1h negative cache, so a book with genuinely no free edition or
            # quotes page costs at most one lookup per hour.
            if cached.get("free_ebook") is None or cached.get("quotes") is None:
                try:
                    tmp = book_data.BookRecord(
                        found=True, title=cached.get("title", ""), author=cached.get("author", ""),
                    )
                    healed = False
                    if cached.get("free_ebook") is None:
                        fe = _cached_free_ebook(tmp)
                        if fe:
                            cached["free_ebook"] = fe
                            healed = True
                    if cached.get("quotes") is None:
                        wq = _cached_quotes(tmp)
                        if wq:
                            cached["quotes"] = wq
                            healed = True
                    if healed:
                        cache.set(cached, *cache_key)  # persist the heal
                except Exception as e:
                    log.warning(f"Free-ebook/quotes self-heal failed for '{cached.get('title')}': {e}")
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
        try:
            from tools.fandom import (
                resolve_series_config_first,
                resolve_fandom_subdomain,
                extract_chapters_from_fandom,
            )
            # Try the structured catalog FIRST — it disambiguates books that
            # share one Fandom subdomain (e.g. "Lord of the Mysteries" vs
            # "Circle of Inevitability", same wiki, different series) via
            # series_filter/exclude_series_patterns. The older
            # resolve_fandom_subdomain has no way to express that distinction
            # and would silently merge them, which is what caused wrong
            # volumes/covers to show up for the wrong title.
            series_config = resolve_series_config_first(record.title)
            subdomain = series_config.subdomain if series_config else resolve_fandom_subdomain(record.title)
            if subdomain:
                chapters = extract_chapters_from_fandom(subdomain, record.title)
                if chapters:
                    return chapters
        except Exception as e:
            log.warning(f"Error fetching Fandom chapters for '{record.title}': {e}")

        # Regular (non-Fandom) books: use Open Library's table of contents when
        # it exists — web/light novels go through Fandom above; mainstream books
        # get their real chapter list here instead of showing none.
        try:
            ol_chapters = book_data.fetch_chapters_from_open_library(
                record.isbn_13, record.isbn_10, record.open_library_work_key
            )
            if ol_chapters:
                return ol_chapters
        except Exception as e:
            log.warning(f"Error fetching Open Library chapters for '{record.title}': {e}")
        return []

    def get_fandom_cover():
        """
        If this title resolves to a cataloged series, prefer its
        hand-verified per-series cover over whatever book_data.py's
        Google Books / Open Library lookup found — those general sources
        often lack correct art for web novels, or (worse) return a cover
        for the wrong edition/series entirely.
        """
        try:
            from tools.fandom import resolve_series_config_first
            series_config = resolve_series_config_first(record.title)
            if series_config and series_config.cover_url:
                return series_config.cover_url
        except Exception as e:
            log.warning(f"Error fetching Fandom cover for '{record.title}': {e}")
        return None

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
            return _similar_books(record, limit=4)
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

    def get_categories():
        """Gemini picks 1-3 slugs FROM OUR FIXED TAXONOMY ONLY (validated);
        falls back to a keyword-mapped default so no book ends up uncategorized.

        Categorized on the SERIES (base) title, not the individual volume, and
        cached by that title — so every volume of one series shares ONE set of
        categories instead of Gemini drifting ("Adventure" on vol 3,
        "Magic & Supernatural" on vol 4) for the same work."""
        # Use the base/series title for volumes so the genre signal is stable.
        cat_title = record.title
        if book_data.extract_volume_number(record.title):
            base = book_data.get_base_title(record.title)
            if base:
                cat_title = base

        # Stored under a RAW key (not the hashed response cache) so a series'
        # categories stay identical across its volumes EVEN when the dev switch
        # DISABLE_RESPONSE_CACHE is on — category consistency is a correctness
        # property, not a perf cache. Bump the version to re-derive after a
        # taxonomy change.
        norm = re.sub(r"[^a-z0-9]+", "-", f"{cat_title}|{record.author}".lower()).strip("-")
        cat_key = f"cat:v1:{norm}"
        cached_cats = cache.get_key(cat_key)
        if cached_cats:
            return cached_cats

        result_cats = None
        try:
            prompt = f"""Assign categories to this book. Choose 1 to 3 category slugs FROM THIS LIST ONLY:

{taxonomy.prompt_list()}

BOOK: "{cat_title}" by {record.author or "Unknown"}
Publisher category: {record.primary_category or "N/A"}
Description: {(record.description or "")[:500]}

Return ONLY JSON: {{"categories": ["slug1", "slug2"]}}"""
            # temperature 0 → the first (cached) computation is as stable as
            # Gemini allows, minimizing drift before the cache is populated.
            from google.genai import types as _gt
            raw = gemini_client.generate(prompt, _gt.GenerateContentConfig(temperature=0.0, max_output_tokens=256))
            data = gemini_client.parse_json_response(raw)
            validated = taxonomy.validate_categories(data.get("categories") or [])
            if validated:
                result_cats = validated
        except Exception as e:
            log.warning(f"Category assignment failed for '{cat_title}': {e}")

        if not result_cats:
            result_cats = [taxonomy.fallback_category(record.primary_category)]
        cache.set_key(cat_key, result_cats)
        return result_cats

    def get_ratings():
        try:
            return resolve_ratings(record)
        except Exception as e:
            log.warning(f"Ratings lookup failed for '{record.title}': {e}")
            return None

    def get_themes_reading_level():
        themes_cache_key = ("themes_v1", record.title, record.author)
        themes_cached = cache.get(*themes_cache_key)
        if themes_cached is not None:
            return themes_cached
        result_tr = _themes_and_reading_level(record)
        cache.set(result_tr, *themes_cache_key)
        return result_tr

    def get_free_ebook():
        return _cached_free_ebook(record)

    def get_quotes():
        return _cached_quotes(record)

    # Execute all 11 tasks concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as executor:
        future_summary = executor.submit(get_summary_text)
        future_chapters = executor.submit(get_chapters)
        future_awards = executor.submit(get_awards)
        future_similar = executor.submit(get_similar)
        future_amazon = executor.submit(get_amazon)
        future_fandom_cover = executor.submit(get_fandom_cover)
        future_categories = executor.submit(get_categories)
        future_ratings = executor.submit(get_ratings)
        future_themes = executor.submit(get_themes_reading_level)
        future_free_ebook = executor.submit(get_free_ebook)
        future_quotes = executor.submit(get_quotes)

        summary_text = future_summary.result()
        chapters = future_chapters.result()
        awards = future_awards.result()
        similar = future_similar.result()
        amazon_url = future_amazon.result()
        fandom_cover = future_fandom_cover.result()
        categories = future_categories.result()
        ratings = future_ratings.result()
        themes_reading_level = future_themes.result()
        free_ebook = future_free_ebook.result()
        quotes = future_quotes.result()

    book_slug = slug_mod.book_slug(record.title)
    static_ready, actual_slug = github_publisher.resolve_published(
        book_slug, record.google_volume_id or record.isbn_13 or ""
    )

    # Volume-specific page count from Goodreads when our sources had none
    # (fandom_series volumes deliberately carry page_count=None — the base
    # title's count applied to every volume was wrong).
    page_count = record.page_count
    if not page_count and ratings and ratings.get("pages"):
        page_count = ratings["pages"]

    result = {
        "found": True,
        "source": record.source,
        "title": record.title,
        "author": record.author,
        "author_slug": slug_mod.author_slug(record.author),
        "depth": req.depth,
        "summary": summary_text,
        "category": record.primary_category,
        "categories": categories,
        "slug": actual_slug,
        # True only once the static page was published >5 min ago (rebuild
        # buffer) — gates the frontend's history.replaceState to the clean URL.
        "static_page": static_ready,
        "page_count": page_count,
        "published_year": record.published_year,
        # Prefer the catalog's hand-verified per-series cover when this
        # title resolved to one — see get_fandom_cover() for why.
        "cover_url": fandom_cover or record.cover_url,
        "average_rating": record.average_rating,
        "ratings": ratings,
        "isbn_13": record.isbn_13,
        "isbn_10": record.isbn_10,
        "amazon_url": amazon_url,
        "similar_books": similar,
        "chapters": chapters,
        "awards": awards,
        "themes": themes_reading_level["themes"],
        "reading_level": themes_reading_level["reading_level"],
        "free_ebook": free_ebook,
        "quotes": quotes,
        "google_volume_id": record.google_volume_id,
        "open_library_work_key": record.open_library_work_key,
    }
    cache.set(result, *cache_key)

    # Publish the static SEO pages in the background — commit failures are
    # logged inside the publisher and never affect this response.
    if github_publisher.is_enabled():
        background_tasks.add_task(github_publisher.publish_book, result)
        background_tasks.add_task(github_publisher.publish_author, record.author, record.title)

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