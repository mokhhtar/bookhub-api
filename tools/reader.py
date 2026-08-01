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
from html import escape as _html_escape
from html.parser import HTMLParser

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


def get_full_text_pages(gid: int) -> list[str] | None:
    """
    Every stored page of the book, in order — the quiz route's grounding
    source. Ensures ingest has run (idempotent, same lock as read_page);
    None when the text can't be fetched/stored. Library function, not a
    route: tools/quiz.py concatenates + re-chunks these via quiz_core's
    paragraph/sentence-aware _chunk_text (reader pagination is a reading-UX
    boundary, not a semantic one).
    """
    meta = cache.get_key(f"read:{gid}:meta")
    if not meta:
        cache.acquire_lock(f"read:lock:{gid}", ttl=60)
        meta = _ingest(gid)
        if not meta:
            return None
    pages: list[str] = []
    total, bundle_size = meta["pages"], meta["bundle"]
    for b in range((total + bundle_size - 1) // bundle_size):
        bundle = cache.get_key(f"read:{gid}:{b}")
        if not isinstance(bundle, list):
            # Bundle evicted while meta survived — one re-ingest attempt.
            meta = _ingest(gid)
            bundle = cache.get_key(f"read:{gid}:{b}") if meta else None
            if not isinstance(bundle, list):
                return None
        pages.extend(bundle)
    return pages or None


# ── Printable edition (the route to a PDF) ───────────────────
#
# Project Gutenberg publishes NO PDF — /ebooks/<id>.pdf and the cache path
# both 404, and an item's file listing holds a .txt and an HTML directory and
# nothing else. Converting the EPUB ourselves would mean carrying a rendering
# engine (Calibre ~500MB, or Chromium, or WeasyPrint's system libraries) on a
# 512MB box with no persistent disk, and its first act would be to unzip the
# EPUB to reach the XHTML that Gutenberg already serves directly.
#
# So we don't make a PDF. We serve the book as one clean document and let the
# browser's own print engine make it — the best typesetter available, already
# installed, free. This endpoint is the "clean document" half.
#
# The HTML edition is used rather than the plain text this module already
# caches, because the difference is the point: it carries real <h1>/<h2>
# chapter headings, emphasis, and illustrations. The text edition would print
# as one undifferentiated slab.
_PRINT_MIRRORS = [
    "https://www.gutenberg.org/cache/epub/{gid}/pg{gid}-images.html",
    "https://www.gutenberg.org/ebooks/{gid}.html.images",
    "https://gutenberg.pglaf.org/cache/epub/{gid}/pg{gid}-images.html",
]
# Decompressed, an average novel is ~850KB — Pride and Prejudice measured
# 852KB from a 137KB gzipped transfer. The cap is generous but real: it is
# the memory this parses in on a box with 512MB.
PRINT_MAX_BYTES = 4_000_000
# Upstash's REST tier caps a single request at ~1MB, so the document is
# stored in pieces, exactly as the reader's page bundles are.
PRINT_CHUNK = 400_000
# Versioned like every other cached value here, and for the reason this repo
# keeps relearning: the sanitiser IS the stored value. The first fix after
# shipping — restoring the line breaks of dropped containers — changed nothing
# for anyone, because Redis kept serving the document produced by the old
# parser for another 30 days.
# v1: initial.
# v2: closing a dropped block container emits <br>, so a table of contents
#     stops printing as one run-on paragraph.
# v3: v2 missed the commoner case — a contents list of bare <a> tags with no
#     container at all. A newline in text OUTSIDE a prose element is now the
#     line structure it plainly is.
PRINT_KEY = "readprint_v3"

_PRINT_TAGS = {
    "h1", "h2", "h3", "h4", "p", "blockquote", "em", "i", "strong", "b",
    "br", "hr", "img", "ul", "ol", "li", "small", "sup", "sub",
}
_PRINT_VOID = {"br", "hr", "img"}
# Every HTML void element, for the suppression DEPTH COUNTER only. It has to
# be the full list, not the three we emit: a <meta> or <link> inside <head>
# never closes, so counting it as an open tag would leave suppression stuck
# on and swallow the entire book.
_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# Tags whose TEXT must go with them. The summary sanitiser lets the text of a
# dropped tag survive escaped — inert, and correct there. Here it is a quality
# bug: Gutenberg's <style> block would print as a slab of CSS on page one of
# every book.
_PRINT_DROP_CONTENT = {"script", "style", "head", "title", "noscript"}
# Block containers we don't emit but whose LINE STRUCTURE we must keep. The
# table of contents made this concrete: Gutenberg lays it out as one block per
# entry, and dropping those left ten chapter titles as adjacent text nodes
# separated by whitespace, which HTML then collapses — "STORY OF THE DOOR
# SEARCH FOR MR. HYDE DR. JEKYLL WAS QUITE AT EASE" as a single paragraph.
# Closing one of these emits a <br> so the lines survive without the layout.
# Elements whose text is prose: inside them the source's line wrapping is
# meaningless and must collapse.
_PRINT_PARAGRAPHISH = {"p", "h1", "h2", "h3", "h4", "blockquote", "li"}
# A run of whitespace containing at least one newline, in bare text OUTSIDE
# any prose element. There the newline is the only line structure present.
_BARE_NEWLINE_RE = re.compile(r"[^\S\r\n]*\r?\n[\s]*")
_PRINT_BLOCK_BREAK = {
    "div", "section", "article", "aside", "header", "footer", "nav",
    "table", "tr", "td", "th", "dl", "dt", "dd", "figure", "figcaption",
    "center", "address", "pre",
}


class _PrintHTMLSanitizer(HTMLParser):
    """
    Gutenberg's HTML reduced to an allow-list, with two jobs beyond safety.

    It DROPS the Project Gutenberg boilerplate — the licence header and
    footer, which are wrapped in elements carrying id="pg-header" /
    id="pg-footer" or class "pg-boilerplate". Those run to hundreds of lines
    and would print as the first and last pages of every book. Suppression is
    depth-counted so nested tags inside a dropped section go with it.

    And it REWRITES image sources. The document references them relatively
    ("images/cover.jpg"), which would resolve against litheca.com and 404;
    they are made absolute against the book's own Gutenberg directory. Images
    are not subject to CORS when displayed, so they load normally.

    Everything else follows the same contract as the summary sanitiser: no
    attributes survive except img src/alt, all text is escaped, comments are
    dropped.
    """

    def __init__(self, gid: int):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._base = f"https://www.gutenberg.org/cache/epub/{gid}/"
        self._suppress_depth = 0   # >0 while inside a boilerplate section
        self._nesting = 0          # depth within that section
        # >0 while inside a paragraph-level element. Outside one, a newline in
        # the source is meaningful line structure; inside one it is only the
        # source's own 70-column wrapping and must collapse. Jekyll & Hyde's
        # contents are bare <a> tags separated by newlines — no container to
        # drop, so the <br>-on-close rule below never fired for them and ten
        # chapter titles printed as a single run-on line.
        self._block_depth = 0

    @staticmethod
    def _is_boilerplate(attrs) -> bool:
        d = dict(attrs)
        return (d.get("id") in ("pg-header", "pg-footer", "pg-machine-header")
                or "pg-boilerplate" in (d.get("class") or ""))

    def handle_starttag(self, tag, attrs):
        if self._suppress_depth:
            if tag not in _VOID_ELEMENTS:
                self._nesting += 1
            return
        if tag in _PRINT_DROP_CONTENT or self._is_boilerplate(attrs):
            self._suppress_depth = 1
            self._nesting = 1
            return
        if tag not in _PRINT_TAGS:
            return
        if tag in _PRINT_PARAGRAPHISH:
            self._block_depth += 1
        if tag == "img":
            src = dict(attrs).get("src") or ""
            if not src or src.startswith("data:"):
                return
            if not src.startswith(("http://", "https://")):
                src = self._base + src.lstrip("./")
            alt = _html_escape(dict(attrs).get("alt") or "", quote=True)
            self._out.append(f'<img src="{_html_escape(src, quote=True)}" alt="{alt}">')
            return
        self._out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._suppress_depth:
            if tag not in _VOID_ELEMENTS:
                self._nesting -= 1
                if self._nesting <= 0:
                    self._suppress_depth = 0
            return
        if tag in _PRINT_TAGS and tag not in _PRINT_VOID:
            if tag in _PRINT_PARAGRAPHISH:
                self._block_depth = max(0, self._block_depth - 1)
            self._out.append(f"</{tag}>")
        elif tag in _PRINT_BLOCK_BREAK:
            self._out.append("<br>")

    def handle_data(self, data):
        if self._suppress_depth:
            return
        text = _html_escape(data, quote=False)
        if self._block_depth == 0:
            text = _BARE_NEWLINE_RE.sub("<br>", text)
        self._out.append(text)

    def handle_comment(self, data):
        pass

    def result(self) -> str:
        html = "".join(self._out)
        # Nested containers each contribute a break, so a chapter wrapped in
        # three divs would open with three blank lines. Two is the most any
        # gap needs to mean.
        html = re.sub(r"(?:\s*<br>\s*){3,}", "<br><br>", html)
        # A break straight after a closing block tag is the source's own
        # newline between elements, not a blank line the author wanted — and
        # it would double every paragraph gap, since the stylesheet already
        # spaces them.
        html = re.sub(r"(</(?:p|h[1-4]|blockquote|li|ul|ol)>)(?:\s*<br>)+",
                      r"\1", html)
        return re.sub(r"\n{3,}", "\n\n", html).strip()


def _fetch_print_html(gid: int) -> str | None:
    timeout = httpx.Timeout(connect=8.0, read=90.0, write=10.0, pool=8.0)
    for pattern in _PRINT_MIRRORS:
        url = pattern.format(gid=gid)
        for attempt in (1, 2):
            try:
                r = httpx.get(url, headers=_UA, timeout=timeout, follow_redirects=True)
                if r.status_code != 200:
                    log.warning(f"Print mirror {url} returned {r.status_code}")
                    break
                if len(r.content) > PRINT_MAX_BYTES:
                    log.warning(f"Print: ebook {gid} too large ({len(r.content)}B) — refusing")
                    return None
                return r.text
            except Exception as e:
                log.warning(f"Print mirror {url} failed (attempt {attempt}): {e}")
    return None


def _ingest_print(gid: int) -> dict | None:
    raw = _fetch_print_html(gid)
    if not raw:
        return None
    parser = _PrintHTMLSanitizer(gid)
    parser.feed(raw)
    html = parser.result()
    # A book that sanitises down to nothing means the document wasn't what we
    # expected — serve no printable edition rather than a blank one.
    if len(html) < 2000:
        log.warning(f"Print: ebook {gid} sanitised to {len(html)} chars — refusing")
        return None
    chunks = [html[i:i + PRINT_CHUNK] for i in range(0, len(html), PRINT_CHUNK)]
    # Chunks first, meta LAST — a half-stored book never looks complete.
    for i, chunk in enumerate(chunks):
        if not cache.set_key_strict(f"{PRINT_KEY}:{gid}:{i}", chunk, ttl=TTL):
            log.warning(f"Print: failed storing chunk {i} for {gid} — aborting")
            return None
    meta = {"chunks": len(chunks), "chars": len(html)}
    cache.set_key(f"{PRINT_KEY}:{gid}:meta", meta, ttl=TTL)
    return meta


# Registered BEFORE /read/{gid}/{page} so "print" isn't parsed as a page number.
@router.get("/read/{gid}/print")
def read_print(gid: int):
    """
    The whole book as one sanitised HTML document, for the site's print view.
    503 when the text can't be fetched — the caller then shows the external
    link instead, the same contract as the reader itself.
    """
    if gid <= 0:
        raise HTTPException(status_code=400, detail="Bad ebook id.")
    meta = cache.get_key(f"{PRINT_KEY}:{gid}:meta")
    if not meta:
        cache.acquire_lock(f"{PRINT_KEY}:lock:{gid}", ttl=120)
        meta = _ingest_print(gid)
        if not meta:
            raise HTTPException(status_code=503, detail="Printable edition unavailable.")
    parts = []
    for i in range(meta["chunks"]):
        chunk = cache.get_key(f"{PRINT_KEY}:{gid}:{i}")
        if not isinstance(chunk, str):
            # A chunk expired while meta survived — one re-ingest attempt.
            meta = _ingest_print(gid)
            chunk = cache.get_key(f"{PRINT_KEY}:{gid}:{i}") if meta else None
            if not isinstance(chunk, str):
                raise HTTPException(status_code=503, detail="Printable edition unavailable.")
        parts.append(chunk)
    return {"gid": gid, "html": "".join(parts)}


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
