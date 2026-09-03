"""
tools/fandom.py — Fandom Wiki Integration Tool

Exposes routes to resolve Fandom wiki subdomains and fetch detailed, grounded universe lore guides
(magic systems, character profiles, factions, etc.) using Gemini and Fandom's MediaWiki API.
"""

import os
import re
import logging
import time
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

# Wikis that aggregate MANY unrelated books instead of covering one work.
# A search engine ranks these highly for almost any title — they mention
# every book — and they are the exact shape of the failure already
# documented in _wiki_matches_book: genuinely book-ish, genuinely not about
# the requested book. That check would usually reject them anyway, but
# dropping them here saves a wasted main-page fetch per candidate and closes
# the case where such a wiki's index page does happen to name the title.
#
# THE HOLE THIS ORIGINALLY PATCHED IS CLOSED. _wiki_matches_book used to
# accept any two significant title words on a main page, so "Sapiens: A
# Brief History of Humankind" matched great-books.fandom.com on "brief" and
# "history" while "sapiens" and "humankind" appeared nowhere. Requiring
# every significant word now rejects that pairing on its own, verified with
# this list disabled.
#
# The list stays for the case that fix cannot reach. An aggregator that
# genuinely hosts a page about the requested book satisfies any word test
# honestly — great-books really does cover "A Brief History of Time", so
# every word of that title really is on its main page. What disqualifies it
# is not the words but that the wiki is a general library rather than this
# book's own wiki, and its in-wiki searches answer with other books'
# material. Nothing here distinguishes "dedicated" from "aggregating"
# structurally yet; until something does, these are named.
_GENERIC_BOOK_WIKIS = {
    "books", "book", "bookclub", "great-books", "literature", "novels",
    "fiction", "tropedia", "allthetropes", "speculativefiction",
    "printmedia", "www", "community", "dev", "c", "support",
}

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _get_fandom_from_brave(title: str) -> list[str]:
    """Candidate subdomains from Brave Search's `site:fandom.com`, best first.

    THE TIER THAT ACTUALLY GENERALIZES. Measured 2026-09-03, the two tiers
    that were supposed to fill this role were both dead, and between them
    they took the whole resolver down with them:

      - Google CSE has no key and never can have one. Google closed Custom
        Search to new customers in January 2026 (the same wall
        scripts/akinator/discover_fandom.py hit and documented), so that
        tier is permanently a no-op, not a misconfiguration to go fix.
      - DuckDuckGo now answers the HTML endpoint with HTTP 202 and an
        anomaly/challenge page. The old code only looked for 200, so it
        returned None with no log line at all — the failure was invisible.

    What survived was Wikidata P6262 (essentially never set on book
    entities: 0 of 6 real books in the check that found this) and a
    normalized-title ping that only fires when a wiki is literally named
    "thenameofthewind". So books with real, healthy, on-topic wikis simply
    resolved to None — kingkiller, wot, stormlightarchive, thegrishaverse
    and enderverse all verified present and all previously unreachable.
    Brave returned the correct wiki as the FIRST result for every one.

    Same key and free tier (2,000 req/month) discover_fandom.py already
    uses. Absent key returns [] — this source stays optional, exactly as it
    is there.

    NOT QUOTED AS AN EXACT PHRASE, for the reason discover_fandom.py's
    try_brave records at length: a wiki may spell the work differently from
    the catalog ("Forty MILLENNIUMS of Cultivation") and an exact-phrase
    query then finds nothing at all. Loosening is safe here for the same
    reason it is safe there — this only PROPOSES candidates, and every one
    still has to pass _wiki_matches_book before it is trusted.

    Returns a LIST, unlike the single-shot tiers it replaces: Brave's top
    hit for a title can be a generic aggregator wiki while the real one
    sits second, so the caller walks the list through the relevance check
    rather than betting everything on rank 1.
    """
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return []

    # Cached because /summary resolves the same title twice (get_chapters and
    # get_characters each run the cascade), which would otherwise spend two
    # calls of a 2,000/month budget on one page view.
    cache_key = ("fandom_brave_v2", title.lower())
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached

    q = urllib.parse.urlencode({"q": f"{title} site:fandom.com", "count": 6})
    payload = None
    for attempt in range(2):
        try:
            r = httpx.get(f"{BRAVE_ENDPOINT}?{q}", timeout=6.0, headers={
                "Accept": "application/json", "X-Subscription-Token": key})
            if r.status_code == 429:      # free tier is 1 req/s
                time.sleep(1.2)
                continue
            if r.status_code != 200:
                log.warning(f"Brave search for '{title}' returned HTTP {r.status_code}")
                return []                 # NOT cached — a bad key or an
                                          # exhausted quota must not freeze
                                          # this title as "no wiki exists".
            payload = r.json()
            break
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"Brave search failed for '{title}': {e}")
    if payload is None:
        return []

    subs: list[str] = []
    for res in ((payload.get("web") or {}).get("results") or []):
        host = urllib.parse.urlparse(res.get("url", "")).hostname or ""
        m = re.match(r"^([a-z0-9_-]+)\.fandom\.com$", host.lower())
        if m and m.group(1) not in _GENERIC_BOOK_WIKIS and m.group(1) not in subs:
            subs.append(m.group(1))

    # An empty result is barely evidence and is cached for an hour, not for
    # days. A 200 from a search API can still be a degraded 200 — zero
    # results for a book that plainly has a wiki — and the 3-day negative
    # this used to write turned one such response into a book that stayed
    # missing. It happened immediately, to "The Eye of the World", from a
    # deploy-polling loop hitting the same title ~22 times: the wiki
    # resolved fine before and after, and nothing but this entry stood
    # between them. An hour still spends only one call on a /summary, which
    # resolves the same title twice seconds apart — that was the entire
    # reason to cache here. v2 orphans what v1 froze.
    cache.set(subs, *cache_key, ttl=None if subs else 3600)
    return subs


def _get_fandom_from_google_cse(title: str, api_key: str, cx_id: str) -> Optional[str]:
    """Query Google Custom Search API to resolve fandom subdomain (site:fandom.com).

    DEAD TIER, kept only because it costs nothing when unconfigured: Google
    closed Custom Search to new customers in January 2026, so the key this
    guards on can no longer be obtained. See _get_fandom_from_brave, which
    is the working replacement.
    """
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
    """Fallback search using DuckDuckGo HTML page parsing.

    LAST RESORT, AND CURRENTLY BLOCKED. As of 2026-09-03 this endpoint
    answers with HTTP 202 and an anomaly/challenge page rather than results.
    It is kept (it may unblock, and it runs only after Brave has already
    failed) but the non-200 branch now LOGS. It used to fall straight
    through to `return None`, which is how a whole tier stayed dead without
    leaving a single line of evidence anywhere.
    """
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    data = {
        "q": f"site:fandom.com {title}"
    }
    try:
        r = httpx.post(url, data=data, headers=headers, timeout=5.0)
        if r.status_code != 200:
            log.warning(f"DuckDuckGo search for '{title}' returned HTTP {r.status_code} "
                        f"(blocked/challenged, not a result page)")
            return None
        urls = re.findall(r'href="https://([^.]+)\.fandom\.com/wiki/', r.text)
        for sub in urls:
            if sub not in _GENERIC_BOOK_WIKIS:
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

# Categories that confidently signal NON-fiction — used to skip Fandom
# wiki resolution entirely (no legitimate dedicated wiki exists for these).
# Deliberately narrower/more conservative than taxonomy.py's
# _FALLBACK_KEYWORDS (which is tuned for full display-category coverage,
# not fiction/nonfiction confidence) — ambiguous genres like "history"
# (could be historical FICTION) or "science" (sci-fi vs real science) are
# intentionally excluded to stay fail-open toward attempting resolution.
_NONFICTION_KEYWORDS = (
    "self-help", "self help", "business", "economics", "personal finance",
    "biography", "autobiography", "memoir", "cooking", "cookbook",
    "health & fitness", "health and fitness", "travel", "reference",
    "textbook", "how-to", "how to", "true crime", "parenting",
)


def is_confidently_nonfiction(categories: Optional[list[str]]) -> bool:
    """
    Cheap, zero-API-call check on already-resolved Google Books/Open
    Library categories: does at least one confidently signal non-fiction?
    No legitimate dedicated Fandom wiki exists for a self-help/business/
    memoir title, so callers can skip resolution entirely for these —
    saves wasted attempts and narrows the false-positive surface for the
    fuzzy resolver tiers. FAILS OPEN (returns False, i.e. "attempt
    resolution") when categories are empty/ambiguous — many legitimate
    novels have messy or absent category tags, and this gate must never
    block a real fiction lookup.
    """
    if not categories:
        return False
    joined = " ".join(categories).lower()
    return any(kw in joined for kw in _NONFICTION_KEYWORDS)


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

_TITLE_STOPWORDS = {
    "a", "an", "the", "of", "and", "or", "in", "on", "at", "to", "for",
    "with", "is", "it", "by", "as", "from", "into", "this", "that",
}


def _title_mentioned_in_text(title: str, text: str, min_hits: int = 2,
                             require_all: bool = False) -> bool:
    """
    Strict relevance check: does this text blob actually mention the
    REQUESTED book, rather than just being generically "about books"?
    A wiki (or a page within one) can easily be genuinely book-related —
    even genuinely about SOME novel — while having nothing to do with the
    title we asked about; this is the exact gap that let an unrelated
    "plot summaries" wiki feed real, quote-verifiable, but wrong-book text
    into the quiz generator (see CLAUDE.md's "no data beats wrong data").

    Extracts significant words from `title` (length >= 3, not a stopword)
    and requires at least `min_hits` of them to appear as whole-word
    matches somewhere in `text`. A title with only one significant word
    (e.g. "Dune") requires that single word rather than demanding two.

    `require_all` demands every significant word AND demands they sit
    together (see _title_appears_together). It exists because counting any
    two is unsafe when the question is "is this whole wiki about this book".
    Titles assembled from ordinary words defeat the count:
    great-books.fandom.com matched "Sapiens: A Brief History of Humankind"
    on "brief" and "history" — supplied by a link to A Brief History of Time
    — while "sapiens" and "humankind" appear nowhere on the page.

    Measured over 28 wiki/title pairs, full coverage separates them where
    counting two does not: all 11 dedicated wikis carry 100% of their book's
    significant words on the main page, and every aggregator is missing at
    least one. Fifteen of eighteen individual volumes clear it too ("Harry
    Potter and the Goblet of Fire", 4 of 4, on a 24,000-article wiki); the
    three that do not — "A Clash of Kings", "A Storm of Swords", "Words of
    Radiance" — are caught by _wiki_names_the_series instead, which is why
    that signal had to land before this one could.

    Coverage alone was still not enough, and the co-occurrence requirement
    is there because of what turned up when it shipped without one: see
    _title_appears_together for the palaeontology wiki that owns every word
    of a Harari title and none of its meaning.

    The strict mode is for wiki IDENTITY only. The extraction gates ask a
    different and much narrower question — does THIS page, already fetched
    from a wiki we accepted, concern the book — and keep the loose default.

    Deliberately dumb (word presence, not semantic understanding) on
    purpose — matches this codebase's existing preference for
    deterministic, code-level verification over trusting an LLM's
    self-report (see quiz_core.py's substring-match quote verification).
    """
    if not title or not text:
        return False
    words = [w for w in re.findall(r"[a-zA-Z0-9']+", title.lower())
             if len(w) >= 3 and w not in _TITLE_STOPWORDS]
    if not words:
        return False
    text_lower = text.lower()
    if require_all:
        return _title_appears_together(words, text_lower)
    hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text_lower))
    return hits >= min(min_hits, len(words))


# How far apart the title's words may sit and still count as naming the book.
# Every one of 19 verified wiki/title pairs puts them within 4 tokens of each
# other — the title is written out, as a title. 12 is three times that, wide
# enough for a subtitle or an interposed edition note and still nowhere near
# wide enough to staple together words from unrelated paragraphs.
_TITLE_SPAN_TOKENS = 12


def _title_appears_together(words: list[str], text_lower: str) -> bool:
    """Do ALL these words occur within one _TITLE_SPAN_TOKENS-wide window?

    Requiring every word but ignoring where they sit is not enough, and the
    case that proved it is paleontology.fandom.com answering for "Sapiens: A
    Brief History of Humankind". That wiki does contain "sapiens" — in Homo
    sapiens — and "history", "brief" and "humankind" too, scattered across a
    page about fossils, so a coverage test passes it and the summary would
    have been handed a palaeontology wiki's characters for a Harari book.

    Co-occurrence is what separates naming a book from using its vocabulary:
    on all 19 true pairs the words land within 4 tokens because the wiki
    writes the title out, while paleontology's four never share a window at
    any width.
    """
    toks = re.findall(r"[a-zA-Z0-9']+", text_lower)
    need = set(words)
    occ = [(i, t) for i, t in enumerate(toks) if t in need]
    if len({t for _, t in occ}) < len(need):
        return False                       # some word is simply absent

    # Sliding window over just the positions that matter.
    counts: dict = {}
    have = lo = 0
    for hi in range(len(occ)):
        w = occ[hi][1]
        counts[w] = counts.get(w, 0) + 1
        if counts[w] == 1:
            have += 1
        while have == len(need):
            if occ[hi][0] - occ[lo][0] <= _TITLE_SPAN_TOKENS:
                return True
            wl = occ[lo][1]
            counts[wl] -= 1
            if counts[wl] == 0:
                have -= 1
            lo += 1
    return False


def _wikidata_series_label(title: str) -> Optional[str]:
    """The name of the series this book belongs to (Wikidata P179), or None.

    Exists because a series wiki is named after the SERIES and a reader asks
    for a BOOK, and nothing in the title has to resemble the wiki. See
    _wiki_names_the_series for what this is used to decide.
    """
    cache_key = ("wd_series_v1", title.lower())
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached.get("series")

    label = None
    try:
        qid = _search_wikidata_qid_by_title(title)
        if qid:
            headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}
            r = httpx.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
                          headers=headers, timeout=6.0)
            if r.status_code == 200:
                claims = (r.json().get("entities", {}).get(qid, {})
                          .get("claims", {}).get("P179", []))
                if claims:
                    sid = (claims[0].get("mainsnak", {}).get("datavalue", {})
                           .get("value", {}).get("id"))
                    if sid:
                        r2 = httpx.get(
                            f"https://www.wikidata.org/wiki/Special:EntityData/{sid}.json",
                            headers=headers, timeout=6.0)
                        if r2.status_code == 200:
                            label = ((r2.json().get("entities", {}).get(sid, {})
                                      .get("labels", {}).get("en", {}) or {}).get("value"))
    except Exception as e:                                           # noqa: BLE001
        log.warning(f"Wikidata series lookup failed for '{title}': {e}")
        return None      # transient: do not cache, and do not claim "no series"

    # A book genuinely outside any series is the common case and is stable,
    # so the miss is worth caching — just not for the full 30 days, since a
    # sparse Wikidata entity does get filled in.
    cache.set({"series": label}, *cache_key, ttl=None if label else 86400 * 7)
    return label


def _wiki_names_the_series(subdomain: str, sitename: str, title: str) -> bool:
    """Is this wiki named after the series the requested book belongs to?

    THE SECOND WAY A WIKI CAN PROVE IT IS THE RIGHT ONE, and the answer to a
    false negative that the main-page test cannot fix by tuning. A series
    wiki's front page advertises the SERIES; it has no reason to name each
    individual volume. stormlightarchive.fandom.com is unmistakably the
    right wiki for "The Way of Kings" — 1,726 articles, all Stormlight — and
    its main page never once says "Way" or "Kings", so requiring the title
    there rejected it every time on a perfectly good fetch.

    Wikidata P179 gives the book's series ("The Stormlight Archive"), and
    the wiki's own sitename is "Stormlight Archive Wiki". Measured across 10
    pairs, this accepted four right wikis the title test missed
    (stormlightarchive, kingkiller, wot, mistborn) and fired on none of the
    three aggregators (great-books, books, memory-beta) — it is purely
    additive, which is the only reason it is safe to OR with the existing
    test rather than replace it.

    Matched against the SITENAME ONLY, not the main page text. Naming
    yourself after the series is a claim about what the whole wiki is;
    mentioning a series somewhere on a page is not, and a general book wiki
    mentions a great many series.
    """
    series = _wikidata_series_label(title)
    if not series:
        return False
    words = [w for w in re.findall(r"[a-zA-Z0-9']+", series.lower())
             if len(w) >= 3 and w not in _TITLE_STOPWORDS]
    if not words:
        return False
    name_low = sitename.lower()
    if all(re.search(rf"\b{re.escape(w)}\b", name_low) for w in words):
        log.info(f"Accepted '{subdomain}' for '{title}' — wiki is named after "
                 f"its series '{series}'.")
        return True
    return False


def _wiki_matches_book(subdomain: str, title: str) -> bool:
    """
    Validates that a Fandom wiki is actually about a book/novel/manga —
    AND specifically about THIS book — before any tier's match is trusted.

    Used to be genre-only (_wiki_looks_bookish) and only gated the fuzzy
    search-engine/ping tiers, while Wikidata-derived tiers were "trusted
    as-is." That trust was misplaced: Wikidata's own title search
    (_search_wikidata_qid_by_title) is itself fuzzy and can hand back a
    wrong entity, so ALL tiers now go through this same check. Confirmed
    real-world failure: a non-fiction title with no dedicated wiki fuzzily
    resolved to an unrelated multi-book "plot summaries" wiki — genuinely
    book-ish (passed the old check), not about the requested book at all —
    whose generic in-wiki searches then returned real, quote-verifiable
    text from completely different novels.

    Reads the wiki's main page text once and requires BOTH: (1) generic
    book/novel signals, and (2) the requested title's own significant
    words appearing in that same text. Cached per (subdomain, title) —
    NOT per subdomain alone, since "matches this book" is title-specific
    (a per-subdomain-only cache would let one book's false-positive
    verdict leak into every other title that later resolves to the same
    wiki). FAIL-OPEN on network trouble; a cleanly-fetched main page
    failing either check is rejected.

    "Network trouble" now includes a main page that answers with an error
    STATUS, which it did not before, and the gap was doing real damage. A
    timeout on that request raised, hit the except, and failed open — but a
    non-200 fell through with `text` still holding nothing but the sitename,
    and no wiki's sitename alone carries book vocabulary ("A Wheel of Time
    Wiki", "Kingkiller Chronicle Wiki", "Stormlight Archive Wiki" all fail
    _BOOKISH_RE). So the identical failure produced opposite verdicts
    depending only on whether httpx raised or returned, and the returning
    case cached `match: False` for THIRTY DAYS against a healthy wiki.
    Observed live: wot.fandom.com was locked out for "The Eye of the World"
    while "Eye of the World" — a different cache key, same two significant
    words — resolved to it fine.

    A verdict is now only cached when the main page text actually arrived.
    v2 orphans the negatives v1 already poisoned. v3 orphans v2's, because
    _wiki_names_the_series turns some of those negatives into positives and
    a stored "no" would outlive the reason it was recorded. v4 orphans v3's
    in the other direction: requiring every significant title word turns
    some stored "yes" verdicts into no.
    """
    cache_key = ("fandom_relevance_v4", subdomain, title.lower())
    cached = cache.get(*cache_key)
    if cached is not None:
        return bool(cached.get("match"))

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

        r2 = httpx.get(url, params={
            "action": "parse", "page": mainpage, "prop": "text", "format": "json",
        }, headers=headers, timeout=8.0)
        if r2.status_code != 200:
            log.warning(f"Main page of '{subdomain}' returned HTTP {r2.status_code} — "
                        f"relevance for '{title}' is inconclusive, failing open")
            return True  # fail-open, and deliberately NOT cached
        html = ((r2.json().get("parse") or {}).get("text") or {}).get("*", "")
        body = clean_wiki_html(html)[:8000]
        if not body:
            log.warning(f"Main page of '{subdomain}' parsed to empty text — "
                        f"relevance for '{title}' is inconclusive, failing open")
            return True  # same reason: no evidence is not evidence of absence
        text = f"{sitename} {body}"

        bookish = bool(_BOOKISH_RE.search(text))
        relevant = bookish and _title_mentioned_in_text(title, text, require_all=True)
        # Only ask Wikidata when the cheap test has already failed. A wiki
        # whose main page names the book needs no second opinion, so the
        # common path costs nothing and the two extra lookups fall only on
        # the cases that were about to be rejected anyway.
        if bookish and not relevant:
            relevant = _wiki_names_the_series(subdomain, sitename, title)
        cache.set({"match": relevant}, *cache_key)
        if not bookish:
            log.info(f"Rejected fuzzy Fandom match '{subdomain}' for '{title}' — main page has no book signals.")
        elif not relevant:
            log.info(f"Rejected fuzzy Fandom match '{subdomain}' for '{title}' — main page doesn't "
                     f"mention this title and the wiki is not named after its series.")
        return relevant
    except Exception as e:
        log.warning(f"Relevance check failed for '{subdomain}'/'{title}' (fail-open): {e}")
        return True


def _resolve_fandom_subdomain_single(title: str, wikidata_id: Optional[str] = None) -> Optional[str]:
    """
    Highly robust 6-tier subdomain resolver cascade for a single title string.
    ALL tiers must pass _wiki_matches_book (genre + this-title relevance)
    before being trusted — Wikidata-derived tiers 1-2 used to be exempted as
    "trusted as-is," but Wikidata's own title search is itself fuzzy and can
    hand back a wrong entity, so a tier failing the check now falls through
    to the next tier instead of returning immediately.

    Tier 3 (Brave) is the one that carries general titles; tiers 4 and 5 are
    kept but are both known-dead as of 2026-09-03 — see their own docstrings
    for why neither is a configuration problem to go fix.
    """
    # Tier 1: QID provided
    if wikidata_id:
        sub = _get_fandom_from_wikidata(wikidata_id)
        if sub and _wiki_matches_book(sub, title):
            return sub

    # Tier 2: Search QID by title
    qid = _search_wikidata_qid_by_title(title)
    if qid:
        sub = _get_fandom_from_wikidata(qid)
        if sub and _wiki_matches_book(sub, title):
            return sub

    # Tier 3: Brave Search — several ranked candidates, each checked in turn
    # rather than only the top hit (a generic aggregator can outrank the
    # work's own wiki).
    for sub in _get_fandom_from_brave(title):
        if _wiki_matches_book(sub, title):
            return sub

    # Tier 4: Google Custom Search (no key obtainable since Jan 2026)
    api_key = os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY")
    cx_id = os.environ.get("GOOGLE_SEARCH_CX_ID")
    if api_key and cx_id:
        sub = _get_fandom_from_google_cse(title, api_key, cx_id)
        if sub and _wiki_matches_book(sub, title):
            return sub

    # Tier 5: DuckDuckGo HTML Search (currently challenge-walled)
    sub = _get_fandom_from_ddg(title)
    if sub and _wiki_matches_book(sub, title):
        return sub

    # Tier 6: Title Normalization Ping
    normalized = "".join(c.lower() for c in title if c.isalnum())
    if normalized and _ping_fandom_subdomain(normalized) and _wiki_matches_book(normalized, title):
        return normalized

    return None
# WHERE THIS USED TO BE A HARDCODED DICT LITERAL, and why it no longer is.
# Every entry here was once hand-pasted from scripts/akinator/'s discovery
# pipeline into this file, by hand, as a separate step someone had to
# remember — and measured 2026-08-25, that step had stopped happening: the
# game's own harvest had 41 confirmed Fandom wikis and this map knew 19,
# missing real books (Mushoku Tensei, Overlord, Martial Peak among them)
# that /search, quiz.py and summary.py could not find purely because nobody
# had re-typed a JSON entry into a .py file recently enough.
#
# So this is now loaded at runtime from games/data/fandom/wikis.json in the
# bookhub repo — the SAME file an admin review panel writes to when a
# candidate the game discovered gets approved. Approving is now the one
# edit that reaches both tools, instead of a discovery step here and a
# separate, easily-forgotten copy-paste step there.
#
# THE REVIEW GATE ITSELF IS UNCHANGED. games/data/fandom/candidates.json
# holds what the game's discovery tactics (subdomain guessing, Brave
# Search, Wikipedia scanning) have PROVEN exist — proving a wiki is real is
# not the same judgement as trusting it in this live-serving path, which is
# exactly why discover_fandom.py's own docstring says in capitals that it
# never touches this map. A human still has to look at aliases/author/
# cover_url and approve before an entry ever reaches wikis.json; only the
# mechanical part (which file, which format) is now shared.
FANDOM_DATA_URL = "https://litheca.com/games/data/fandom/wikis.json"

# Config data, refreshed at most this often — an approval does not need to
# reach production within seconds, and fetching on every request would spend
# a request's latency on a file that changes rarely.
_FANDOM_CACHE_TTL = 3600

# THESE STAY AS MODULE-LEVEL DICTS, MUTATED IN PLACE, never reassigned.
# book_data.py does `from tools.fandom import FANDOM_SERIES_DETAILS` — a
# plain name import binds to the OBJECT, not to this module's attribute, so
# rebinding this name to a fresh dict on refresh would freeze that import on
# whatever it saw first and never see a later approval. clear()+update()
# keeps the same object alive, so every existing import stays correct with
# no changes anywhere else.
FANDOM_WIKIS: dict = {}
FANDOM_STATIC_MAP: dict = {}
FANDOM_SERIES_DETAILS: dict = {}
_fandom_cache_at = 0.0


def _norm_alias(s: str) -> str:
    """Lowercase, collapse whitespace — the one normalization both sides of
    the static-map comparison must agree on.

    They did not agree. wikis.json stores aliases in display case ("Against
    the Gods", "Coiling Dragon", "The Greatest Estate Developer") because a
    human types them into the review card, while the lookup lowercased the
    incoming title and then tested it against those raw strings. Every
    capitalized alias was therefore unreachable — measured 2026-09-03, 6 of
    25 entries had dead aliases.

    The cost was invisible rather than fatal: a miss here just falls through
    to the resolver cascade, so the book usually still resolved, only after
    an 8-35s round trip through Wikidata and a search engine instead of the
    dict lookup that exists precisely to avoid that. "Fast and 100%
    reliable" was doing neither.
    """
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _refresh_fandom_wikis() -> dict:
    """Reload FANDOM_WIKIS (and its two derived maps) if the cache is stale.

    Called at the top of every function that reads FANDOM_WIKIS directly,
    so callers never see an unpopulated map on first use. A fetch failure
    KEEPS the existing (possibly still-empty, possibly stale) data rather
    than clearing it — a transient network hiccup must not silently stop
    resolving every book this file exists for, the same fail-open shape as
    every other provider chain in this codebase.
    """
    global _fandom_cache_at
    now = time.time()
    if FANDOM_WIKIS and now - _fandom_cache_at < _FANDOM_CACHE_TTL:
        return FANDOM_WIKIS
    try:
        resp = httpx.get(FANDOM_DATA_URL, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data:
            raise ValueError(f"empty or malformed response ({type(data).__name__})")
    except Exception as e:                                       # noqa: BLE001
        log.warning(f"could not refresh fandom_wikis.json: {e}")
        if FANDOM_WIKIS:
            return FANDOM_WIKIS
        return FANDOM_WIKIS  # empty on the very first fetch ever failing

    FANDOM_WIKIS.clear()
    FANDOM_WIKIS.update(data)

    # Keys AND aliases are normalized the same way resolve_fandom_subdomain
    # normalizes an incoming title (lowercased, whitespace collapsed) — see
    # _norm_alias for the bug that came from not doing this.
    FANDOM_STATIC_MAP.clear()
    for k, cfg in FANDOM_WIKIS.items():
        for alias in cfg.get("aliases", []):
            FANDOM_STATIC_MAP[_norm_alias(alias)] = cfg["subdomain"]
        FANDOM_STATIC_MAP[_norm_alias(k)] = cfg["subdomain"]

    FANDOM_SERIES_DETAILS.clear()
    FANDOM_SERIES_DETAILS.update({
        cfg["subdomain"]: {"author": cfg.get("author"), "cover_url": cfg.get("cover_url")}
        for cfg in FANDOM_WIKIS.values()
    })

    _fandom_cache_at = now
    return FANDOM_WIKIS

def resolve_fandom_subdomain(title: str, wikidata_id: Optional[str] = None,
                             categories: Optional[list[str]] = None) -> Optional[str]:
    """
    Resolves Fandom subdomain by trying a static map first, and then various candidates.

    `categories` is optional (backward compatible) — when the caller
    already has Google Books/Open Library categories in scope, passing
    them lets is_confidently_nonfiction() skip resolution entirely for
    non-fiction titles (no legitimate dedicated wiki exists for those).
    """
    if is_confidently_nonfiction(categories):
        return None
    _refresh_fandom_wikis()
    candidates = get_series_title_candidates(title)

    # Tier 0: Static mapping lookup (fast and 100% reliable)
    #
    # Reads FANDOM_STATIC_MAP, which _refresh_fandom_wikis already builds
    # from exactly the same keys and aliases. This scan used to walk
    # FANDOM_WIKIS itself and re-implement the comparison inline — leaving
    # the map built on every refresh and read by nobody, and leaving the
    # inline copy free to disagree with it, which is precisely what it did
    # (it compared a lowercased title against display-case aliases; see
    # _norm_alias).
    for cand in candidates:
        sub = FANDOM_STATIC_MAP.get(_norm_alias(cand))
        if sub:
            return sub


    for cand in candidates:
        sub = _resolve_fandom_subdomain_single(cand, wikidata_id)
        if sub:
            return sub
    return None


def resolve_series_config_first(title: str, categories: Optional[list[str]] = None):
    """
    Consults fandom_catalog.py's CATALOG_SERIES before anything else.

    `categories` is optional (backward compatible) — see resolve_fandom_subdomain's
    docstring for why passing them lets non-fiction titles skip resolution.

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
    if is_confidently_nonfiction(categories):
        return None
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
    _refresh_fandom_wikis()
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
    if not _title_mentioned_in_text(book_title, combined):
        log.info(f"Rejected Fandom chapter extraction for '{book_title}' on wiki '{subdomain}' — fetched pages don't mention this title.")
        return []
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


def extract_characters_from_fandom(subdomain: str, book_title: str) -> list[dict]:
    """
    Finds the wiki page(s) most likely to list this book's characters and
    hands their RAW TEXT to Gemini for extraction — the same
    search→clean_wiki_html→LLM-extraction shape as extract_chapters_from_fandom,
    and for the same reason (every wiki formats character pages differently;
    parsing HTML structure is a special-case treadmill). Gemini is instructed
    to return nothing rather than invent characters not in the text.
    """
    # v2: gated on _title_mentioned_in_text — v1 entries could carry
    # wrong-book characters from an unrelated wiki that passed the old
    # genre-only bookish check.
    cache_key = ("fandom_characters_v2", subdomain, book_title)
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    search_queries = [
        f"{book_title} characters",
        "List of characters",
        "Characters",
        "Main characters",
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
        penalty = 10 if "/" in t else 0
        if "list of characters" in t_low or "character list" in t_low:
            return 0 + penalty
        if t_low.endswith("characters") or t_low == "characters":
            return 1 + penalty
        if "character" in t_low:
            return 2 + penalty
        if book_title.lower() in t_low:
            return 3 + penalty
        return 5 + penalty

    page_titles.sort(key=page_priority)

    MAX_CHARS_PER_PAGE = 6000
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
            log.warning(f"Failed fetching character page '{page_title}' on wiki '{subdomain}': {e}")

    if not candidate_texts:
        cache.set([], *cache_key, ttl=86400)  # short negative — wiki may grow
        return []

    combined = "\n\n".join(f"=== Wiki page: {t} ===\n{txt}" for t, txt in candidate_texts)
    if not _title_mentioned_in_text(book_title, combined):
        log.info(f"Rejected Fandom character extraction for '{book_title}' on wiki '{subdomain}' — fetched pages don't mention this title.")
        cache.set([], *cache_key, ttl=86400)
        return []
    prompt = _build_character_extraction_prompt(book_title, combined)

    characters: list[dict] = []
    try:
        raw = gemini_client.generate(prompt)
        data = gemini_client.parse_json_response(raw)
        if data.get("confident") and isinstance(data.get("characters"), list):
            for c in data["characters"][:15]:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name") or "").strip()
                if not name or len(name) > 80:
                    continue
                characters.append({
                    "name": name,
                    "description": str(c.get("description") or "").strip()[:400],
                    "role": str(c.get("role") or "").strip()[:60],
                    "source": "fandom",
                })
    except Exception as e:
        log.warning(f"Character extraction via Gemini failed for '{book_title}' on wiki '{subdomain}': {e}")

    cache.set(characters, *cache_key, ttl=None if characters else 86400)
    return characters


# Infobox fields worth showing on a character card, in display order.
_INFOBOX_FIELDS = [
    ("age", "Age"), ("date_of_birth", "Born"), ("sex", "Sex"),
    ("gender", "Gender"), ("species", "Species"), ("race", "Race"),
    ("nationality", "Nationality"), ("origin", "Origin"),
    ("occupation", "Occupation"), ("affiliation", "Affiliation"),
    ("status", "Status"),
]


def _clean_infobox_value(val: str, max_len: int = 120) -> str:
    val = re.sub(r'<[^>]+>', ' ', val)
    val = html_lib.unescape(val)
    val = re.sub(r'\[\s*\d+\s*\]', '', val)          # [1] refs
    val = re.sub(r'\[\s*Show Spoilers?\s*\]', '', val, flags=re.IGNORECASE)
    val = re.sub(r'\s+', ' ', val).strip(' ,;')
    return val[:max_len]


def fetch_fandom_character_details(subdomain: str, character_name: str) -> Optional[dict]:
    """
    The character's own wiki page, structured: portable-infobox image +
    whitelisted facts (age/sex/species/...) + the first real prose
    paragraph. Pure parsing of the wiki's own data — nothing generated.
    """
    # v2: description skips flavor quotes/blockquotes (was returning
    # quotations by/about the character instead of who they are).
    cache_key = ("fandom_char_details_v2", subdomain, character_name)
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached or None

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    # Find the character's page (exact-ish title first).
    page_title = None
    try:
        r = httpx.get(url, params={"action": "query", "list": "search",
                                   "srsearch": character_name, "format": "json", "srlimit": 5},
                      headers=headers, timeout=4.0)
        if r.status_code == 200:
            want = re.sub(r"[^a-z0-9]+", " ", character_name.lower()).strip()
            exact, partial = None, None
            for res in r.json().get("query", {}).get("search", []):
                t = res.get("title") or ""
                if "/" in t:  # subpages (Image Gallery, Relationships…) never carry the infobox
                    continue
                got = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
                if got == want and not exact:
                    exact = t
                elif want in got and not partial:
                    partial = t
            page_title = exact or partial
    except Exception as e:
        log.warning(f"Character page search failed for '{character_name}' on '{subdomain}': {e}")
    if not page_title:
        cache.set({}, *cache_key, ttl=86400)
        return None

    try:
        r = httpx.get(url, params={"action": "parse", "page": page_title,
                                   "prop": "text", "format": "json"},
                      headers=headers, timeout=6.0)
        html = r.json().get("parse", {}).get("text", {}).get("*", "") if r.status_code == 200 else ""
    except Exception as e:
        log.warning(f"Character page fetch failed for '{page_title}' on '{subdomain}': {e}")
        html = ""
    if not html:
        cache.set({}, *cache_key, ttl=86400)
        return None

    # Portable-infobox image (strip the /revision/... suffix for a clean URL).
    image_url = None
    m = re.search(r'<figure[^>]*pi-image[^>]*>.*?<img[^>]+src="([^"]+)"', html, re.DOTALL)
    if m:
        image_url = m.group(1).split("/revision/")[0]

    facts = {}
    for src, label in _INFOBOX_FIELDS:
        fm = re.search(
            rf'<div[^>]*class="pi-item pi-data[^"]*"[^>]*data-source="{src}"[^>]*>.*?'
            rf'<div[^>]*pi-data-value[^>]*>(.*?)</div>',
            html, re.DOTALL)
        if fm:
            val = _clean_infobox_value(fm.group(1))
            if val and len(val) > 1:
                facts[label] = val

    # First real prose paragraph AFTER the infobox (paragraphs inside the
    # <aside> are stat rows, not prose). Character pages often OPEN with a
    # flavor quote (blockquote/quote templates) — that's a quotation, not a
    # description, so strip quote blocks and skip quote-looking paragraphs.
    description = ""
    aside_end = html.find("</aside>")
    prose_html = html[aside_end + 8:] if aside_end != -1 else html
    prose_html = re.sub(r'<blockquote[^>]*>.*?</blockquote>', ' ', prose_html, flags=re.DOTALL)
    prose_html = re.sub(r'<(?:table|figure)[^>]*>.*?</(?:table|figure)>', ' ', prose_html, flags=re.DOTALL)
    for pm in re.finditer(r'<p[^>]*>(.*?)</p>', prose_html, re.DOTALL):
        text = _clean_infobox_value(pm.group(1), max_len=500)
        if len(text) <= 80:
            continue
        # Quote heuristics: opens with a quotation mark/dash, or ends with a
        # spoken-by attribution ("— Klein", "―Amber to Sunny").
        if text[0] in "\"'“”‘’«—―" or re.search(r'[—―–]\s*[A-Z][\w .]{1,40}$', text):
            continue
        description = text
        break

    details = {
        "name": character_name,
        "page": page_title,
        "image_url": image_url,
        "facts": facts,
        "description": description,
        "wiki_url": f"https://{subdomain}.fandom.com/wiki/{page_title.replace(' ', '_')}",
        "source": "fandom",
    }
    cache.set(details, *cache_key)
    return details


class CharacterDetailsRequest(BaseModel):
    book_title: str
    name: str
    wikipedia_title: str = ""
    source: str = ""


@router.post("/character/details")
def character_details(req: CharacterDetailsRequest):
    """
    On-demand character card data — no static page needed (fixes the
    404-on-click: the frontend expands details in place instead of
    navigating). Fandom books → infobox facts/photo; classics → the
    Wikipedia page summary behind the character's P674 sitelink.
    """
    if req.source == "wikidata" and req.wikipedia_title:
        try:
            import github_publisher
            wiki = github_publisher._fetch_wikipedia_author(req.wikipedia_title, gate_re=None)
            if wiki.get("extract"):
                return {"found": True, "name": req.name, "source": "wikipedia",
                        "image_url": wiki.get("photo_url") or None, "facts": {},
                        "description": wiki["extract"][:600],
                        "wiki_url": wiki.get("wikipedia_url") or ""}
        except Exception as e:
            log.warning(f"Wikipedia character details failed for '{req.name}': {e}")
        return {"found": False}

    try:
        series_config = resolve_series_config_first(req.book_title)
        subdomain = series_config.subdomain if series_config else resolve_fandom_subdomain(req.book_title)
        if subdomain:
            details = fetch_fandom_character_details(subdomain, req.name)
            if details:
                return {"found": True, **details}
    except Exception as e:
        log.warning(f"Fandom character details failed for '{req.name}': {e}")
    return {"found": False}


# A page that DECLARES it also covers a screen adaptation is dropped
# whole, before a single question is generated from it.
#
# V for Vendetta's page for Gordon Deitrich opens: "a character in the
# V for Vendetta graphic novel as well as its 2006 film adaptation" —
# and then describes his television show and an unapproved sketch, which
# happen only in the film. In the book he is a small-time crook with no
# TV show at all. The quote we published was verbatim from the wiki and
# verified against it; the wiki was simply telling us about a different
# work in the same article.
#
# This reads the page's own OPENING DECLARATION rather than hunting for
# film vocabulary in the body. Chasing wording is what failed three times
# in this codebase already — the disambiguation filter, the Wikiquote
# redirect, the adaptation qualifier. A page that says what it is can be
# believed; a page that does not is judged on nothing.
_ADAPTATION_DECLARATION = re.compile(
    r"\b(?:as well as|and(?: in)?|also(?: appears)? in)\b[^.]{0,80}?"
    r"\b(?:\d{4}\s+)?(?:film|movie|television series|TV series|miniseries|"
    r"anime|musical)\b(?:\s+adaptation)?",
    re.IGNORECASE,
)

# The cheapest declaration of all: the page TITLE. Wikis disambiguate a book
# from its adaptation exactly this way — "V for Vendetta (Film)" sits beside
# "V for Vendetta" on the same wiki, and the search that gathers quiz pages
# had queued it. Caught before a single request is spent on it.
_ADAPTATION_TITLE = re.compile(
    r"\((?:\d{4}\s+)?(?:film|movie|tv|television)[^)]*\)|"
    r"\b(?:film|movie|television)\s+(?:series|adaptation|version)\b",
    re.IGNORECASE,
)


def fetch_fandom_quiz_text(subdomain: str, book_title: str) -> Optional[str]:
    """
    Grounding text for the per-book quiz: the wiki's own PLOT/RECAP prose
    (fan-written summaries — Fandom never hosts the original novel text,
    which is also why this is the legally safe source). Deliberately
    on-demand and cached separately from chapter extraction: chapters run
    on every summary view, quiz text only when someone actually asks for
    a quiz. Returns one big text blob (quiz_core chunks it) or None.
    """
    # v3: gated on _title_mentioned_in_text — v2 could return real, quote-
    # verifiable text from a completely unrelated book (the wiki genuinely
    # is book-ish, just not about the requested title). v2: main-story-first
    # page ordering (v1 let side stories dominate).
    # v4: pages that DECLARE they also cover a screen adaptation are dropped
    # — by their opening sentence, and by their title — so a v3 blob can
    # still contain film-only events described as the book's.
    cache_key = ("fandom_quiz_text_v4", subdomain, book_title)
    cached = cache.get(*cache_key)
    if cached is not None:
        return cached or None

    url = f"https://{subdomain}.fandom.com/api.php"
    headers = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

    search_queries = [
        f"{book_title} plot",
        f"{book_title} synopsis",
        "Plot",
        "Synopsis",
        "Story arc",
        book_title,
        "Volume 1",
    ]
    page_titles: list[str] = []
    for q in search_queries:
        params = {"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 4}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=3.0)
            if r.status_code == 200:
                for res in r.json().get("query", {}).get("search", []):
                    t = res.get("title")
                    if t not in page_titles:
                        page_titles.append(t)
        except Exception:
            pass

    # Main-story pages first: quizzing readers on a side story or an
    # author's note produces "out of context" questions (seen empirically
    # with LOTM's "In Modern Day" side story dominating the sample).
    def quiz_page_priority(t: str) -> int:
        t_low = t.lower()
        if any(x in t_low for x in ("side story", "author's note", "authors note",
                                    "extra", "gallery", "trivia")):
            return 5
        if "/" in t:
            return 4
        if book_title.lower() in t_low or "plot" in t_low or "synopsis" in t_low:
            return 0
        if "volume" in t_low or "arc" in t_low:
            return 1
        return 2
    page_titles.sort(key=quiz_page_priority)

    MAX_TOTAL = 60_000        # ~20 chunks — plenty for 16-sample generation
    MIN_PAGE_TEXT = 600       # skip stubs/navigation-only pages
    parts: list[str] = []
    total = 0
    for page_title in page_titles[:8]:
        if total >= MAX_TOTAL:
            break
        if _ADAPTATION_TITLE.search(page_title):
            log.info(f"Skipping Fandom page '{page_title}' on '{subdomain}' — "
                     f"its title says it is an adaptation.")
            continue
        params = {"action": "parse", "page": page_title, "prop": "text", "format": "json"}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=4.0)
            if r.status_code != 200:
                continue
            html = r.json().get("parse", {}).get("text", {}).get("*", "")
            text = clean_wiki_html(html)
            if len(text) < MIN_PAGE_TEXT:
                continue
            # Only the opening: that is where a wiki states what the article
            # covers. Later paragraphs mention adaptations in passing on
            # pages that are perfectly about the book.
            if _ADAPTATION_DECLARATION.search(text[:400]):
                log.info(f"Skipping Fandom page '{page_title}' on '{subdomain}' — "
                         f"it declares it also covers a screen adaptation.")
                continue
            take = text[:MAX_TOTAL - total]
            parts.append(f"=== Wiki page: {page_title} ===\n{take}")
            total += len(take)
        except Exception as e:
            log.warning(f"Quiz-text fetch failed for page '{page_title}' on '{subdomain}': {e}")

    combined = "\n\n".join(parts) if total >= 3000 else ""  # too thin → no quiz
    if combined and not _title_mentioned_in_text(book_title, combined):
        log.info(f"Rejected Fandom quiz text for '{book_title}' on wiki '{subdomain}' — fetched pages don't mention this title.")
        combined = ""
    cache.set(combined, *cache_key, ttl=(86400 * 7) if combined else 86400)
    return combined or None


def _build_character_extraction_prompt(book_title: str, wiki_text: str) -> str:
    """Pure extraction — same contract as _build_chapter_extraction_prompt:
    read ONLY the supplied text, return nothing rather than invent."""
    return f"""You are extracting a character list from raw wiki page text. You are NOT summarizing the story and NOT using any outside knowledge of it — only read the text below.

BOOK/SERIES: "{book_title}"

RAW WIKI PAGE TEXT (may mix navigation, categories, or unrelated page furniture with real content):
\"\"\"
{wiki_text}
\"\"\"

TASK: Extract the main characters of this book/series, IF this text clearly describes them. For each: their name, a 1-3 sentence description of who they are (based ONLY on this text), and their role if stated (e.g. "protagonist", "antagonist", "supporting").

RULES:
- Extract only — do not invent characters or facts not explicitly present in the text above.
- If the text does not clearly describe this book's characters, return an empty list with "confident": false. Never fall back on general knowledge.
- Skip navigation clutter, category names, actor/voice-actor names, and non-character entries.
- Most important characters first. Maximum 12 characters.
- Return ONLY a JSON object, nothing else:
{{"confident": true_or_false, "characters": [{{"name": "...", "description": "...", "role": "..."}}]}}"""


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