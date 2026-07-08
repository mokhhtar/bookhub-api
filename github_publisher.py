"""
github_publisher.py — publishes static SEO pages to the bookhub Jekyll repo.

After a summary is generated, publish_book()/publish_author() commit
`_books/<slug>.md` / `_authors/<slug>.md` to GitHub via the Contents API
(plain httpx — no git binary on Render). GitHub Actions then rebuilds the
site, so each book gets a real, indexable page at /summary/<slug>/ and each
author at /authors/<slug>/.

Design rules:
- Runs in FastAPI BackgroundTasks; NEVER raises into the request path.
- Create-only (no `sha` sent): we never overwrite an existing page. 409/422
  from concurrent writers are benign skips.
- Dedupe: Redis flags first (published_gid:{google_id} / published:{slug} /
  published_author:{slug}), then a Contents-API GET as the fallback source
  of truth when Redis is cold.
- YAML safety: every front-matter string is emitted via json.dumps (valid
  YAML scalar, immune to quotes/colons in titles).
- Author bios are GROUNDED: Wikipedia page summary accepted only when its
  description says the person is a writer; Gemini rewrites strictly from
  that extract. No qualifying page → bio-less template, never invented text.
"""
import base64
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone

import httpx

import cache
import slug as slug_mod

log = logging.getLogger("bookhub-api.github_publisher")

GITHUB_API = "https://api.github.com"
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "mokhhtar/bookhub")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PUBLISH_ENABLED = os.environ.get("GITHUB_PUBLISH_ENABLED", "false").lower() == "true"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "BookHub/1.0",
}

_WRITER_RE = re.compile(
    r"writer|novelist|author|poet|playwright|essayist|journalist", re.IGNORECASE
)

# Same "source already numbered its chapters" cleanup as the dynamic tool's
# JS (summary.html), ported to Python so the STATIC page never needs
# client-side cleanup — the committed markdown is already correct.
_CHAPTER_NUM_RE = re.compile(
    r"^\s*(?:ch(?:apter)?\.?\s*)?\d{1,4}\s*[-–—.:)\]]?\s*", re.IGNORECASE
)

# A cheap floor against ever publishing a failed/near-empty summary as a
# public page — not a quality bar, just a "did this actually work" guard.
MIN_SUMMARY_CHARS = 300


def is_enabled() -> bool:
    return PUBLISH_ENABLED and bool(GITHUB_PAT)


# ── GitHub Contents API helpers ──────────────────────────────

def _contents_url(path: str) -> str:
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}"


def _file_exists(path: str) -> tuple[bool, str]:
    """Returns (exists, decoded_content). decoded_content only on exists=True."""
    try:
        r = httpx.get(_contents_url(path), headers=_HEADERS,
                      params={"ref": GITHUB_BRANCH}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data.get("content", "") or "").decode("utf-8", errors="replace")
            return True, content
    except Exception as e:
        log.warning(f"Contents GET failed for '{path}': {e}")
    return False, ""


def _create_file(path: str, content: str, message: str) -> bool:
    """Create-only PUT. Returns True on created; False on any skip/failure."""
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": GITHUB_BRANCH,
        }
        r = httpx.put(_contents_url(path), headers=_HEADERS, json=payload, timeout=10.0)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (409, 422):
            # Already exists / concurrent create race — benign.
            log.info(f"Contents PUT skipped ({r.status_code}) for '{path}' — already exists.")
            return False
        log.warning(f"Contents PUT failed ({r.status_code}) for '{path}': {r.text[:200]}")
    except Exception as e:
        log.warning(f"Contents PUT failed for '{path}': {e}")
    return False


# ── Front-matter emission ────────────────────────────────────

def _yaml_str(value) -> str:
    """JSON string literal — a valid YAML scalar, safe for quotes/colons."""
    return json.dumps(value if value is not None else "", ensure_ascii=False)


def _yaml_json(value) -> str:
    """
    JSON encoding of ANY JSON-serializable value (list/dict/scalar/None) — a
    JSON list/object is also valid flow-style YAML, so this is just _yaml_str
    generalized beyond scalars. Used for the richer nested fields (chapters,
    similar_books, awards, ratings) below.
    """
    return json.dumps(value, ensure_ascii=False) if value is not None else "null"


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _clean_chapter_title(title: str) -> str:
    """Strip source-provided leading numbering ('01 Experiment 626' -> 'Experiment 626')
    so the static page's own numbered list never doubles up — mirrors the JS
    fix already shipped on the dynamic page's chapter list."""
    if not isinstance(title, str):
        return ""
    stripped = _CHAPTER_NUM_RE.sub("", title).strip()
    return stripped or title.strip()


def _is_publishable(result: dict) -> bool:
    """Cheap floor against publishing a failed/near-empty summary as a public
    page — not a quality bar, just a sanity check that generation worked."""
    text = _strip_tags(result.get("summary", "") or "")
    return len(text) >= MIN_SUMMARY_CHARS


def _book_markdown(result: dict, book_slug: str, a_slug: str) -> str:
    summary_html = result.get("summary", "") or ""
    description = _strip_tags(summary_html)[:160]
    categories = result.get("categories") or []

    chapters = [_clean_chapter_title(c) for c in (result.get("chapters") or [])]
    similar_books = [
        {k: b.get(k) for k in ("title", "author", "cover_url", "google_id", "isbn_13")}
        for b in (result.get("similar_books") or [])
    ]
    awards = result.get("awards") or []
    ratings = result.get("ratings")  # dict or None
    themes = result.get("themes") or []

    lines = [
        "---",
        "layout: book",
        f"title: {_yaml_str(result.get('title'))}",
        f"author: {_yaml_str(result.get('author'))}",
        f"author_slug: {_yaml_str(a_slug)}",
        f"slug: {_yaml_str(book_slug)}",
        "categories: [" + ", ".join(_yaml_str(c) for c in categories) + "]",
        f"cover_url: {_yaml_str(result.get('cover_url'))}",
        f"isbn_13: {_yaml_str(result.get('isbn_13'))}",
        f"isbn_10: {_yaml_str(result.get('isbn_10'))}",
        f"google_id: {_yaml_str(result.get('google_volume_id'))}",
        f"openlibrary_id: {_yaml_str(result.get('open_library_work_key'))}",
        f"published_year: {_yaml_str(result.get('published_year'))}",
        f"page_count: {result.get('page_count') if isinstance(result.get('page_count'), int) else 'null'}",
        f"average_rating: {result.get('average_rating') if isinstance(result.get('average_rating'), (int, float)) else 'null'}",
        f"amazon_url: {_yaml_str(result.get('amazon_url'))}",
        f"description: {_yaml_str(description)}",
        # Richer content + trust signals — see plan "Enrich the static book
        # page" for why: chapters/similar_books/awards/ratings were already
        # available in the API response but previously discarded here.
        f"chapters: {_yaml_json(chapters)}",
        f"similar_books: {_yaml_json(similar_books)}",
        f"awards: {_yaml_json(awards)}",
        f"ratings: {_yaml_json(ratings)}",
        f"themes: {_yaml_json(themes)}",
        f"reading_level: {_yaml_str(result.get('reading_level'))}",
        f"free_ebook: {_yaml_json(result.get('free_ebook'))}",
        f"quotes: {_yaml_json(result.get('quotes'))}",
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z')}",
        "---",
        "",
        summary_html,  # raw block HTML — kramdown passes it through unindented
        "",
    ]
    return "\n".join(lines)


def _author_markdown(name: str, a_slug: str, photo_url: str, wikipedia_url: str, bio: str) -> str:
    lines = [
        "---",
        "layout: author",
        f"name: {_yaml_str(name)}",
        f"slug: {_yaml_str(a_slug)}",
        f"photo_url: {_yaml_str(photo_url)}",
        f"wikipedia_url: {_yaml_str(wikipedia_url)}",
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z')}",
        "---",
        "",
        bio,
        "",
    ]
    return "\n".join(lines)


# ── Author grounding (Wikipedia + Gemini rewrite) ────────────

def _fetch_wikipedia_author(name: str) -> dict:
    """
    Wikipedia REST page summary for the author. Accepted ONLY if the page's
    short description says the person is a writer — the gate against grabbing
    a same-named athlete/politician. Returns {} when no qualifying page.
    """
    try:
        title = urllib.parse.quote((name or "").split(",")[0].strip().replace(" ", "_"))
        r = httpx.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
            headers={"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"},
            timeout=8.0, follow_redirects=True,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        desc = data.get("description", "") or ""
        extract = data.get("extract", "") or ""
        if not _WRITER_RE.search(desc) and not _WRITER_RE.search(extract[:200]):
            return {}
        return {
            "extract": extract,
            "photo_url": (data.get("thumbnail") or {}).get("source", ""),
            "wikipedia_url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
        }
    except Exception as e:
        log.warning(f"Wikipedia author lookup failed for '{name}': {e}")
        return {}


def _write_author_bio(name: str, extract: str) -> str:
    """Gemini rewrite grounded STRICTLY on the Wikipedia extract."""
    import gemini_client
    prompt = f"""Rewrite the following Wikipedia extract as a clean 1-2 paragraph author bio for a book website. Use ONLY facts present in the extract — do not add anything from outside it. Plain text, no markdown, no headings.

AUTHOR: {name}

EXTRACT:
\"\"\"{extract[:2000]}\"\"\""""
    try:
        return gemini_client.generate(prompt).strip()
    except Exception as e:
        log.warning(f"Author bio generation failed for '{name}': {e}")
        return ""


# ── Public entry points (run in BackgroundTasks) ─────────────

def publish_book(result: dict) -> None:
    """Commits _books/<slug>.md for a freshly summarized book. Never raises."""
    try:
        if not is_enabled():
            return
        title = result.get("title") or ""
        if not _is_publishable(result):
            log.info(f"Summary for '{title}' too short/empty — skipping static publish.")
            return
        book_slug = result.get("slug") or slug_mod.book_slug(title)
        if not book_slug:
            log.info(f"No usable slug for '{title}' (non-Latin?) — skipping publish.")
            return
        gid = result.get("google_volume_id") or result.get("isbn_13") or ""

        # Dedupe layer 1: Redis flags.
        if gid and cache.get_key(f"published_gid:{gid}"):
            return
        if cache.get_key(f"published:{book_slug}"):
            return

        a_slug = slug_mod.author_slug(result.get("author") or "")

        # Dedupe layer 2 + collision handling: the repo itself.
        path = f"_books/{book_slug}.md"
        exists, content = _file_exists(path)
        if exists:
            if gid and gid in content:
                _mark_published(book_slug, gid)
                return  # same book already published
            # Different book shares the slug — suffix with author, then -2.
            for candidate in (f"{book_slug}-{a_slug}", f"{book_slug}-2"):
                if not candidate:
                    continue
                c_exists, c_content = _file_exists(f"_books/{candidate}.md")
                if c_exists:
                    if gid and gid in c_content:
                        _mark_published(candidate, gid)
                        return
                    continue
                book_slug, path = candidate, f"_books/{candidate}.md"
                break
            else:
                log.warning(f"Slug collision unresolved for '{title}' — skipping publish.")
                return

        md = _book_markdown(result, book_slug, a_slug)
        if _create_file(path, md, f"Add book page: {title}"):
            log.info(f"Published book page: {path}")
        _mark_published(book_slug, gid)  # set flags even on benign-skip
    except Exception as e:
        log.warning(f"publish_book failed (non-fatal): {e}")


def _mark_published(book_slug: str, gid: str) -> None:
    ts = datetime.now(timezone.utc).timestamp()
    cache.set_key(f"published:{book_slug}", {"gid": gid, "ts": ts})
    if gid:
        cache.set_key(f"published_gid:{gid}", {"slug": book_slug, "ts": ts})


def publish_author(name: str, book_title: str) -> None:
    """Commits _authors/<slug>.md the first time an author appears. Never raises."""
    try:
        if not is_enabled() or not name or name.lower() in ("unknown", "author"):
            return
        first_author = name.split(",")[0].strip()
        a_slug = slug_mod.author_slug(first_author)
        if not a_slug:
            return

        if cache.get_key(f"published_author:{a_slug}"):
            return
        path = f"_authors/{a_slug}.md"
        exists, _ = _file_exists(path)
        if exists:
            cache.set_key(f"published_author:{a_slug}", {"ts": datetime.now(timezone.utc).timestamp()})
            return

        wiki = _fetch_wikipedia_author(first_author)
        bio = ""
        if wiki.get("extract"):
            bio = _write_author_bio(first_author, wiki["extract"])
        if not bio:
            # Grounding gate failed → neutral template, never an invented bio.
            bio = f"{first_author} is the author of {book_title}."

        md = _author_markdown(first_author, a_slug, wiki.get("photo_url", ""),
                              wiki.get("wikipedia_url", ""), bio)
        if _create_file(path, md, f"Add author page: {first_author}"):
            log.info(f"Published author page: {path}")
        cache.set_key(f"published_author:{a_slug}", {"ts": datetime.now(timezone.utc).timestamp()})
    except Exception as e:
        log.warning(f"publish_author failed (non-fatal): {e}")


def resolve_published(book_slug: str, gid: str = "") -> tuple:
    """
    Returns (static_page_ready, actual_slug).

    Checks the gid-keyed flag FIRST because it records the slug the page was
    ACTUALLY published under — on a slug collision publish_book suffixes the
    slug (e.g. "dune-frank-herbert"), and checking only the unsuffixed slug
    would report static_page=False forever for that book. "Ready" means
    published >5 minutes ago (GitHub Actions rebuild buffer), so the frontend
    may safely swap to the clean URL.
    """
    now = datetime.now(timezone.utc).timestamp()
    if gid:
        flag = cache.get_key(f"published_gid:{gid}")
        if isinstance(flag, dict) and flag.get("slug"):
            return (now - (flag.get("ts") or 0)) > 300, flag["slug"]
    if book_slug:
        flag = cache.get_key(f"published:{book_slug}")
        if isinstance(flag, dict):
            return (now - (flag.get("ts") or 0)) > 300, book_slug
    return False, book_slug


def static_page_ready(book_slug: str) -> bool:
    """Back-compat wrapper around resolve_published."""
    ready, _ = resolve_published(book_slug)
    return ready
