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
FANDOM_WIKIS = {
    "tbate": {
        "subdomain": "tbate",
        "aliases": ["beginning after the end", "the beginning after the end", "tbate"],
        "author": "TurtleMe",
        "cover_url": "https://covers.openlibrary.org/b/id/14815307-M.jpg"
    },
    "lordofthemysteries": {
        "subdomain": "lordofthemysteries",
        "aliases": ["lord of the mysteries", "circle of inevitability", "coi"],
        "author": "Cuttlefish That Loves Diving",
        "cover_url": "https://static.wikia.nocookie.net/lord-of-the-mystery/images/c/cd/LOM_Manhua_cover.png/revision/latest?cb=20200113124228"
    },
    "shadowslave": {
        "subdomain": "shadowslave",
        "aliases": ["shadow slave"],
        "author": "Guiltythree",
        "cover_url": "https://covers.openlibrary.org/b/id/15173101-M.jpg"
    },
    "mother-of-learning": {
        "subdomain": "mother-of-learning",
        "aliases": ["mother of learning"],
        "author": "nobody103 (Domagoj Kurmaic)",
        "cover_url": "https://covers.openlibrary.org/b/id/12836262-M.jpg"
    },
    "reverend-insanity": {
        "subdomain": "reverend-insanity",
        "aliases": ["reverend insanity"],
        "author": "Gu Zhen Ren",
        "cover_url": "https://static.wikia.nocookie.net/reverend-insanity/images/2/23/Fang_Yuan_2.png/revision/latest?cb=20260630200735"
    },
    "you-zitsu": {
        "subdomain": "you-zitsu",
        "aliases": ["classroom of the elite"],
        "author": "Shōgo Kinugasa",
        "cover_url": "https://covers.openlibrary.org/b/id/10166148-M.jpg"
    },
    "omniscient-readers-point-of-view": {
        "subdomain": "omniscient-readers-point-of-view",
        "aliases": ["omniscient reader", "omniscient reader's viewpoint", "orvp"],
        "author": "sing N song",
        "cover_url": "https://covers.openlibrary.org/b/id/14321241-M.jpg"
    },
    "solo-leveling": {
        "subdomain": "solo-leveling",
        "aliases": ["solo leveling"],
        "author": "Chugong",
        "cover_url": "https://covers.openlibrary.org/b/id/10582298-M.jpg"
    }
}

# Dynamically populate for backward compatibility
FANDOM_STATIC_MAP = {}
for k, cfg in FANDOM_WIKIS.items():
    for alias in cfg.get("aliases", []):
        FANDOM_STATIC_MAP[alias] = cfg["subdomain"]
    FANDOM_STATIC_MAP[k] = cfg["subdomain"]

FANDOM_SERIES_DETAILS = {
    cfg["subdomain"]: {
        "author": cfg.get("author"),
        "cover_url": cfg.get("cover_url")
    }
    for cfg in FANDOM_WIKIS.values()
}

def resolve_fandom_subdomain(title: str, wikidata_id: Optional[str] = None) -> Optional[str]:
    """
    Resolves Fandom subdomain by trying a static map first, and then various candidates.
    """
    candidates = get_series_title_candidates(title)
    
    # Tier 0: Static mapping lookup (fast and 100% reliable)
    for cand in candidates:
        cand_clean = re.sub(r'\s+', ' ', cand.lower()).strip()
        for subdomain_key, config in FANDOM_WIKIS.items():
            if cand_clean == subdomain_key or cand_clean in config.get("aliases", []):
                return config["subdomain"]
            
    for cand in candidates:
        sub = _resolve_fandom_subdomain_single(cand, wikidata_id)
        if sub:
            return sub
    return None

def extract_fandom_infobox_metadata(subdomain: str, novel_title: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extracts author and cover image URL from a Fandom wiki.
    First checks if the subdomain has configured details in FANDOM_WIKIS.
    If not, queries the MediaWiki API to find the main novel page, and scrapes the infobox.
    """
    # 1. Check configuration first (fastest and most reliable)
    for k, cfg in FANDOM_WIKIS.items():
        if cfg["subdomain"] == subdomain:
            return cfg.get("author"), cfg.get("cover_url")

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    # 2. Query search API to find target page
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": f'"{novel_title}" novel',
        "format": "json",
        "srlimit": 5
    }
    
    # 2. Get mainpage from siteinfo as a fallback/primary target
    main_page = None
    try:
        r = httpx.get(url, params={"action": "query", "meta": "siteinfo", "siprop": "general", "format": "json"}, headers=headers, timeout=3.0)
        if r.status_code == 200:
            main_page = r.json().get("query", {}).get("general", {}).get("mainpage")
    except Exception:
        pass

    # 3. Query search API to find target page
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": f'"{novel_title}" novel',
        "format": "json",
        "srlimit": 5
    }
    
    target_pages = []
    try:
        r = httpx.get(url, params=search_params, headers=headers, timeout=5.0)
        if r.status_code == 200:
            results = r.json().get("query", {}).get("search", [])
            for res in results:
                t = res.get("title", "")
                t_low = t.lower()
                if "(novel)" in t_low or "(light novel)" in t_low or "(web novel)" in t_low:
                    target_pages.append(t)
            if not target_pages and results:
                # Check if any page title is very close to novel_title
                for res in results:
                    t = res.get("title", "")
                    if re.sub(r'[^a-z0-9]', '', t.lower()) == re.sub(r'[^a-z0-9]', '', novel_title.lower()):
                        target_pages.append(t)
                        break
                if not target_pages:
                    target_pages.append(results[0].get("title"))
    except Exception as e:
        log.warning(f"Fandom search failed for {novel_title} in subdomain {subdomain}: {e}")
        
    if main_page and main_page not in target_pages:
        target_pages.append(main_page)
    if novel_title not in target_pages:
        target_pages.append(novel_title)

    author = None
    cover_url = None
    
    # Try parsing target pages in order of priority to extract author and cover
    for page_name in target_pages:
        if not page_name:
            continue
        parse_params = {
            "action": "parse",
            "page": page_name,
            "prop": "text",
            "format": "json"
        }
        try:
            r = httpx.get(url, params=parse_params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                html = r.json().get("parse", {}).get("text", {}).get("*", "")
                soup = BeautifulSoup(html, 'html.parser')
                
                # A. Look for Portable Infobox
                infobox = soup.find(class_=lambda x: x and ("infobox" in x or "portable-infobox" in x))
                if infobox:
                    # Extract author
                    if not author:
                        author_keys = ["author", "writer", "novelist", "creator", "original_writer", "original writer", "written_by", "written by"]
                        for key in author_keys:
                            author_item = infobox.find(class_=lambda x: x and "pi-data" in x, attrs={"data-source": key})
                            if author_item:
                                val_div = author_item.find(class_="pi-data-value")
                                if val_div:
                                    author = val_div.get_text().strip()
                                    author = re.sub(r'\s+', ' ', author)
                                    break
                    
                    # Extract image
                    if not cover_url:
                        img_container = infobox.find(class_=lambda x: x and "pi-image" in x)
                        img = None
                        if img_container:
                            img = img_container.find("img")
                        if not img:
                            img = infobox.find("img")
                        if img:
                            cover_url = img.get("data-src") or img.get("src")
                            if cover_url and cover_url.startswith("data:"):
                                cover_url = img.get("data-src") or img.get("src") # re-extract if lazyloaded
                                
                # B. Fallback to standard tables
                if not author:
                    for table in soup.find_all("table"):
                        headers_list = table.find_all(["th", "td"])
                        for cell in headers_list:
                            cell_txt = cell.get_text().strip().lower()
                            if cell_txt in ["author", "author(s)", "novelist", "writer", "written by"]:
                                sibling = cell.find_next_sibling(["td", "th"])
                                if sibling:
                                    author = sibling.get_text().strip()
                                    break
                        if author:
                            break
                            
                # C. Extract cover from any decent image if still not found
                if not cover_url:
                    for img in soup.find_all("img"):
                        src = img.get("data-src") or img.get("src")
                        if src and not src.startswith("data:"):
                            src_low = src.lower()
                            if any(term in src_low for term in ["logo", "icon", "warning", "stub", "edit", "button", "social", "facebook", "twitter", "discord"]):
                                continue
                            cover_url = src
                            break

                # D. Plain text search for author (e.g. "written by Zogarth")
                if not author:
                    page_text = soup.get_text()
                    m_auth = re.search(r'\b(?:written by|novel by|authored by)\s+([A-Z][a-zA-Z0-9_-]{1,30})\b', page_text, flags=re.IGNORECASE)
                    if m_auth:
                        author = m_auth.group(1).strip()
                        
                # If we have both, we can stop!
                if author and cover_url:
                    break
        except Exception as e:
            log.warning(f"Fandom parse/scraping failed for page {page_name} in subdomain {subdomain}: {e}")
            
    # Post-process cover URL
    if cover_url:
        if "/revision/latest" in cover_url:
            cover_url = cover_url.split("/revision/latest")[0] + "/revision/latest"
        if "?" in cover_url:
            cover_url = cover_url.split("?")[0]
            
    return author, cover_url


def fetch_volumes_from_fandom(subdomain: str, book_title: str) -> list[str]:
    """
    Queries Fandom for all volumes of a book/series.
    Cataloged series (see fandom_catalog.py) use structured configs first;
    all others fall back to legacy heuristic scraping below.
    """
    try:
        from tools.fandom_catalog import fetch_volumes_for_search
        catalog_volumes = fetch_volumes_for_search(subdomain, book_title)
        if catalog_volumes:
            return [v.wiki_page for v in catalog_volumes]
    except Exception as e:
        log.warning(f"Fandom catalog volume fetch failed for '{book_title}': {e}")

    # ── Legacy heuristic path (non-catalog series) ────────────
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    verified_volumes = []
    
    # 1. Prioritize querying a master page (e.g. List of Volumes, Volumes and Chapters)
    master_titles = ["List of Volumes", "Volumes and Chapters", "Volumes & Chapters", "List of Light Novels", "Volumes"]
    for m_title in master_titles:
        params_master = {
            "action": "query",
            "titles": m_title,
            "prop": "revisions",
            "rvprop": "content",
            "format": "json"
        }
        try:
            r = httpx.get(url, params=params_master, headers=headers, timeout=5.0)
            if r.status_code == 200:
                pages_data = r.json().get("query", {}).get("pages", {}).values()
                for p_info in pages_data:
                    if "missing" in p_info:
                        continue
                    wikitext = p_info.get("revisions", [{}])[0].get("*", "")
                    if wikitext:
                        # Method A: Extract volume headers
                        vols = re.findall(r'==+\s*(Volume\s+\d+.*?)\s*==+', wikitext, flags=re.IGNORECASE)
                        vols_clean = [v.strip() for v in vols if v.strip()]
                        
                        # Method B: Extract wikilinks containing 'volume' or 'vol'
                        if len(vols_clean) < 2:
                            links = re.findall(r'\[\[([^\]|]*?\b(?:volume|vol)\b[^\]|]*?)(?:\|[^\]]*)?\]\]', wikitext, flags=re.IGNORECASE)
                            for l in links:
                                t = l.strip()
                                if not t or "/" in t:
                                    continue
                                if any(t.lower().startswith(p) for p in ["category:", "file:", "image:", "template:", "media:"]):
                                    continue
                                if t not in vols_clean:
                                    vols_clean.append(t)
                                        
                        if len(vols_clean) >= 2:
                            verified_volumes = vols_clean
                            break
                if verified_volumes:
                    break
        except Exception:
            pass

    # 2. Fallback to scanning all pages starting with 'Volume'
    if not verified_volumes:
        params = {
            "action": "query",
            "list": "allpages",
            "apprefix": "Volume",
            "format": "json",
            "aplimit": 100
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                search_results = r.json().get("query", {}).get("allpages", [])
                volume_pages = []
                for res in search_results:
                    title = res.get("title", "")
                    if "/" not in title and re.match(r'^Volume\s+\d+(?:\.\d+)?\b', title, flags=re.IGNORECASE):
                        volume_pages.append(title)
                
                if volume_pages:
                    # Batch fetch page contents to verify they belong to the series
                    params_content = {
                        "action": "query",
                        "titles": "|".join(volume_pages),
                        "prop": "revisions",
                        "rvprop": "content",
                        "format": "json"
                    }
                    r_c = httpx.get(url, params=params_content, headers=headers, timeout=5.0)
                    if r_c.status_code == 200:
                        pages_data = r_c.json().get("query", {}).get("pages", {}).values()
                        for p_info in pages_data:
                            title = p_info.get("title", "")
                            wikitext = p_info.get("revisions", [{}])[0].get("*", "")
                            
                            clean_q = re.sub(r'[^a-z0-9]', '', book_title.lower())
                            clean_wiki = re.sub(r'[^a-z0-9]', '', wikitext.lower()) if wikitext else ""
                            
                            is_coi_query = "circle" in clean_q or "inevitability" in clean_q
                            has_coi_in_wiki = "circleofinevitability" in clean_wiki
                            if has_coi_in_wiki and not is_coi_query:
                                continue
                                
                            if clean_q in clean_wiki:
                                verified_volumes.append(title)
                    if not verified_volumes:
                        verified_volumes = volume_pages
        except Exception:
            pass

    # Deduplicate and sort numerically by volume number
    verified_volumes = list(set(verified_volumes))
    def get_vol_num(v):
        # 1. Extract major segment: Year/Arc/Season/Part/Act number
        major_val = 0.0
        # Try pattern like: "2nd Year", "3rd Arc", "1st Season"
        m_major1 = re.search(r'\b(\d+)(?:st|nd|rd|th)?\s+(year|arc|season|part|act)\b', v, flags=re.IGNORECASE)
        if m_major1:
            num = int(m_major1.group(1))
            major_val = (num - 1) * 100.0
        else:
            # Try pattern like: "Year 2", "Arc 3", "Season 1"
            m_major2 = re.search(r'\b(year|arc|season|part|act)\s+(\d+)\b', v, flags=re.IGNORECASE)
            if m_major2:
                num = int(m_major2.group(2))
                major_val = (num - 1) * 100.0

        # 2. Extract minor segment: Volume/Vol/Book/Chapter number
        m_minor = re.search(r'\b(?:volume|vol|v|book)\.?\s*(\d+(?:\.\d+)?)\b', v, flags=re.IGNORECASE)
        if m_minor:
            minor_val = float(m_minor.group(1))
        else:
            # Fallback to the first stand-alone number in the title
            m_num = re.search(r'\b(\d+(?:\.\d+)?)\b', v)
            minor_val = float(m_num.group(1)) if m_num else 999.0
            
        return major_val + minor_val
        
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
    Cataloged series use fandom_catalog.py; others use legacy heuristics below.
    """
    try:
        from tools.fandom_catalog import fetch_chapters_for_title
        catalog_chapters = fetch_chapters_for_title(subdomain, book_title)
        if catalog_chapters:
            return catalog_chapters
    except Exception as e:
        log.warning(f"Fandom catalog chapter fetch failed for '{book_title}': {e}")

    # ── Legacy heuristic path (non-catalog series) ────────────
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    def clean_name(name):
        return re.sub(r'\s+', ' ', name).strip().lower()
        
    req_vol = None
    m = re.search(r'\b(vol\.|volume|vol|part|pt\.|book|bk\.)\s*(\d+(?:\.\d+)?)\b', book_title, flags=re.IGNORECASE)
    if m:
        req_vol = m.group(2)

    # 1. Search for matching pages
    if req_vol:
        search_queries = [
            f"{book_title} chapters",
            book_title,
            f"Volume {req_vol}",
            f"Vol. {req_vol}",
            f"Vol {req_vol}",
            "Volumes and Chapters",
            "Volumes & Chapters",
            "List of Volumes",
            "Volumes"
        ]
    else:
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
            
    # Filter page titles to exclude sequel volume pages if searching for the main series
    clean_book_title = book_title.lower()
    is_coi_query = "circle" in clean_book_title or "inevitability" in clean_book_title
    filtered_page_titles = []
    for t in page_titles:
        t_low = t.lower()
        if ("circle" in t_low or "inevitability" in t_low or "eternal aeon" in t_low) and not is_coi_query:
            continue
        filtered_page_titles.append(t)
    page_titles = filtered_page_titles

    # Sort page_titles to prioritize main lists and volume 1, penalizing subpages (e.g. /Author's Note)
    def page_priority(t):
        t_low = t.lower()
        title_low = book_title.lower()
        penalty = 10 if "/" in t else 0
        
        # If we are looking for a specific volume, boost pages matching "Volume X" or "Vol X"
        if req_vol:
            vol_pat = rf'\b(volume|vol|bk|book)\s*{req_vol}\b'
            if re.search(vol_pat, t_low):
                return 0 + penalty
                
        if any(m in t_low for m in ["volumes and chapters", "volumes & chapters", "list of volumes"]):
            return 1 + penalty
        if "list of chapters" in t_low and title_low in t_low:
            return 1 + penalty
        if "volume 1" in t_low or "vol. 1" in t_low or "vol 1" in t_low:
            return 2 + penalty
        if "list of chapters" in t_low or "chapter list" in t_low:
            return 3 + penalty
        if title_low in t_low:
            return 4 + penalty
        return 5 + penalty

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
            
            # --- Check if this is a master page listing all volumes ---
            page_title_clean = page_title.lower()
            is_master_page = any(m in page_title_clean for m in ["volumes and chapters", "volumes & chapters", "list of volumes"]) or page_title_clean == "volumes"
            
            if is_master_page and req_vol:
                # Extract chapters for this specific volume section only
                headlines = soup.find_all(class_="mw-headline")
                target_headline = None
                for hl in headlines:
                    hl_text = hl.get_text().strip()
                    # Match "Volume X" or "Volume X: ..." where X = req_vol
                    m_hl = re.search(r'\bVolume\s+(' + re.escape(str(req_vol)) + r')\b', hl_text, flags=re.IGNORECASE)
                    if m_hl:
                        target_headline = hl
                        break
                        
                if target_headline:
                    chapters = []
                    current = target_headline.parent
                    for sibling in current.next_siblings:
                        if sibling.name in ("h2", "h3", "h4"):
                            sib_text = sibling.get_text().strip()
                            if re.search(r'\bVolume\s+\d+', sib_text, flags=re.IGNORECASE):
                                break
                        
                        if sibling.name == "ul":
                            for li in sibling.find_all("li"):
                                txt = li.get_text().strip()
                                if txt:
                                    txt_clean = re.sub(r'^\d+[\s.:.-]+', '', txt).strip()
                                    txt_clean = re.sub(r'\s+', ' ', txt_clean)
                                    clean_txt = re.sub(r'^(chapter\s+\d+|ch\.\s+\d+|\d+)\s*[:.-]\s*', '', txt_clean, flags=re.IGNORECASE)
                                    clean_txt = re.sub(r'\s+', ' ', clean_txt).strip()
                                    if clean_txt and len(clean_txt) < 80:
                                        chapters.append(clean_txt)
                        elif sibling.name == "table":
                            for tr in sibling.find_all("tr"):
                                cells = tr.find_all("td")
                                if cells:
                                    for cell in cells:
                                        ct = cell.get_text().strip()
                                        if ct and not ct.isdigit() and len(ct) < 80:
                                            chapters.append(ct)
                                            break
                    if len(chapters) >= 1:
                        return chapters

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
                header_rows = []
                for tr in table.find_all("tr"):
                    ths = tr.find_all("th")
                    if ths:
                        header_rows.append(ths)
                        
                if not header_rows:
                    continue
                    
                best_ths = max(header_rows, key=len)
                headers_list = [clean_name(th.get_text()) for th in best_ths]
                
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
                    for tr in rows:
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
        page_title_lower = page_title.lower()
        if "chapters" in page_title_lower or "list" in page_title_lower or "volume" in page_title_lower:
            is_volume_page = False
            if req_vol:
                vol_pat = rf'\b(volume|vol|bk|book)\s*{req_vol}\b'
                if re.search(vol_pat, page_title_lower):
                    is_volume_page = True
                    
            ignored_terms = {
                "synopsis", "summary", "trivia", "site navigation", "gallery", "illustrations",
                "fan arts", "official art", "songs", "videos", "characters", "references",
                "general information", "main story", "others", "mini arcs", "navigation"
            }
            
            # A. Try to isolate chapters by header section
            headlines = soup.find_all(class_="mw-headline")
            chapter_headline = None
            for hl in headlines:
                hl_text = hl.get_text().strip().lower()
                if "chapter" in hl_text or hl_text == "chapters":
                    chapter_headline = hl
                    break
                    
            if chapter_headline:
                chapters = []
                current = chapter_headline.parent
                for sibling in current.next_siblings:
                    if sibling.name in ("h2", "h3", "h4"):
                        break
                    if sibling.name == "ul":
                        for li in sibling.find_all("li"):
                            txt = li.get_text().strip()
                            if not txt:
                                continue
                            txt_clean = re.sub(r'^\d+[\s.:.-]+', '', txt).strip()
                            txt_clean = re.sub(r'\s+', ' ', txt_clean)
                            txt_clean_lower = txt_clean.lower()
                            if txt_clean_lower in ignored_terms or txt_clean_lower.startswith("category:"):
                                continue
                            clean_txt = re.sub(r'^(chapter\s+\d+|ch\.\s+\d+|\d+)\s*[:.-]\s*', '', txt_clean, flags=re.IGNORECASE)
                            clean_txt = re.sub(r'\s+', ' ', clean_txt).strip()
                            if clean_txt and clean_txt.lower() not in ignored_terms and len(clean_txt) < 80:
                                if clean_txt not in chapters:
                                    chapters.append(clean_txt)
                if len(chapters) >= 1:
                    return chapters[:150]
            
            # B. Page-wide fallback scan
            chapters = []
            for li in soup.find_all("li"):
                txt = li.get_text().strip()
                if not txt:
                    continue
                # Clean leading number/dot prefixes like "1. Synopsis" or "1 Synopsis" -> "Synopsis"
                txt_clean = re.sub(r'^\d+[\s.:.-]+', '', txt).strip()
                # Normalize whitespace
                txt_clean = re.sub(r'\s+', ' ', txt_clean)
                txt_clean_lower = txt_clean.lower()
                
                # Skip navigation links or ignored terms
                if txt_clean_lower in ignored_terms:
                    continue
                if txt_clean_lower.startswith("category:"):
                    continue
                if re.match(r'^(vol\b|volume\b|part|pt\b|book|bk\b)\s*\d+', txt_clean_lower):
                    continue
                    
                # Match chapter pattern or generic list item
                if "chapter" in txt_clean_lower or re.match(r'^\d+\.', txt_clean) or (len(txt_clean) < 80):
                    clean_txt = re.sub(r'^(chapter\s+\d+|ch\.\s+\d+|\d+)\s*[:.-]\s*', '', txt_clean, flags=re.IGNORECASE)
                    clean_txt = re.sub(r'\s+', ' ', clean_txt).strip()
                    if clean_txt and clean_txt.lower() not in ignored_terms and len(clean_txt) < 80:
                        if clean_txt not in chapters:
                            chapters.append(clean_txt)
                            
            min_chapters_threshold = 1 if is_volume_page else 5
            if len(chapters) >= min_chapters_threshold:
                return chapters[:150]
                
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


def fetch_volume_synopsis_from_fandom(subdomain: str, page_title: str) -> str:
    """
    Queries Fandom parse API for the given page_title, searches for a 'Synopsis' section,
    and extracts all text/paragraphs under it until the next headline.
    """
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    
    # 1. Search to resolve the exact page title if it is a bit different (e.g. casing/punctuation)
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": page_title,
        "format": "json",
        "srlimit": 3
    }
    resolved_title = page_title
    try:
        r = httpx.get(url, params=search_params, headers=headers, timeout=3.0)
        if r.status_code == 200:
            search_results = r.json().get("query", {}).get("search", [])
            if search_results:
                # Prioritize a match that contains the volume name
                resolved_title = search_results[0].get("title")
    except Exception:
        pass

    parse_params = {
        "action": "parse",
        "page": resolved_title,
        "prop": "text",
        "format": "json"
    }
    try:
        r = httpx.get(url, params=parse_params, headers=headers, timeout=4.0)
        if r.status_code != 200:
            return ""
        html = r.json().get("parse", {}).get("text", {}).get("*", "")
        soup = BeautifulSoup(html, 'html.parser')
        
        # Find Synopsis headline
        synopsis_head = None
        for hl in soup.find_all(class_="mw-headline"):
            if "synopsis" in hl.get_text().lower():
                synopsis_head = hl
                break
                
        if synopsis_head:
            paragraphs = []
            current = synopsis_head.parent
            for sibling in current.next_siblings:
                if sibling.name in ("h2", "h3"):
                    break
                if sibling.name == "p":
                    p_text = sibling.get_text().strip()
                    if p_text:
                        paragraphs.append(p_text)
                elif sibling.name == "ul":
                    for li in sibling.find_all("li"):
                        li_text = li.get_text().strip()
                        if li_text:
                            paragraphs.append("- " + li_text)
            
            # Clean text (remove brackets/references like [1], [2])
            text = "\n\n".join(paragraphs)
            text = re.sub(r'\[\d+\]', '', text)
            text = re.sub(r'&\#91;\d+&\#93;', '', text)
            return text.strip()
    except Exception as e:
        log.warning(f"Failed to fetch volume synopsis for '{resolved_title}': {e}")
    return ""

