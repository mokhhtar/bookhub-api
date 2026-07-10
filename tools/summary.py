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
    language: str = Field(default="en", pattern="^(en|ar)$")


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
def _build_prompt(record: book_data.BookRecord, depth: str = "deep", language: str = "en") -> str:
    """
    The prompt embeds VERIFIED data as mandatory context and explicitly
    forbids the model from adding plot details, quotes, or facts that
    are not present in — or directly inferable from — that context.
    language="ar" keeps the exact same structure and grounding rules but
    writes the content in Modern Standard Arabic (section headers too).
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
- Ensure the output is detailed, substantial, and reads like a premium-quality study guide.{'''
- WRITE THE ENTIRE GUIDE IN MODERN STANDARD ARABIC (الفصحى). Translate the section headers too (e.g. `<h2>1. الفكرة الجوهرية ونظرة عامة</h2>`). Keep the book title and author name in their original language, followed by an Arabic transliteration in parentheses on first mention. All grounding rules above still apply — never add facts beyond the verified data.''' if language == "ar" else ''}"""



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


# ── Characters (Wikidata P674 — verified entities only) ──────

def resolve_wikidata_characters(record: book_data.BookRecord) -> Optional[list[dict]]:
    """
    Real characters from the book's Wikidata entry (property P674), each with
    an enwiki sitelink — only well-documented classics have this claim, and
    that's the point: no P674 → None → no characters card, never a guess.
    Reuses _fetch_wikidata_qid's existing P31/P50-gated book resolution.
    Lightweight by design: name + description + wikipedia title only — the
    full grounded bio is written later, in the background, at publish time.
    """
    import httpx

    qid = _fetch_wikidata_qid(record)
    if not qid:
        return None

    cache_key = ("wd_characters_v1", qid)
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached or None  # [] negative marker → None

    try:
        r = httpx.get(_WIKIDATA_URL, params={
            "action": "wbgetentities", "ids": qid,
            "props": "claims", "format": "json",
        }, headers=_WIKIDATA_HEADERS, timeout=8.0)
        if r.status_code != 200:
            return None
        claims = (r.json().get("entities", {}).get(qid, {}) or {}).get("claims", {})
        char_qids = []
        for c in claims.get("P674", [])[:20]:
            try:
                char_qids.append(c["mainsnak"]["datavalue"]["value"]["id"])
            except (KeyError, TypeError):
                continue
        if not char_qids:
            cache.set([], *cache_key, ttl=86400 * 7)
            return None

        # One batch call resolves every character's label/description/sitelink.
        r = httpx.get(_WIKIDATA_URL, params={
            "action": "wbgetentities", "ids": "|".join(char_qids),
            "props": "labels|descriptions|sitelinks", "languages": "en",
            "sitefilter": "enwiki", "format": "json",
        }, headers=_WIKIDATA_HEADERS, timeout=8.0)
        if r.status_code != 200:
            return None
        entities = r.json().get("entities", {})
    except Exception as e:
        log.warning(f"Wikidata P674 lookup failed for '{record.title}': {e}")
        return None

    characters = []
    for cq in char_qids:
        e = entities.get(cq) or {}
        name = ((e.get("labels") or {}).get("en") or {}).get("value", "")
        wiki_title = ((e.get("sitelinks") or {}).get("enwiki") or {}).get("title", "")
        if not name or not wiki_title:
            continue  # no enwiki page → no grounded bio possible → skip
        characters.append({
            "name": name,
            "slug": slug_mod.character_slug(name),
            "description": ((e.get("descriptions") or {}).get("en") or {}).get("value", ""),
            "wikipedia_title": wiki_title,
            "source": "wikidata",
        })

    cache.set(characters, *cache_key, ttl=None if characters else 86400 * 7)
    return characters or None


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


# ── Free public-domain ebook (Open Library → Gutenberg/Archive links) ─
# Gutendex (Project Gutenberg's own JSON API) 403s requests from Render's
# datacenter IP, so availability is resolved through Open Library's search
# API instead — already used throughout this codebase and never blocked.
# The Gutenberg/Archive URLs built here are only ever opened by the
# READER's browser; the server never fetches them.

_UA_HEADERS = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}


def _norm_match(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_free_ebook(record: book_data.BookRecord) -> Optional[dict]:
    """
    Free-to-read edition via Open Library: a Project Gutenberg ebook when
    the record carries one (public domain by definition), else a public
    Internet Archive scan (ebook_access == "public"). Only an exact-ish
    title match with a matching author surname is accepted — linking the
    WRONG free ebook is worse than none.
    """
    import httpx
    try:
        r = httpx.get("https://openlibrary.org/search.json", params={
            "title": record.title,
            "author": record.author or "",
            "fields": "title,author_name,ebook_access,ia,id_project_gutenberg",
            "limit": 5,
        }, headers=_UA_HEADERS, timeout=8.0)
        if r.status_code != 200:
            log.warning(f"OL free-ebook lookup returned {r.status_code} for '{record.title}'")
            return None
        docs = r.json().get("docs") or []
    except Exception as e:
        log.warning(f"OL free-ebook lookup failed for '{record.title}': {e}")
        return None

    want_title = _norm_match(record.title)
    author_last = _norm_match(record.author).split(" ")[-1] if record.author else ""
    for doc in docs:
        got_title = _norm_match(doc.get("title", ""))
        if not (want_title == got_title or want_title in got_title or got_title in want_title):
            continue
        authors = _norm_match(" ".join(doc.get("author_name") or []))
        if author_last and author_last not in authors:
            continue
        gut_ids = doc.get("id_project_gutenberg") or []
        if gut_ids:
            gid = gut_ids[0]
            return {
                "source": "project_gutenberg",
                "gutenberg_id": gid,
                "page_url": f"https://www.gutenberg.org/ebooks/{gid}",
                "read_url": f"https://www.gutenberg.org/ebooks/{gid}.html.images",
                "epub_url": f"https://www.gutenberg.org/ebooks/{gid}.epub3.images",
                "txt_url": f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8",
            }
        if doc.get("ebook_access") == "public" and doc.get("ia"):
            ia_id = doc["ia"][0]
            return {
                "source": "internet_archive",
                "page_url": f"https://archive.org/details/{ia_id}",
                "read_url": f"https://archive.org/details/{ia_id}",
                "epub_url": None,
                "txt_url": None,
            }
    return None


# ── NYT bestseller badge ──────────────────────────────────────
# Snapshot fetching/caching lives in tools/nyt.py (shared with the
# homepage /nyt/weekly rail); this is just the per-book match.

def resolve_nyt_bestseller(record: book_data.BookRecord) -> Optional[dict]:
    """
    "N weeks on the NYT list · currently #R" trust signal for books on the
    CURRENT week's lists (weeks_on_list is cumulative). Same strict
    title+author matching as the other resolvers; None → no badge.
    """
    from tools import nyt as nyt_mod
    want_title = _norm_match(record.title)
    author_last = _norm_match(record.author).split(" ")[-1] if record.author else ""
    best = None
    for b in nyt_mod.overview():
        got_title = _norm_match(b["title"])
        if not (want_title == got_title or want_title in got_title or got_title in want_title):
            continue
        if author_last and author_last not in _norm_match(b["author"]):
            continue
        if not b.get("weeks_on_list"):
            continue
        if best is None or b["weeks_on_list"] > best["weeks_on_list"]:
            best = b
    if not best:
        return None
    return {
        "source": "nyt",
        "weeks_on_list": best["weeks_on_list"],
        "list_name": best["list_name"],
        "rank": best["rank"],
        "review_url": best["review_url"],
    }


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
    # v3: v2 negatives are all Gutendex 403s (blocked from Render's IP);
    # the resolver now goes through Open Library instead.
    key = ("free_ebook_v3", record.title, record.author)
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


def _cached_nyt(record: book_data.BookRecord) -> Optional[dict]:
    # No per-book cache needed: the shared 24h overview snapshot underneath
    # is the only NYT request, and matching against it is pure CPU.
    return resolve_nyt_bestseller(record)


# ── Editions & translations stats (Open Library) ─────────────

def resolve_editions_stats(record: book_data.BookRecord) -> Optional[dict]:
    """
    "N editions · translated into M languages" credibility line. One
    search.json request; by work key when we have it (exact), else
    title+author with the usual strict matching. None unless the numbers
    are actually interesting (≥2 editions or ≥2 languages).
    """
    import httpx
    params = {"fields": "title,author_name,edition_count,language"}
    if record.open_library_work_key:
        params["q"] = f"key:{record.open_library_work_key}"
    else:
        params["title"] = record.title
        params["author"] = record.author or ""
        params["limit"] = 3
    try:
        r = httpx.get("https://openlibrary.org/search.json", params=params,
                      headers=_UA_HEADERS, timeout=8.0)
        if r.status_code != 200:
            return None
        docs = r.json().get("docs") or []
    except Exception as e:
        log.warning(f"OL editions lookup failed for '{record.title}': {e}")
        return None

    want_title = _norm_match(record.title)
    author_last = _norm_match(record.author).split(" ")[-1] if record.author else ""
    for d in docs:
        if not record.open_library_work_key:  # searched by title → verify match
            got = _norm_match(d.get("title", ""))
            if not (want_title == got or want_title in got or got in want_title):
                continue
            if author_last and author_last not in _norm_match(" ".join(d.get("author_name") or [])):
                continue
        editions = int(d.get("edition_count") or 0)
        languages = len(d.get("language") or [])
        if editions >= 2 or languages >= 2:
            return {"editions": editions, "languages": languages}
        return None
    return None


def _cached_editions(record: book_data.BookRecord) -> Optional[dict]:
    key = ("editions_v1", record.title, record.author)
    hit = cache.get(*key)
    if hit is not None:
        return hit or None
    stats = resolve_editions_stats(record)
    cache.set(stats or {}, *key, ttl=None if stats else 3600)
    return stats


# ── More by this author (Open Library, lazy-loaded) ──────────
# Deliberately NOT part of the /summary task pool: the frontend fetches it
# AFTER the summary renders, so it never slows the main response and needs
# no summary-cache version bump.

@router.get("/author/works")
def author_works(name: str, exclude: str = "", exclude_key: str = ""):
    """Up to 8 other works by this author, real catalog data only."""
    import httpx
    name = (name or "").strip()
    if not name or name.lower() in ("unknown", "author"):
        return {"works": []}

    # v2: v1 let multi-contributor anthologies (a work misattributed to
    # every author whose story appears inside it) slip through.
    cache_key = ("author_works_v2", name)
    cached = cache.get(*cache_key)
    if cached is None:
        docs = []
        try:
            r = httpx.get("https://openlibrary.org/search.json", params={
                "author": name,
                # readinglog ≈ popularity — surfaces the author's known works
                # instead of obscure pamphlets.
                "sort": "readinglog",
                "limit": 20,
                "fields": "title,author_name,cover_i,first_publish_year,key,language",
            }, headers=_UA_HEADERS, timeout=8.0)
            if r.status_code == 200:
                docs = r.json().get("docs") or []
        except Exception as e:
            log.warning(f"OL author-works lookup failed for '{name}': {e}")

        author_last = _norm_match(name).split(" ")[-1] if name else ""
        works, seen = [], set()
        for d in docs:
            title = (d.get("title") or "").strip()
            if not title or not d.get("cover_i"):
                continue
            if book_data.is_companion_material(title):
                continue
            # Skip non-English editions (e.g. the Swedish original of a
            # translated hit) — but keep docs with NO language metadata.
            langs = d.get("language") or []
            if langs and "eng" not in langs:
                continue
            authors_list = d.get("author_name") or []
            # Anthologies/textbooks ("The Story and Its Writer") list every
            # contributing author — sometimes 15-45 names — so a plain
            # substring check on the joined list matches them even though
            # the target author only contributed ONE story, not the book.
            # A real (co-)authored work rarely credits more than a few
            # people, so cap it and require the match among those few.
            if len(authors_list) > 4:
                continue
            authors = _norm_match(" ".join(authors_list))
            if author_last and author_last not in authors:
                continue
            base = book_data.get_base_title(title) or _norm_match(title)
            if base in seen:
                continue
            seen.add(base)
            works.append({
                "title": title,
                "author": (d.get("author_name") or [name])[0],
                "cover_url": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg",
                "year": d.get("first_publish_year"),
                "openlibrary_id": d.get("key"),
            })
        cached = works
        cache.set(cached, *cache_key)  # 30d — an author's back catalog is stable

    # Exclude the CURRENT book per-request (not baked into the cached list,
    # so one cached lookup serves every book by this author). Title match
    # catches same-language duplicates; the OL work key catches the same
    # work under a foreign original title ("En man som heter Ove").
    ex = _norm_match(exclude)
    out = []
    for w in cached:
        if exclude_key and w.get("openlibrary_id") == exclude_key:
            continue
        wt = _norm_match(w["title"])
        if ex and (ex in wt or wt in ex):
            continue
        out.append(w)
    return {"works": out[:8]}


# ── Shared assembly (used by /summary and /summary/stream) ──

def _gather_extras(record: book_data.BookRecord) -> dict:
    """
    Everything in the summary payload EXCEPT the Gemini summary text —
    12 independent lookups run concurrently. Shared by the classic route
    (which runs it in parallel with the text generation) and the streaming
    route (which runs it while the text is streaming to the client).
    """
    import concurrent.futures

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
            return _similar_books(record, limit=10)
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

    tasks = {
        "chapters": get_chapters,
        "awards": get_awards,
        "similar": get_similar,
        "amazon_url": get_amazon,
        "fandom_cover": get_fandom_cover,
        "categories": get_categories,
        "ratings": get_ratings,
        "themes_reading_level": get_themes_reading_level,
        "free_ebook": lambda: _cached_free_ebook(record),
        "quotes": lambda: _cached_quotes(record),
        "nyt": lambda: _cached_nyt(record),
        "editions": lambda: _cached_editions(record),
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {name: executor.submit(fn) for name, fn in tasks.items()}
        return {name: f.result() for name, f in futures.items()}


def _assemble_result(record: book_data.BookRecord, depth: str, summary_text: str, extras: dict) -> dict:
    book_slug = slug_mod.book_slug(record.title)
    static_ready, actual_slug = github_publisher.resolve_published(
        book_slug, record.google_volume_id or record.isbn_13 or ""
    )

    # Volume-specific page count from Goodreads when our sources had none
    # (fandom_series volumes deliberately carry page_count=None — the base
    # title's count applied to every volume was wrong).
    ratings = extras["ratings"]
    page_count = record.page_count
    if not page_count and ratings and ratings.get("pages"):
        page_count = ratings["pages"]

    return {
        "found": True,
        "source": record.source,
        "title": record.title,
        "author": record.author,
        "author_slug": slug_mod.author_slug(record.author),
        "depth": depth,
        "summary": summary_text,
        "category": record.primary_category,
        "categories": extras["categories"],
        "slug": actual_slug,
        # True only once the static page was published >5 min ago (rebuild
        # buffer) — gates the frontend's history.replaceState to the clean URL.
        "static_page": static_ready,
        "page_count": page_count,
        "published_year": record.published_year,
        # Prefer the catalog's hand-verified per-series cover when this
        # title resolved to one — see get_fandom_cover() for why.
        "cover_url": extras["fandom_cover"] or record.cover_url,
        "average_rating": record.average_rating,
        "ratings": ratings,
        "isbn_13": record.isbn_13,
        "isbn_10": record.isbn_10,
        "amazon_url": extras["amazon_url"],
        "similar_books": extras["similar"],
        "chapters": extras["chapters"],
        "awards": extras["awards"],
        "themes": extras["themes_reading_level"]["themes"],
        "reading_level": extras["themes_reading_level"]["reading_level"],
        "free_ebook": extras["free_ebook"],
        "quotes": extras["quotes"],
        "nyt": extras["nyt"],
        "editions": extras["editions"],
        "google_volume_id": record.google_volume_id,
        "open_library_work_key": record.open_library_work_key,
    }


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
    # v11: added nyt bestseller history + similar_books grew 4 → 10.
    # v12: added editions/translations stats; language ("en"/"ar") joined
    # the key when Arabic summaries shipped.
    cache_key = ("summary_v12", req.title, req.author, req.depth, req.isbn, req.google_id, req.openlibrary_id, req.bookwyrm_id, req.language)
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
            if (cached.get("free_ebook") is None or cached.get("quotes") is None
                    or cached.get("nyt") is None or cached.get("editions") is None):
                try:
                    tmp = book_data.BookRecord(
                        found=True, title=cached.get("title", ""), author=cached.get("author", ""),
                        open_library_work_key=cached.get("open_library_work_key"),
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
                    # Also heals summaries generated before NYT_API_KEY was
                    # configured; the 24h negative cache in _cached_nyt keeps
                    # this within NYT's 500/day budget.
                    if cached.get("nyt") is None:
                        ny = _cached_nyt(tmp)
                        if ny:
                            cached["nyt"] = ny
                            healed = True
                    if cached.get("editions") is None:
                        ed = _cached_editions(tmp)
                        if ed:
                            cached["editions"] = ed
                            healed = True
                    if healed:
                        cache.set(cached, *cache_key)  # persist the heal
                except Exception as e:
                    log.warning(f"Free-ebook/quotes/nyt self-heal failed for '{cached.get('title')}': {e}")
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

    # Summary text and the 12 extras run in parallel — same total latency
    # as the old single 13-task pool.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as outer:
        future_summary = outer.submit(
            lambda: gemini_client.generate(_build_prompt(record, req.depth, req.language)))
        future_extras = outer.submit(_gather_extras, record)
        summary_text = future_summary.result()
        extras = future_extras.result()

    result = _assemble_result(record, req.depth, summary_text, extras)
    cache.set(result, *cache_key)

    # Publish the static SEO pages in the background — commit failures are
    # logged inside the publisher and never affect this response. English
    # only: Arabic summaries are an on-page toggle, not separate /ar/ pages.
    if github_publisher.is_enabled() and req.language == "en":
        background_tasks.add_task(github_publisher.publish_book, result)
        background_tasks.add_task(github_publisher.publish_author, record.author, record.title)

    return result


@router.post("/summary/stream")
def summary_stream(req: SummaryRequest, background_tasks: BackgroundTasks):
    """
    SSE variant of /summary: streams the Gemini summary text as it
    generates (events {"t": chunk}), then one final {"done": payload}
    with the complete assembled response. Shares the same cache key as
    /summary — a cached book answers with a single done event, and a
    fresh build is cached for both routes. The 12 extras run concurrently
    WHILE the text streams, so total time matches the classic route but
    the reader sees text within seconds.
    """
    import json as _json
    import concurrent.futures
    from fastapi.responses import StreamingResponse

    def sse(obj) -> str:
        return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

    def gen():
        cache_key = ("summary_v12", req.title, req.author, req.depth, req.isbn,
                     req.google_id, req.openlibrary_id, req.bookwyrm_id, req.language)
        cached = cache.get(*cache_key)
        if cached:
            yield sse({"done": cached})
            return

        record = book_data.resolve_book(req.title, req.author, req.isbn,
                                        req.google_id, req.openlibrary_id, req.bookwyrm_id)
        if not record.found:
            payload = {
                "found": False,
                "title": req.title,
                "author": req.author,
                "message": (
                    f"We couldn't verify \"{req.title}\" in our book sources (Google Books "
                    f"or Open Library). Please check the spelling, or try adding the author's name."
                ),
            }
            cache.set(payload, *cache_key, ttl=3600)
            yield sse({"done": payload})
            return

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future_extras = executor.submit(_gather_extras, record)
            parts = []
            try:
                for chunk in gemini_client.generate_stream(_build_prompt(record, req.depth, req.language)):
                    parts.append(chunk)
                    yield sse({"t": chunk})
            except Exception as e:
                # Mid-stream failure: tell the client to fall back to the
                # classic route rather than caching a truncated summary.
                log.warning(f"Summary stream failed for '{record.title}': {e}")
                yield sse({"error": "stream_failed"})
                return

            summary_text = "".join(parts)
            result = _assemble_result(record, req.depth, summary_text, future_extras.result())
            cache.set(result, *cache_key)
            if github_publisher.is_enabled() and req.language == "en":
                background_tasks.add_task(github_publisher.publish_book, result)
                background_tasks.add_task(github_publisher.publish_author, record.author, record.title)
            yield sse({"done": result})
        finally:
            executor.shutdown(wait=False)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell proxies not to buffer the stream
    })


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