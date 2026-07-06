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

_BOOKISH_RE = re.compile(
    r"light novel|web novel|novel series|webnovel|\bnovels?\b|\bbooks?\b|"
    r"\bmanga\b|manhwa|manhua|webtoon|light-novel|written by|\bauthor\b|"
    r"book series|\bvolumes?\b",
    re.IGNORECASE,
)


def _wiki_looks_bookish(subdomain: str) -> bool:
    """
    Validates that a Fandom wiki is actually about a book/novel/manga before
    a FUZZY match (search-engine or subdomain-ping tiers) is trusted — those
    tiers happily return a same-named GAME or film wiki, whose volumes then
    pollute search results. Reads the wiki's main page text and requires book
    signals. Cached per-subdomain; FAIL-OPEN on network trouble (a broken
    check must not take down legit wikis), but a cleanly-fetched main page
    with zero book signals is rejected.
    """
    cache_key = ("fandom_bookish_v1", subdomain)
    cached = cache.get(*cache_key)
    if cached is not None:
        return bool(cached.get("bookish"))

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
    try:
        # Resolve the wiki's actual main page name, then read its text.
        r = httpx.get(url, params={
            "action": "query", "meta": "siteinfo", "siprop": "general", "format": "json",
        }, headers=headers, timeout=6.0)
        if r.status_code != 200:
            return True  # fail-open
        general = r.json().get("query", {}).get("general", {})
        mainpage = general.get("mainpage", "Main Page")
        sitename = general.get("sitename", "")

        text = sitename
        r2 = httpx.get(url, params={
            "action": "parse", "page": mainpage, "prop": "text", "format": "json",
        }, headers=headers, timeout=8.0)
        if r2.status_code == 200:
            html = ((r2.json().get("parse") or {}).get("text") or {}).get("*", "")
            text += " " + clean_wiki_html(html)[:8000]

        bookish = bool(_BOOKISH_RE.search(text))
        cache.set({"bookish": bookish}, *cache_key)
        if not bookish:
            log.info(f"Rejected fuzzy Fandom match '{subdomain}' — main page has no book signals.")
        return bookish
    except Exception as e:
        log.warning(f"Bookish check failed for '{subdomain}' (fail-open): {e}")
        return True


def _resolve_fandom_subdomain_single(title: str, wikidata_id: Optional[str] = None) -> Optional[str]:
    """
    Highly robust 5-tier subdomain resolver cascade for a single title string.
    Tiers 1-2 (Wikidata-derived) are trusted as-is; tiers 3-5 are fuzzy
    text-search matches and must pass _wiki_looks_bookish before being used.
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
        if sub and _wiki_looks_bookish(sub):
            return sub

    # Tier 4: DuckDuckGo HTML Search
    sub = _get_fandom_from_ddg(title)
    if sub and _wiki_looks_bookish(sub):
        return sub

    # Tier 5: Title Normalization Ping
    normalized = "".join(c.lower() for c in title if c.isalnum())
    if normalized and _ping_fandom_subdomain(normalized) and _wiki_looks_bookish(normalized):
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
        # NOTE: "Circle of Inevitability" is a SEPARATE work by the same author
        # on the same wiki, not an alias for this book — see CATALOG_SERIES
        # in fandom_catalog.py ("lotm" vs "coi") for the correct disambiguation.
        # Do not add it back here; this simple map has no way to express
        # "same subdomain, different series" and will silently merge the two.
        "aliases": ["lord of the mysteries"],
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


def resolve_series_config_first(title: str):
    """
    Consults fandom_catalog.py's CATALOG_SERIES before anything else.

    Why this has to run BEFORE resolve_fandom_subdomain / FANDOM_WIKIS: two
    different real books can share one Fandom subdomain (e.g. "Lord of the
    Mysteries" and "Circle of Inevitability" — same author, same wiki,
    different series). FANDOM_WIKIS is a flat alias->subdomain map with no
    way to express that distinction, so looking a title up there FIRST
    (as this code used to) silently collapses both books into one entry —
    which is exactly the "shows the wrong volumes/cover" bug this fixes.
    CATALOG_SERIES's series_filter / exclude_series_patterns exist
    specifically to keep such series apart on a shared subdomain, so it
    must be the first thing consulted, not a fallback.

    Returns the matching FandomSeriesConfig, or None if this title isn't
    in the structured catalog yet (caller should fall back to
    resolve_fandom_subdomain for those).
    """
    try:
        from tools.fandom_catalog import resolve_series_config
    except ImportError:
        return None
    try:
        return resolve_series_config(title)
    except Exception as e:
        log.warning(f"resolve_series_config_first failed for '{title}': {e}")
        return None

def extract_fandom_infobox_metadata(
    subdomain: str, novel_title: str, series_config=None
) -> tuple[Optional[str], Optional[str]]:
    """
    Extracts author and cover image URL from a Fandom wiki.

    Priority order:
      1. series_config (a FandomSeriesConfig from fandom_catalog.py, if the
         caller already resolved one via resolve_series_config_first) —
         this is per-SERIES, not per-subdomain, so it correctly distinguishes
         two books sharing one wiki (e.g. lotm vs coi).
      2. FANDOM_WIKIS static config, matched by subdomain — fine for series
         that are the ONLY book on their subdomain, ambiguous otherwise.
      3. Gemini extraction from the actual page content (see below).
    """
    if series_config is not None:
        return series_config.author, series_config.cover_url

    # 2. Otherwise, find the likely novel page and let Gemini identify the
    # author from raw text and pick the correct cover from REAL candidate
    # image URLs — it never invents a URL it wasn't given.
    #
    # Why not keep parsing the infobox HTML directly? Portable Infobox
    # markup, class names (pi-data / pi-image), and even whether an infobox
    # exists at all vary per wiki. A chain of "try infobox, then table, then
    # regex on plain text" fallbacks (as this used to do) is itself a
    # hardcoded model of what a wiki page looks like — it works for the
    # wikis it was tested against and silently returns nothing (or the
    # wrong thing) for the shapes it wasn't. Extraction from the actual
    # page content generalizes instead.

    # 1. Check configuration first (fastest, most reliable, zero API calls)
    for k, cfg in FANDOM_WIKIS.items():
        if cfg["subdomain"] == subdomain:
            return cfg.get("author"), cfg.get("cover_url")

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    # 2. Find the likely novel page (this search step is sound — the
    #    fragility was never in FINDING the page, only in interpreting it).
    main_page = None
    try:
        r = httpx.get(url, params={"action": "query", "meta": "siteinfo", "siprop": "general", "format": "json"},
                       headers=headers, timeout=3.0)
        if r.status_code == 200:
            main_page = r.json().get("query", {}).get("general", {}).get("mainpage")
    except Exception:
        pass

    search_params = {"action": "query", "list": "search", "srsearch": f'"{novel_title}" novel',
                      "format": "json", "srlimit": 5}
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

    # 3. Fetch raw text + real candidate image URLs from the top page only —
    #    one Gemini call, not per-tier guessing across every page.
    page_text = ""
    candidate_images: list[str] = []
    for page_name in target_pages[:2]:
        if not page_name:
            continue
        parse_params = {"action": "parse", "page": page_name, "prop": "text", "format": "json"}
        try:
            r = httpx.get(url, params=parse_params, headers=headers, timeout=5.0)
            if r.status_code != 200:
                continue
            html = r.json().get("parse", {}).get("text", {}).get("*", "")
            soup = BeautifulSoup(html, 'html.parser')

            if not page_text:
                page_text = clean_wiki_html(html)[:4000]

            for img in soup.find_all("img"):
                src = img.get("data-src") or img.get("src")
                if src and not src.startswith("data:") and src not in candidate_images:
                    candidate_images.append(src)

            if page_text and candidate_images:
                break
        except Exception as e:
            log.warning(f"Fandom parse failed for page {page_name} in subdomain {subdomain}: {e}")

    if not page_text:
        return None, None

    author, cover_url = _extract_author_and_cover_via_gemini(novel_title, page_text, candidate_images[:12])

    if cover_url:
        if "/revision/latest" in cover_url:
            cover_url = cover_url.split("/revision/latest")[0] + "/revision/latest"
        if "?" in cover_url:
            cover_url = cover_url.split("?")[0]

    return author, cover_url


def _extract_author_and_cover_via_gemini(
    novel_title: str, page_text: str, candidate_images: list[str]
) -> tuple[Optional[str], Optional[str]]:
    """
    Extraction, not generation: Gemini reads real page text to find the
    author's name, and picks the correct cover from a REAL list of image
    URLs we scraped — it is never allowed to output a URL that isn't in
    that list, so it cannot invent a cover image.
    """
    if not candidate_images:
        images_block = "(no images found on this page)"
    else:
        images_block = "\n".join(f"{i}: {u}" for i, u in enumerate(candidate_images))

    prompt = f"""You are extracting metadata from a Fandom wiki page about a novel. Use ONLY the text and image list below — no outside knowledge.

NOVEL: "{novel_title}"

PAGE TEXT:
\"\"\"
{page_text}
\"\"\"

CANDIDATE IMAGES ON THIS PAGE (index: url):
{images_block}

TASK:
1. Find the author's real name if it is stated in the page text (e.g. near "Author", "Written by", "Novelist"). If not clearly stated, return null.
2. Pick the index of the image most likely to be this novel's cover art (usually the main infobox image — not a logo, icon, wiki banner, or social media icon). If no image looks like a book cover, return null.

RULES:
- Do not invent an author name not present in the text.
- Only choose from the exact image indices given — never output a URL yourself.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format: {{"author": "Name or null", "cover_image_index": integer_or_null}}"""

    try:
        raw = gemini_client.generate(prompt)
        data = gemini_client.parse_json_response(raw)
    except Exception as e:
        log.warning(f"Author/cover extraction via Gemini failed for '{novel_title}': {e}")
        return None, None

    author = data.get("author") if isinstance(data.get("author"), str) and data.get("author") else None

    cover_url = None
    idx = data.get("cover_image_index")
    if isinstance(idx, int) and 0 <= idx < len(candidate_images):
        cover_url = candidate_images[idx]

    return author, cover_url


# Category names (case-insensitive, exact) that Fandom light-novel wikis use to
# group their volume pages. This is the structured, deterministic entry point —
# far more reliable than scraping a page's prose with Gemini.
_VOLUME_CATEGORY_NAMES = {
    "light novel", "light novels", "volumes", "light novel volumes",
    "novels", "novel volumes", "light novel series", "novel",
}

# Titles that look like volumes but are companion/adaptation material, not the
# main light-novel series. Excluded from the deterministic volume list.
_COMPANION_TITLE_RE = re.compile(
    r"fanbook|fan book|junior bunko|manga|short story|artbook|art book|"
    r"\bart collection\b|calendar|anthology|drama|web novel|side story|"
    r"\bguide\b|bonus|special edition|omnibus",
    re.IGNORECASE,
)


def _fetch_volumes_via_category(subdomain: str, book_title: str) -> list[str]:
    """
    Deterministic volume discovery via MediaWiki's STRUCTURED category API —
    no Gemini, no prose scraping, nothing to hallucinate.

    How it works (validated against e.g. Ascendance of a Bookworm → 33 vols,
    Overlord → 17 vols):
      1. List the wiki's categories and match ones that group novel volumes
         (Category:"Light Novel", "Light Novels", "Volumes", ...).
      2. Pull those categories' member pages (list=categorymembers).
      3. Keep only titles shaped like a real volume page — "Part N Volume M",
         "Volume N", or "{Series} Volume N" — and drop companion/adaptation
         material (fanbooks, manga, junior bunko, bonus/special editions...).
      4. Sort by (part, volume).

    Returns [] when the wiki has no usable volume category or too few matches,
    so the caller can fall back to the master-page/Gemini path. Note: like the
    rest of this generalized path, this runs only for series NOT in the
    hand-verified catalog; series that share a subdomain are disambiguated by
    the catalog, which is consulted first.
    """
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    # 1. Find volume-grouping categories on this wiki.
    matched_cats: list[str] = []
    try:
        r = httpx.get(url, params={
            "action": "query", "list": "allcategories",
            "aclimit": 500, "format": "json",
        }, headers=headers, timeout=6.0)
        if r.status_code == 200:
            for c in r.json().get("query", {}).get("allcategories", []):
                name = c.get("*", "")
                if name.strip().lower() in _VOLUME_CATEGORY_NAMES:
                    matched_cats.append(name)
    except Exception as e:
        log.warning(f"Category listing failed for '{subdomain}': {e}")
        return []

    if not matched_cats:
        return []

    # 2. Collect member page titles from those categories.
    member_titles: list[str] = []
    seen: set[str] = set()
    for cat in matched_cats:
        try:
            r = httpx.get(url, params={
                "action": "query", "list": "categorymembers",
                "cmtitle": f"Category:{cat}", "cmlimit": 500, "format": "json",
            }, headers=headers, timeout=8.0)
            if r.status_code == 200:
                for m in r.json().get("query", {}).get("categorymembers", []):
                    t = m.get("title", "")
                    if m.get("ns") == 0 and t and t not in seen:
                        seen.add(t)
                        member_titles.append(t)
        except Exception as e:
            log.warning(f"categorymembers fetch failed for '{cat}' on '{subdomain}': {e}")

    if not member_titles:
        return []

    # 3. Keep only real volume pages, drop companion material.
    series_prefix = re.escape(book_title.strip())
    vol_patterns = [
        re.compile(r"^(Part\s+\d+\s+)?Volume\s+\d+\b", re.IGNORECASE),
        re.compile(rf"^{series_prefix}\s+Volume\s+\d+\b", re.IGNORECASE),
    ]
    volumes = [
        t for t in member_titles
        if "/" not in t
        and not _COMPANION_TITLE_RE.search(t)
        and any(p.match(t) for p in vol_patterns)
    ]

    # A lone match is more likely a false positive than a real one-volume
    # series; require at least two before trusting this path.
    if len(volumes) < 2:
        return []

    # 4. Order by (part, volume).
    def _sort_key(t: str) -> tuple:
        p = re.search(r"Part\s+(\d+)", t, re.IGNORECASE)
        v = re.search(r"Volume\s+(\d+)", t, re.IGNORECASE)
        return (int(p.group(1)) if p else 0, int(v.group(1)) if v else 0)

    volumes.sort(key=_sort_key)
    log.info(f"Resolved {len(volumes)} volumes for '{book_title}' via category API on '{subdomain}'")
    return volumes


def fetch_volumes_from_fandom(subdomain: str, book_title: str) -> list[str]:
    """
    Queries Fandom for all volumes of a book/series.
    Resolution order (most to least reliable):
      1. Hand-verified catalog config (fandom_catalog.py).
      2. Structured MediaWiki category API (_fetch_volumes_via_category) —
         deterministic, no LLM.
      3. Master/index page raw text handed to Gemini for extraction (last
         resort, for wikis with no usable volume category).
    """
    try:
        from tools.fandom_catalog import fetch_volumes_for_search
        catalog_volumes = fetch_volumes_for_search(subdomain, book_title)
        if catalog_volumes:
            return [v.wiki_page for v in catalog_volumes]
    except Exception as e:
        log.warning(f"Fandom catalog volume fetch failed for '{book_title}': {e}")

    # Structured, deterministic category lookup — preferred over the Gemini path.
    try:
        category_volumes = _fetch_volumes_via_category(subdomain, book_title)
        if category_volumes:
            return category_volumes
    except Exception as e:
        log.warning(f"Category-based volume fetch failed for '{book_title}': {e}")

    # ── Generalized path: read a master/index page's raw text via Gemini ──
    # (Always cross-checked against a real page-title scan below — a wiki's
    # naming convention for its master page or volume pages can't be guessed
    # from a fixed literal list, so we try title-derived candidates too.)
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    MAX_CHARS_PER_PAGE = 10000  # keeps prompt size sane; a real volume table fits easily

    master_titles = [
        f"{book_title} (Novel Series)",
        f"{book_title} (Light Novel Series)",
        f"{book_title} (Series)",
        "List of Volumes", "Volumes and Chapters", "Volumes & Chapters", "List of Light Novels", "Volumes",
    ]
    master_text = ""
    for m_title in master_titles:
        params = {"action": "parse", "page": m_title, "prop": "text", "format": "json"}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                parse_data = r.json().get("parse")
                if parse_data:
                    html = parse_data.get("text", {}).get("*", "")
                    text = clean_wiki_html(html)
                    if len(text) > len(master_text):
                        master_text = text[:MAX_CHARS_PER_PAGE]
        except Exception:
            pass

    # Always scan for real volume pages as a closed-set constraint, not only
    # when no master page was found — a wiki's volume pages may be titled
    # "Volume N" or "{Series} Volume N" (e.g. Overlord's "Overlord Volume 01"),
    # so both prefixes are tried and merged.
    all_page_titles: list[str] = []
    seen_titles: set[str] = set()
    for apprefix in ("Volume", f"{book_title} Volume"):
        params = {"action": "query", "list": "allpages", "apprefix": apprefix, "format": "json", "aplimit": 200}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=5.0)
            if r.status_code == 200:
                for res in r.json().get("query", {}).get("allpages", []):
                    t = res.get("title", "")
                    if t and "/" not in t and t not in seen_titles:
                        seen_titles.add(t)
                        all_page_titles.append(t)
        except Exception:
            pass

    if not master_text:
        if not all_page_titles:
            return []
        master_text = "Candidate page titles found on this wiki:\n" + "\n".join(all_page_titles)

    volumes = _extract_volume_list_via_gemini(book_title, master_text, all_page_titles)
    return volumes


def _extract_volume_list_via_gemini(book_title: str, wiki_text: str, known_page_titles: list[str]) -> list[str]:
    """
    Extraction, not generation: Gemini reads the wiki's own volume-index
    text (or, lacking one, a real list of "Volume ..." page titles it
    actually found) and identifies which volumes belong to THIS book,
    in the correct order. It cannot invent a volume/page title that
    wasn't present in the source.
    """
    constraint = (
        f"\nYou may ONLY return titles from this exact list of real page titles on the wiki "
        f"(do not alter their spelling/casing): {known_page_titles}"
        if known_page_titles else ""
    )
    prompt = f"""You are extracting a list of volumes/books belonging to a specific series from raw wiki text. Use ONLY the text below — no outside knowledge.

SERIES/BOOK: "{book_title}"{constraint}

RAW WIKI TEXT:
\"\"\"
{wiki_text}
\"\"\"

TASK: Extract the volume or book titles that belong to this specific series, in their correct reading order. If this text mixes in volumes from an unrelated series (e.g. a spin-off or different work sharing wiki space), exclude those.

RULES:
- Extract only — do not invent volumes not present in the text.
- If you cannot find a genuine volume list for this series in the text, return an empty list.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format: {{"confident": true_or_false, "volumes": ["Volume title 1", "Volume title 2", ...]}}"""

    try:
        raw = gemini_client.generate(prompt)
        data = gemini_client.parse_json_response(raw)
        if data.get("confident") and isinstance(data.get("volumes"), list):
            volumes = [v.strip() for v in data["volumes"] if isinstance(v, str) and v.strip()]
            # If we had a closed set of known real page titles, drop anything
            # not in that set — a final guard against the model straying.
            if known_page_titles:
                known_set = set(known_page_titles)
                volumes = [v for v in volumes if v in known_set]
            return volumes
    except Exception as e:
        log.warning(f"Volume extraction via Gemini failed for '{book_title}': {e}")

    return []



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
    Finds the Fandom wiki page(s) most likely to list this book's chapters,
    then hands their RAW TEXT (not parsed HTML structure) to Gemini for
    extraction. Cataloged series (fandom_catalog.py) still get first refusal,
    since those configs were hand-verified against real wiki structure.

    Why not parse HTML structure (tables / <li> lists / headlines) anymore?
    Every Fandom wiki community formats its chapter-list page differently —
    tables with different column orders, bullet lists, plain headings, or a
    mix of all three on the same page — and a growing pile of series-specific
    exceptions (COI filtering, "ignored_terms" sets, volume-header regexes)
    is exactly the kind of special-case dictionary that breaks on the next
    new title. Extraction with Gemini generalizes: it reads the page's
    meaning regardless of its markup shape, and is explicitly instructed to
    return nothing rather than invent a plausible-looking fake list.
    """
    try:
        from tools.fandom_catalog import fetch_chapters_for_title
        catalog_chapters = fetch_chapters_for_title(subdomain, book_title)
        if catalog_chapters:
            return catalog_chapters
    except Exception as e:
        log.warning(f"Fandom catalog chapter fetch failed for '{book_title}': {e}")

    # ── Generalized path: search for candidate pages, then let Gemini read them ──
    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    req_vol = None
    m = re.search(r'\b(vol\.|volume|vol|part|pt\.|book|bk\.)\s*(\d+(?:\.\d+)?)\b', book_title, flags=re.IGNORECASE)
    if m:
        req_vol = m.group(2)

    # Page search is source-agnostic and stays as-is — the fragility was
    # never in FINDING the page, only in interpreting its HTML shape.
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
            "Volumes",
        ]
    else:
        search_queries = [
            f"List of chapters in the {book_title}",
            f"{book_title} chapters",
            "List of chapters",
            "Chapters",
            book_title,
            f"{book_title} Volume 1",
            "Volume 1",
        ]

    page_titles = []
    for q in search_queries:
        params = {"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 3}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=3.0)
            if r.status_code == 200:
                for res in r.json().get("query", {}).get("search", []):
                    t = res.get("title")
                    if t not in page_titles:
                        page_titles.append(t)
        except Exception:
            pass

    def page_priority(t):
        t_low = t.lower()
        title_low = book_title.lower()
        penalty = 10 if "/" in t else 0
        if req_vol:
            vol_pat = rf'\b(volume|vol|bk|book)\s*{req_vol}\b'
            if re.search(vol_pat, t_low):
                return 0 + penalty
        if any(x in t_low for x in ["volumes and chapters", "volumes & chapters", "list of volumes"]):
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

    # Fetch RAW TEXT for the top candidates — no table/list/headline
    # interpretation. clean_wiki_html() strips tags into plain text.
    MAX_CHARS_PER_PAGE = 6000  # keeps prompt size sane; a real chapter list fits easily
    candidate_texts = []
    for page_title in page_titles[:3]:
        params = {"action": "parse", "page": page_title, "prop": "text", "format": "json"}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=4.0)
            if r.status_code != 200:
                continue
            html = r.json().get("parse", {}).get("text", {}).get("*", "")
            text = clean_wiki_html(html)
            if text:
                candidate_texts.append((page_title, text[:MAX_CHARS_PER_PAGE]))
        except Exception as e:
            log.warning(f"Failed fetching raw text for page '{page_title}' on wiki '{subdomain}': {e}")

    if not candidate_texts:
        return []

    combined = "\n\n".join(f"=== Wiki page: {t} ===\n{txt}" for t, txt in candidate_texts)
    prompt = _build_chapter_extraction_prompt(book_title, req_vol, combined)

    try:
        raw = gemini_client.generate(prompt)
        data = gemini_client.parse_json_response(raw)
        if data.get("confident") and isinstance(data.get("chapters"), list):
            chapters = [c.strip() for c in data["chapters"] if isinstance(c, str) and c.strip()]
            return chapters[:150]
    except Exception as e:
        log.warning(f"Chapter extraction via Gemini failed for '{book_title}' on wiki '{subdomain}': {e}")

    return []


def _build_chapter_extraction_prompt(book_title: str, req_vol: Optional[str], wiki_text: str) -> str:
    """
    Pure extraction prompt — Gemini reads TEXT WE GIVE IT and pulls out a
    chapter list if one is genuinely present. It must not use its own
    training knowledge of the book to fill in gaps or invent a plausible
    list when the source text doesn't clearly contain one.
    """
    volume_hint = (
        f"\nThe user asked specifically about Volume/Part/Book {req_vol}. If the text covers "
        f"multiple volumes, extract ONLY the chapters belonging to that volume."
        if req_vol else ""
    )
    return f"""You are extracting a table of contents from raw wiki page text. You are NOT summarizing the book and NOT using any outside knowledge of it — only read the text below.

BOOK: "{book_title}"{volume_hint}

RAW WIKI PAGE TEXT (may mix unrelated navigation, categories, or other page furniture with real content):
\"\"\"
{wiki_text}
\"\"\"

TASK: Find and extract the actual chapter titles or part/volume titles for this book, IF this text clearly contains a real chapter list (e.g. numbered chapters with titles, a "List of Chapters" section, a table of contents).

RULES:
- Extract only — do not invent, complete, or guess chapter titles not explicitly present in the text above.
- If the text does not contain a clear, genuine chapter list for THIS book (and this volume, if specified), return an empty list. Do not fall back on general knowledge of the book to construct one.
- Ignore wiki navigation clutter, category links, infobox fields, and unrelated content mixed into the text.
- Preserve the original chapter order as it appears in the text.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format: {{"confident": true_or_false, "chapters": ["Chapter title 1", "Chapter title 2", ...]}}
- Maximum 150 chapters."""


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
        
    # Same fix applied everywhere else: try the structured catalog first,
    # since it correctly distinguishes series sharing a subdomain (e.g.
    # lotm vs coi) that the flat FANDOM_WIKIS alias map cannot.
    catalog_cfg = resolve_series_config_first(title)
    subdomain = catalog_cfg.subdomain if catalog_cfg else resolve_fandom_subdomain(title, wikidata_id)
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

    # 1. Resolve subdomain if missing — catalog first, same reasoning as above.
    if subdomain:
        resolved_sub = subdomain
    else:
        catalog_cfg = resolve_series_config_first(title)
        resolved_sub = catalog_cfg.subdomain if catalog_cfg else resolve_fandom_subdomain(title)
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