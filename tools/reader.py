"""
tools/reader.py — GET /read/{gutenberg_id}/{page}: in-site reading of
public-domain Project Gutenberg texts.

SPIKE NOTE: gutendex.com (the third-party API) 403s Render's datacenter
IP, so this module fetches the raw text straight from gutenberg.org with
PGLAF mirrors as fallbacks — whether gutenberg.org itself blocks Render
is exactly what deploying this verifies. If every mirror fails, /read
returns 503 and the frontend reader page shows the external link instead.

Storage: the cleaned text is split into ~4,000-char pages at paragraph
boundaries, stored in Redis as BUNDLES of 20 pages per key (a 1MB novel
≈ 13 keys instead of 250) with a small meta key. Ingest happens once on
first request, guarded by the same best-effort lock /daily uses.
"""
import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Response

import cache

log = logging.getLogger("bookhub-api.tools.reader")

router = APIRouter()

_UA = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

PAGE_CHARS = 4000       # target characters per reader page
BUNDLE = 20             # pages per Redis key
MAX_TEXT_BYTES = 3_000_000  # refuse anything larger (protects Redis quota)
TTL = 60 * 60 * 24 * 30

# Plain-text URL patterns, most canonical first. PGLAF runs the official
# Gutenberg mirrors — same files, different hosts/routes.
_MIRRORS = [
    "https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
    "https://www.gutenberg.org/files/{gid}/{gid}-0.txt",
    "https://gutenberg.pglaf.org/cache/epub/{gid}/pg{gid}.txt",
    "https://aleph.pglaf.org/cache/epub/{gid}/pg{gid}.txt",
]


def _fetch_text(gid: int) -> str | None:
    # Full novels are ~1MB — allow a slow read (60s) but fail connects fast,
    # and retry each mirror once: gutenberg.org occasionally drops a transfer
    # midway and succeeds on the second attempt.
    timeout = httpx.Timeout(connect=8.0, read=60.0, write=10.0, pool=8.0)
    for pattern in _MIRRORS:
        url = pattern.format(gid=gid)
        for attempt in (1, 2):
            try:
                r = httpx.get(url, headers=_UA, timeout=timeout, follow_redirects=True)
                if r.status_code != 200:
                    log.warning(f"Reader mirror {url} returned {r.status_code}")
                    break  # try next mirror — a 4xx/5xx won't change on retry
                if len(r.content) > MAX_TEXT_BYTES:
                    log.warning(f"Reader: ebook {gid} too large ({len(r.content)}B) — refusing")
                    return None
                return r.text
            except Exception as e:
                log.warning(f"Reader mirror {url} failed (attempt {attempt}): {e}")
    return None


def _strip_boilerplate(text: str) -> str:
    """Cut the Gutenberg license header/footer via the *** START/END markers."""
    m = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text, re.IGNORECASE | re.DOTALL)
    if m:
        text = text[m.end():]
    m = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", text, re.IGNORECASE)
    if m:
        text = text[:m.start()]
    return text.strip()


def _paginate(text: str) -> list[str]:
    """~PAGE_CHARS pages, split at paragraph boundaries so pages read cleanly."""
    paragraphs = re.split(r"\n\s*\n", text)
    pages, current, size = [], [], 0
    for p in paragraphs:
        p = p.strip("\n")
        if size and size + len(p) > PAGE_CHARS:
            pages.append("\n\n".join(current))
            current, size = [], 0
        current.append(p)
        size += len(p) + 2
    if current:
        pages.append("\n\n".join(current))
    return pages


def _ingest(gid: int) -> dict | None:
    text = _fetch_text(gid)
    if not text:
        return None
    pages = _paginate(_strip_boilerplate(text))
    if not pages:
        return None
    meta = {"pages": len(pages), "bundle": BUNDLE}
    # Bundles first, meta LAST — a half-stored book never looks complete.
    for i in range(0, len(pages), BUNDLE):
        key = f"read:{gid}:{i // BUNDLE}"
        if not cache.set_key_strict(key, pages[i:i + BUNDLE], ttl=TTL):
            log.warning(f"Reader: failed storing bundle {key} — aborting ingest")
            return None
    cache.set_key(f"read:{gid}:meta", meta, ttl=TTL)
    return meta


# Registered BEFORE /read/{gid}/{page} so "search" isn't parsed as a page number.
@router.get("/read/{gid}/search")
def read_search(gid: int, q: str):
    """
    Case-insensitive whole-book search. Scans the cached page bundles
    (L1-memoized after the first scan) and returns up to 50 matches as
    {page, snippet} — one hit per page, ±60 chars of context.
    """
    q = (q or "").strip()
    if gid <= 0 or len(q) < 2 or len(q) > 100:
        raise HTTPException(status_code=400, detail="Query must be 2-100 characters.")

    meta = cache.get_key(f"read:{gid}:meta")
    if not meta:
        cache.acquire_lock(f"read:lock:{gid}", ttl=60)
        meta = _ingest(gid)
        if not meta:
            raise HTTPException(status_code=503, detail="Text unavailable right now.")

    needle = q.lower()
    total, bundle_size = meta["pages"], meta["bundle"]
    results = []
    for b in range((total + bundle_size - 1) // bundle_size):
        bundle = cache.get_key(f"read:{gid}:{b}")
        if not bundle:
            continue
        for i, page_text in enumerate(bundle):
            pos = page_text.lower().find(needle)
            if pos == -1:
                continue
            start = max(0, pos - 60)
            end = min(len(page_text), pos + len(q) + 60)
            snippet = (("…" if start else "")
                       + re.sub(r"\s+", " ", page_text[start:end]).strip()
                       + ("…" if end < len(page_text) else ""))
            results.append({"page": b * bundle_size + i, "snippet": snippet})
            if len(results) >= 50:
                return {"query": q, "results": results, "truncated": True}
    return {"query": q, "results": results, "truncated": False}


@router.options("/read/{gid}/{page}")
def read_options(gid: int, page: int):
    return Response(status_code=204)


@router.get("/read/{gid}/{page}")
def read_page(gid: int, page: int):
    if gid <= 0 or page < 0:
        raise HTTPException(status_code=400, detail="Bad id/page.")

    meta = cache.get_key(f"read:{gid}:meta")
    if not meta:
        # One ingester per book; a racing loser just ingests too (idempotent).
        cache.acquire_lock(f"read:lock:{gid}", ttl=60)
        meta = _ingest(gid)
        if not meta:
            raise HTTPException(
                status_code=503,
                detail="This text couldn't be fetched right now — use the Project Gutenberg link instead.",
            )

    total = meta["pages"]
    if page >= total:
        raise HTTPException(status_code=404, detail="Page out of range.")

    bundle = cache.get_key(f"read:{gid}:{page // meta['bundle']}")
    if not bundle:
        # Bundle evicted/expired while meta survived — re-ingest once.
        meta = _ingest(gid)
        bundle = cache.get_key(f"read:{gid}:{page // BUNDLE}") if meta else None
        if not bundle:
            raise HTTPException(status_code=503, detail="Text temporarily unavailable.")

    return {"page": page, "pages": total, "text": bundle[page % meta["bundle"]]}
