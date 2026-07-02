"""
tools/fandom.py — Fandom Wiki Integration Tool

Exposes routes to resolve Fandom wiki subdomains and fetch detailed, grounded universe lore guides
(magic systems, character profiles, factions, etc.) using Gemini and Fandom's MediaWiki API.
"""

import os
import re
import logging
import urllib.parse
import concurrent.futures
from typing import Optional, List
import html as html_lib

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

import cache
import gemini_client

log = logging.getLogger("bookhub-api.tools.fandom")

router = APIRouter(prefix="/fandom")

# ── Response Models ──────────────────────────────────────────

class SubdomainResponse(BaseModel):
    subdomain: Optional[str] = None
    title: str

class CharacterModel(BaseModel):
    name: str
    faction: Optional[str] = None
    description: str

class FactionModel(BaseModel):
    name: str
    description: str

class UniverseResponse(BaseModel):
    found: bool
    subdomain: Optional[str] = None
    title: str
    overview: Optional[str] = None
    magic_system: Optional[str] = None
    key_characters: Optional[List[CharacterModel]] = None
    factions: Optional[List[FactionModel]] = None
    lore_notes: Optional[str] = None

# ── Subdomain Resolver Logic ─────────────────────────────────

def _parse_fandom_subdomain_from_claim(val: str) -> Optional[str]:
    """Extract subdomain from Wikidata P6262 claim value (e.g. 'harrypotter:Harry_Potter')."""
    if not val or ":" not in val:
        return None
    sub = val.split(":", 1)[0]
    if "." in sub:
        parts = sub.split(".")
        # If language prefix is present (e.g., 'ca.harrypotter'), extract main subdomain
        if len(parts[0]) <= 3:
            return parts[-1]
    return sub

def _extract_subdomain_from_url(url: str) -> Optional[str]:
    """Extract fandom subdomain from a full URL."""
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc or parsed.path
    if "fandom.com" in netloc:
        parts = netloc.split(".")
        try:
            fdom_idx = parts.index("fandom")
            if fdom_idx > 0:
                sub = parts[fdom_idx - 1]
                if sub not in ("www", "community", "dev", "c", "support"):
                    return sub
        except ValueError:
            pass
    return None

def _get_fandom_from_wikidata(qid: str) -> Optional[str]:
    """Retrieve Fandom subdomain from Wikidata entity claims (P6262)."""
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    try:
        r = httpx.get(url, headers=headers, timeout=5.0)
        if r.status_code == 200:
            claims = r.json().get("entities", {}).get(qid, {}).get("claims", {})
            fandom_article_claims = claims.get("P6262", [])
            for a in fandom_article_claims:
                val = a.get("mainsnak", {}).get("datavalue", {}).get("value")
                sub = _parse_fandom_subdomain_from_claim(val)
                if sub:
                    return sub
    except Exception as e:
        log.warning(f"Wikidata P6262 fetch failed for {qid}: {e}")
    return None

def _search_wikidata_qid_by_title(title: str) -> Optional[str]:
    """Search Wikidata by book title and return first matching QID."""
    url = "https://www.wikidata.org/w/api.php"
    headers = {
        "User-Agent": "BookHubApp/1.0 (https://github.com/mokhhtar; mokhhtar@gmail.com) httpx/0.24",
        "Accept": "application/json"
    }
    params = {
        "action": "wbsearchentities",
        "search": title,
        "language": "en",
        "format": "json",
        "limit": 5
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=5.0)
        if r.status_code == 200:
            search_results = r.json().get("search", [])
            book_keywords = {"novel", "book", "play", "story", "literary", "writing", "work", "poem", "biography", "memoir", "fictional"}
            for res in search_results:
                desc = res.get("description", "").lower()
                if any(kw in desc for kw in book_keywords):
                    return res.get("id")
            if search_results:
                return search_results[0].get("id")
    except Exception as e:
        log.warning(f"Wikidata QID search by title failed: {e}")
    return None

def _get_fandom_from_google_cse(title: str, api_key: str, cx_id: str) -> Optional[str]:
    """Query Google Custom Search API to resolve fandom subdomain (site:fandom.com)."""
    url = "https://www.googleapis.com/customsearch/v1"
    query = f'site:fandom.com "{title}"'
    params = {
        "key": api_key,
        "cx": cx_id,
        "q": query,
        "num": 3
    }
    try:
        r = httpx.get(url, params=params, timeout=5.0)
        if r.status_code == 200:
            items = r.json().get("items", [])
            for item in items:
                link = item.get("link", "")
                subdomain = _extract_subdomain_from_url(link)
                if subdomain:
                    return subdomain
    except Exception as e:
        log.warning(f"Google CSE query failed: {e}")
    return None

def _get_fandom_from_ddg(title: str) -> Optional[str]:
    """Fallback search using DuckDuckGo HTML page parsing."""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    data = {
        "q": f"site:fandom.com {title}"
    }
    try:
        r = httpx.post(url, data=data, headers=headers, timeout=5.0)
        if r.status_code == 200:
            urls = re.findall(r'href="https://([^.]+)\.fandom\.com/wiki/', r.text)
            for sub in urls:
                if sub not in ("www", "community", "dev", "c", "support"):
                    return sub
    except Exception as e:
        log.warning(f"DuckDuckGo search failed: {e}")
    return None

def _ping_fandom_subdomain(subdomain: str) -> bool:
    """Verify if Fandom subdomain exists and responds correctly."""
    url = f"https://{subdomain}.fandom.com/api.php"
    params = {"action": "query", "meta": "siteinfo", "format": "json"}
    try:
        r = httpx.get(url, params=params, timeout=3.0)
        return r.status_code == 200 and "query" in r.json()
    except Exception:
        return False

def get_series_title_candidates(title: str) -> list[str]:
    candidates = [title.strip(":,.- ")]
    cleaned = title
    cleaned = re.sub(r'\s*,\s*(vol\.|volume|vol|part|pt\.|book|bk\.)\s*\d+\b.*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+\b(vol\.|volume|vol|part|pt\.|book|bk\.)\s*\d+\b.*', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(":,.- ")
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)
        
    for sep in (":", ","):
        if sep in title:
            first_part = title.split(sep)[0].strip()
            first_part_clean = re.sub(r'\s+\b(vol\.|volume|vol|part|pt\.|book|bk\.)\s*\d+\b.*', '', first_part, flags=re.IGNORECASE)
            first_part_clean = first_part_clean.strip(":,.- ")
            if first_part_clean and first_part_clean not in candidates:
                candidates.append(first_part_clean)
    return candidates

def _resolve_fandom_subdomain_single(title: str, wikidata_id: Optional[str] = None) -> Optional[str]:
    """
    Highly robust 5-tier subdomain resolver cascade for a single title string.
    """
    # Tier 1: QID provided
    if wikidata_id:
        sub = _get_fandom_from_wikidata(wikidata_id)
        if sub:
            return sub
            
    # Tier 2: Search QID by title
    qid = _search_wikidata_qid_by_title(title)
    if qid:
        sub = _get_fandom_from_wikidata(qid)
        if sub:
            return sub
            
    # Tier 3: Google Custom Search
    api_key = os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY")
    cx_id = os.environ.get("GOOGLE_SEARCH_CX_ID")
    if api_key and cx_id:
        sub = _get_fandom_from_google_cse(title, api_key, cx_id)
        if sub:
            return sub
            
    # Tier 4: DuckDuckGo HTML Search
    sub = _get_fandom_from_ddg(title)
    if sub:
        return sub
        
    # Tier 5: Title Normalization Ping
    normalized = "".join(c.lower() for c in title if c.isalnum())
    if normalized and _ping_fandom_subdomain(normalized):
        return normalized
        
    return None
def resolve_fandom_subdomain(title: str, wikidata_id: Optional[str] = None) -> Optional[str]:
    """
    Resolves Fandom subdomain by trying the full title and various normalized series name candidates.
    """
    candidates = get_series_title_candidates(title)
    for cand in candidates:
        sub = _resolve_fandom_subdomain_single(cand, wikidata_id)
        if sub:
            return sub
    return None

def fetch_volumes_from_fandom(subdomain: str, book_title: str) -> list[str]:
    """
    Queries Fandom search for page titles containing 'Volume', fetches their wikitext content,
    and returns volume titles that belong to the queried book/series.
    """
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    # 1. Search for pages matching 'Volume'
    params = {
        "action": "query",
        "list": "search",
        "srsearch": "Volume",
        "format": "json",
        "srlimit": 40
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=5.0)
        if r.status_code != 200:
            return []
        search_results = r.json().get("query", {}).get("search", [])
    except Exception:
        return []
        
    volume_pages = []
    for res in search_results:
        title = res.get("title", "")
        if "/" not in title and re.match(r'^Volume\s+\d+[:\s]', title, flags=re.IGNORECASE):
            volume_pages.append(title)
            
    if not volume_pages:
        return []
        
    # 2. Batch fetch page contents to verify they belong to the series
    verified_volumes = []
    params_content = {
        "action": "query",
        "titles": "|".join(volume_pages),
        "prop": "revisions",
        "rvprop": "content",
        "format": "json"
    }
    try:
        r = httpx.get(url, params=params_content, headers=headers, timeout=5.0)
        if r.status_code == 200:
            pages_data = r.json().get("query", {}).get("pages", {}).values()
            for p_info in pages_data:
                title = p_info.get("title", "")
                wikitext = p_info.get("revisions", [{}])[0].get("*", "")
                
                clean_q = re.sub(r'[^a-z0-9]', '', book_title.lower())
                clean_wiki = re.sub(r'[^a-z0-9]', '', wikitext.lower()) if wikitext else ""
                
                # Exclude sequel volumes (Circle of Inevitability) if not searching for COI
                is_coi_query = "circle" in clean_q or "inevitability" in clean_q
                has_coi_in_wiki = "circleofinevitability" in clean_wiki
                if has_coi_in_wiki and not is_coi_query:
                    continue
                    
                if clean_q in clean_wiki:
                    verified_volumes.append(title)
    except Exception:
        pass
        
    if not verified_volumes:
        verified_volumes = volume_pages
        
    # Deduplicate and sort numerically by volume number
    verified_volumes = list(set(verified_volumes))
    def get_vol_num(v):
        m = re.search(r'^Volume\s+(\d+)', v, flags=re.IGNORECASE)
        return int(m.group(1)) if m else 999
        
    verified_volumes.sort(key=get_vol_num)
    return verified_volumes



# ── Content Scraping & Cleaning ─────────────────────────────

def clean_wiki_html(html: str) -> str:
    """Strips HTML tags, styles, scripts, brackets, references, and normalizes space."""
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = html_lib.unescape(text)
    # Remove reference tags like [1]
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'&\#91;\d+&\#93;', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def fetch_wiki_category_content(subdomain: str, category_query: str) -> str:
    """Searches a wiki for a category/topic and parses the content of the first page match."""
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": category_query,
        "format": "json",
        "srlimit": 1
    }
    try:
        r = httpx.get(url, params=search_params, headers=headers, timeout=5.0)
        if r.status_code == 200:
            results = r.json().get("query", {}).get("search", [])
            if results:
                page_title = results[0].get("title")
                parse_params = {
                    "action": "parse",
                    "page": page_title,
                    "prop": "text",
                    "format": "json",
                    "disablelimitreport": "1",
                    "disableeditsection": "1"
                }
                r_parse = httpx.get(url, params=parse_params, headers=headers, timeout=5.0)
                if r_parse.status_code == 200:
                    html = r_parse.json().get("parse", {}).get("text", {}).get("*", "")
                    text = clean_wiki_html(html)
                    return f"=== Page: {page_title} ===\n{text[:3000]}"
    except Exception as e:
        log.warning(f"Failed fetching category '{category_query}' from wiki '{subdomain}': {e}")
    return ""

def extract_chapters_from_fandom(subdomain: str, book_title: str) -> list[str]:
    """
    Scrapes a series' Fandom wiki to find the correct, official chapter names for a book.
    Parses tables with headers like 'Chapter title' or 'Chapter Name' or list items.
    """
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    def clean_name(name):
        return re.sub(r'\s+', ' ', name).strip().lower()

    # 1. Search for matching pages
    search_queries = [
        f"List of chapters in the {book_title}",
        f"{book_title} chapters",
        "List of chapters",
        "Chapters",
        book_title,
        f"{book_title} Volume 1",
        "Volume 1"
    ]
    
    page_titles = []
    for q in search_queries:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "format": "json",
            "srlimit": 3
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=3.0)
            if r.status_code == 200:
                search_results = r.json().get("query", {}).get("search", [])
                for res in search_results:
                    t = res.get("title")
                    if t not in page_titles:
                        page_titles.append(t)
        except Exception:
            pass
            
    # Sort page_titles to prioritize main lists and volume 1, penalizing subpages (e.g. /Author's Note)
    def page_priority(t):
        t_low = t.lower()
        title_low = book_title.lower()
        penalty = 10 if "/" in t else 0
        if "list of chapters" in t_low and title_low in t_low:
            return 0 + penalty
        if "volume 1" in t_low or "vol. 1" in t_low or "vol 1" in t_low:
            return 1 + penalty
        if "list of chapters" in t_low or "chapter list" in t_low:
            return 2 + penalty
        if title_low in t_low:
            return 3 + penalty
        return 4 + penalty

    page_titles.sort(key=page_priority)
    
    parsed_pages = []
    
    # Phase 1: Try to extract chapters from tables
    for page_title in page_titles[:5]:
        params = {
            "action": "parse",
            "page": page_title,
            "prop": "text",
            "format": "json"
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=4.0)
            if r.status_code != 200:
                continue
            html = r.json().get("parse", {}).get("text", {}).get("*", "")
            soup = BeautifulSoup(html, 'html.parser')
            parsed_pages.append((page_title, soup))
            
            headlines = soup.find_all(class_="mw-headline")
            target_headline = None
            book_title_clean = clean_name(book_title)
            
            for hl in headlines:
                hl_text = clean_name(hl.get_text())
                if book_title_clean in hl_text or hl_text in book_title_clean or "list of chapters" in hl_text or "chapters" in hl_text:
                    target_headline = hl
                    break
            
            tables = []
            if target_headline:
                current = target_headline.parent
                for sibling in current.next_siblings:
                    if sibling.name in ("h2", "h3"):
                        break
                    if sibling.name == "table":
                        tables.append(sibling)
            else:
                tables = soup.find_all("table")
                
            for table in tables:
                headers_list = [clean_name(th.get_text()) for th in table.find_all("th")]
                name_col_idx = -1
                for idx, h in enumerate(headers_list):
                    # Check for title or name, but NOT just "chapter" to avoid "chapter#" number columns
                    if "title" in h or "name" in h:
                        name_col_idx = idx
                        break
                
                # If no explicit header, guess by column count
                if name_col_idx == -1 and len(headers_list) >= 2:
                    if len(headers_list) == 3:
                        name_col_idx = 1
                    elif len(headers_list) == 4:
                        name_col_idx = 2
                        
                if name_col_idx != -1:
                    chapters = []
                    rows = table.find_all("tr")
                    for tr in rows[1:]:
                        cells = tr.find_all("td")
                        if len(cells) > name_col_idx:
                            cell_text = cells[name_col_idx].get_text().strip()
                            cell_text = re.sub(r'["\']', '', cell_text)
                            cell_text = re.sub(r'\s+', ' ', cell_text)
                            if cell_text and not cell_text.isdigit() and len(cell_text) < 100:
                                chapters.append(cell_text)
                                
                    if len(chapters) >= 3:
                        return chapters
        except Exception as e:
            log.warning(f"Failed parsing table chapter list on '{page_title}': {e}")
            
    # Phase 2: Fallback to list items (li) if no tables succeeded
    for page_title, soup in parsed_pages:
        if "chapters" in clean_name(page_title) or "list" in clean_name(page_title) or "volume" in clean_name(page_title):
            chapters = []
            for li in soup.find_all("li"):
                txt = li.get_text().strip()
                if "chapter" in txt.lower() or re.match(r'^\d+\.', txt) or (len(txt) < 80 and not txt.startswith("Category:")):
                    clean_txt = re.sub(r'^(chapter\s+\d+|ch\.\s+\d+|\d+)\s*[:.-]\s*', '', txt, flags=re.IGNORECASE)
                    clean_txt = clean_txt.strip()
                    if clean_txt and len(clean_txt) < 80:
                        chapters.append(clean_txt)
            if len(chapters) >= 5:
                return chapters[:100]
                
    return []


# ── Prompts ──────────────────────────────────────────────────

def _build_fandom_prompt(title: str, wiki_data: str) -> str:
    return f"""You are an expert on literary lore, fantasy worldbuilding, and wiki analysis.
Your job is to synthesize a structured, comprehensive Guide to the Universe of "{title}" using the provided Fandom wiki pages as your grounding source.

=== Grounding Wiki Content ===
{wiki_data}
=============================

Instructions:
1. Rely strictly on the Grounding Wiki Content provided above. Do not invent lore, names, magic rules, or character details not mentioned in the source.
2. If the grounding content is sparse or missing details for a section, write a brief, accurate summary of what is known from the source, and do not embellish.
3. Your output MUST be a valid JSON object matching the schema below. Do not wrap the JSON in Markdown fences, or if you do, ensure it is clean JSON.

JSON Schema:
{{
  "overview": "A rich description of the setting, world history, tone, and main premise of the work.",
  "magic_system": "A detailed explanation of the rules of magic, supernatural powers, abilities, pathways, or spells in this universe.",
  "key_characters": [
    {{
      "name": "Character Name",
      "faction": "Their faction, house, organization, or family affiliation",
      "description": "Their role in the story, abilities, and notable traits."
    }}
  ],
  "factions": [
    {{
      "name": "Faction or Organization Name",
      "description": "Their goals, role in the world, and members."
    }}
  ],
  "lore_notes": "A collection of interesting bullet points, key rules, history milestones, or conceptual guidelines governing this world."
}}
"""

# ── Routes ───────────────────────────────────────────────────

@router.get("/resolve", response_model=SubdomainResponse)
def resolve_fandom(title: str = Query(..., min_length=1), wikidata_id: Optional[str] = None):
    """
    Endpoint to resolve a book's Fandom subdomain.
    Caches the results to minimize external network requests.
    """
    cache_key = ("fandom_resolve_v1", title, wikidata_id or "")
    cached = cache.get(*cache_key)
    if cached:
        return cached
        
    subdomain = resolve_fandom_subdomain(title, wikidata_id)
    result = {"subdomain": subdomain, "title": title}
    cache.set(result, *cache_key)
    return result

@router.get("/universe", response_model=UniverseResponse)
def get_universe(title: str = Query(..., min_length=1), subdomain: Optional[str] = None):
    """
    Endpoint to generate a structured, grounded universe guide for a work of fiction.
    Queries the Fandom wiki, pulls character list, factions, magic systems, and uses Gemini to synthesize the guide.
    """
    cache_key = ("fandom_universe_v1", title, subdomain or "")
    cached = cache.get(*cache_key)
    if cached:
        return cached

    # 1. Resolve subdomain if missing
    resolved_sub = subdomain or resolve_fandom_subdomain(title)
    if not resolved_sub:
        return {
            "found": False,
            "subdomain": None,
            "title": title,
            "overview": None,
            "magic_system": None,
            "key_characters": None,
            "factions": None,
            "lore_notes": f"We couldn't resolve a Fandom subdomain for '{title}'."
        }

    # 2. Fetch grounding articles in parallel
    search_targets = [
        title,  # Main Overview Page
        "Magic System",  # Magic/Occult/Power Rules
        "Characters",  # List of Characters
        "Factions"  # Factions / Organizations
    ]
    
    wiki_texts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_wiki_category_content, resolved_sub, target): target for target in search_targets}
        for future in concurrent.futures.as_completed(futures):
            res_text = future.result()
            if res_text:
                wiki_texts.append(res_text)

    wiki_combined = "\n\n".join(wiki_texts).strip()

    if not wiki_combined:
        return {
            "found": False,
            "subdomain": resolved_sub,
            "title": title,
            "overview": None,
            "magic_system": None,
            "key_characters": None,
            "factions": None,
            "lore_notes": f"Resolved wiki subdomain '{resolved_sub}', but no content could be retrieved from Fandom API."
        }

    # 3. Call Gemini to synthesize
    prompt = _build_fandom_prompt(title, wiki_combined)
    try:
        raw_ai = gemini_client.generate(prompt)
        ai_data = gemini_client.parse_json_response(raw_ai)
        
        result = {
            "found": True,
            "subdomain": resolved_sub,
            "title": title,
            "overview": ai_data.get("overview"),
            "magic_system": ai_data.get("magic_system"),
            "key_characters": ai_data.get("key_characters"),
            "factions": ai_data.get("factions"),
            "lore_notes": ai_data.get("lore_notes")
        }
    except Exception as e:
        log.error(f"Fandom Gemini synthesis failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to synthesize lore guide: {str(e)}")

    cache.set(result, *cache_key)
    return result
