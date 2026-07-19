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

# Content-format version for BOOK pages. Bump when _book_markdown starts
# emitting materially richer front-matter/body, so already-published pages
# (create-only otherwise) get REWRITTEN with the new content on the next
# summarize instead of staying frozen at an old, sparse format forever.
# History:
#   1 — implicit original format (pre-versioning: description-only body,
#       none of chapters/quotes/similar/characters/awards/themes)
#   2 — full summary body + enriched front-matter + canonical_id
# v3: free-ebook pages emit noindex:false + sitemap:true (indexing earned by
# the standalone free-book value; everything else stays gated).
# v4: pre-generated, quote-verified quiz (Gutenberg/Fandom-grounded) baked
# into the static page at publish time — `quiz` + `quiz_source` fields.
PUBLISH_CONTENT_VERSION = 4


def is_enabled() -> bool:
    return PUBLISH_ENABLED and bool(GITHUB_PAT)


# ── GitHub Contents API helpers ──────────────────────────────

def _contents_url(path: str) -> str:
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}"


def _file_exists(path: str) -> tuple[bool, str, str]:
    """Returns (exists, decoded_content, blob_sha). content/sha only on exists."""
    try:
        r = httpx.get(_contents_url(path), headers=_HEADERS,
                      params={"ref": GITHUB_BRANCH}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data.get("content", "") or "").decode("utf-8", errors="replace")
            return True, content, data.get("sha", "") or ""
    except Exception as e:
        log.warning(f"Contents GET failed for '{path}': {e}")
    return False, "", ""


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


def _update_file(path: str, content: str, message: str, sha: str) -> bool:
    """
    Overwrite an existing file (PUT WITH its blob sha). Used only to refresh
    a stale-format book page in place. Returns True on success. Deliberately
    separate from _create_file so overwriting is always an explicit choice —
    only version-gated republish (below) ever calls it.
    """
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": GITHUB_BRANCH,
            "sha": sha,
        }
        r = httpx.put(_contents_url(path), headers=_HEADERS, json=payload, timeout=10.0)
        if r.status_code in (200, 201):
            return True
        log.warning(f"Contents update PUT failed ({r.status_code}) for '{path}': {r.text[:200]}")
    except Exception as e:
        log.warning(f"Contents update PUT failed for '{path}': {e}")
    return False


_CONTENT_VERSION_RE = re.compile(r"^content_version:\s*(\d+)", re.MULTILINE)


def _page_content_version(markdown: str) -> int:
    """Parse `content_version:` from a page's front-matter. Absent → 1 (the
    original pre-versioning format)."""
    m = _CONTENT_VERSION_RE.search(markdown or "")
    return int(m.group(1)) if m else 1


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

    # Quiz — same verified pipeline as POST /quiz/book, run once here so the
    # static page never needs a live API call. Gutenberg/Fandom-grounded
    # only (see tools.quiz.generate_static_quiz); no grounding text → None,
    # and the static page simply omits the Quiz section.
    quiz_questions, quiz_source = None, None
    try:
        from tools.quiz import generate_static_quiz
        quiz_result = generate_static_quiz(result.get("title") or "", result.get("free_ebook"),
                                           categories=categories)
        if quiz_result:
            quiz_questions, quiz_source = quiz_result["questions"], quiz_result["source"]
    except Exception as e:
        log.warning(f"Static quiz generation failed for '{result.get('title')}': {e}")

    lines = [
        "---",
        "layout: book",
        # Content-format version — drives version-gated republish of stale
        # pages (see PUBLISH_CONTENT_VERSION / publish_book).
        f"content_version: {PUBLISH_CONTENT_VERSION}",
        f"title: {_yaml_str(result.get('title'))}",
        f"author: {_yaml_str(result.get('author'))}",
        f"author_slug: {_yaml_str(a_slug)}",
        f"slug: {_yaml_str(book_slug)}",
        # Edition-independent key for community data (ratings/comments/recs) —
        # NOT the URL slug. Falls back to the URL slug if the API response
        # predates the field. See tools/summary.py _canonical_id_from.
        f"canonical_id: {_yaml_str(result.get('canonical_id') or book_slug)}",
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
        # Deferred-indexing policy: generated pages ship noindex by default
        # (Jekyll collection defaults in the bookhub repo). Pages that offer
        # a FREE readable/downloadable book carry standalone user value
        # beyond the AI text, so they earn indexing immediately — page-level
        # front matter overrides the collection default.
        # PROJECT GUTENBERG ONLY: Internet Archive "free" books are scanned
        # page images (no real text/download value), so they don't qualify.
        *(["noindex: false", "sitemap: true"]
          if (result.get("free_ebook") or {}).get("source") == "project_gutenberg"
          else []),
        f"quotes: {_yaml_json(result.get('quotes'))}",
        f"quiz: {_yaml_json(quiz_questions)}",
        f"quiz_source: {_yaml_str(quiz_source)}",
        f"nyt: {_yaml_json(result.get('nyt'))}",
        f"editions: {_yaml_json(result.get('editions'))}",
        "characters: " + _yaml_json([
            {"name": c.get("name"), "slug": c.get("slug"), "role": c.get("role") or ""}
            for c in (result.get("characters") or []) if c.get("slug")
        ]),
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

def _fetch_wikipedia_author(name: str, gate_re=_WRITER_RE) -> dict:
    """
    Wikipedia REST page summary for the author. Accepted ONLY if the page's
    short description matches gate_re (default: the person is a writer — the
    gate against grabbing a same-named athlete/politician). Pass gate_re=None
    to skip the gate when provenance is already verified upstream (e.g.
    character pages arriving via a Wikidata P674 claim's own enwiki
    sitelink — the exact-title link IS the confidence signal there).
    Returns {} when no qualifying page.
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
        if gate_re is not None and not gate_re.search(desc) and not gate_re.search(extract[:200]):
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

        # Dedupe layer 1: Redis flags — but only trust a flag that records a
        # content version AT LEAST the current one. A stale-version (or pre-
        # versioning) flag falls through to the repo check, which rewrites.
        if gid and _flag_is_current(cache.get_key(f"published_gid:{gid}")):
            return
        if _flag_is_current(cache.get_key(f"published:{book_slug}")):
            return

        a_slug = slug_mod.author_slug(result.get("author") or "")

        # Dedupe layer 2 + collision handling: the repo itself.
        path = f"_books/{book_slug}.md"
        exists, content, sha = _file_exists(path)
        if exists:
            if (gid and gid in content) or _same_book(content, title, a_slug):
                # Same book already published. Refresh it in place only if its
                # content format is older than the current version; otherwise
                # it's up to date.
                if _page_content_version(content) < PUBLISH_CONTENT_VERSION:
                    md = _book_markdown(result, book_slug, a_slug)
                    if _update_file(path, md,
                                    f"Refresh book page to v{PUBLISH_CONTENT_VERSION}: {title}", sha):
                        log.info(f"Refreshed stale book page ({path}) to v{PUBLISH_CONTENT_VERSION}")
                _mark_published(book_slug, gid, a_slug)
                return
            # Different book shares the slug — suffix with author, then -2.
            for candidate in (f"{book_slug}-{a_slug}", f"{book_slug}-2"):
                if not candidate:
                    continue
                c_exists, c_content, c_sha = _file_exists(f"_books/{candidate}.md")
                if c_exists:
                    if (gid and gid in c_content) or _same_book(c_content, title, a_slug):
                        if _page_content_version(c_content) < PUBLISH_CONTENT_VERSION:
                            md = _book_markdown(result, candidate, a_slug)
                            if _update_file(f"_books/{candidate}.md", md,
                                            f"Refresh book page to v{PUBLISH_CONTENT_VERSION}: {title}", c_sha):
                                log.info(f"Refreshed stale book page (_books/{candidate}.md)")
                        _mark_published(candidate, gid, a_slug)
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
        _mark_published(book_slug, gid, a_slug)  # set flags even on benign-skip
    except Exception as e:
        log.warning(f"publish_book failed (non-fatal): {e}")


def _flag_is_current(flag) -> bool:
    """A published:* / published_gid:* flag counts as 'done' only if it records
    a content version >= the current one. Absent flag or a pre-versioning flag
    (no 'v', treated as 1) is stale → allow the repo check to refresh."""
    return isinstance(flag, dict) and flag.get("v", 1) >= PUBLISH_CONTENT_VERSION


# ── Published-books index (single Redis key) ─────────────────
# Consumed by /search result annotation: ONE cache read per search instead
# of per-result flag lookups (Upstash free tier has a daily command quota).
# Maps the ACTUAL published slug -> {"g": gid, "a": author_slug, "ts": ...,
# "r": known-ready}. Read-modify-write races between concurrent publishes
# can drop an entry — benign: it self-heals on that book's next publish or
# summary view, and a missing entry only means a dynamic link (never a
# wrong one).
_PUBLISHED_INDEX_KEY = "published_books_v1"
_PUBLISHED_INDEX_TTL = 60 * 60 * 24 * 180


def _index_published(slug: str, gid: str, a_slug: str, ready: bool = False) -> None:
    try:
        idx = cache.get_key(_PUBLISHED_INDEX_KEY)
        if not isinstance(idx, dict):
            idx = {}
        entry = {"g": gid or "", "a": a_slug or "",
                 "ts": datetime.now(timezone.utc).timestamp()}
        if ready:
            entry["r"] = True
        idx[slug] = entry
        cache.set_key(_PUBLISHED_INDEX_KEY, idx, ttl=_PUBLISHED_INDEX_TTL)
    except Exception as e:
        log.warning(f"published-index upsert failed (non-fatal): {e}")


def index_published_ready(slug: str, gid: str, a_slug: str) -> None:
    """Backfill hook for pages published before the index existed: called
    from the summary route whenever resolve_published() confirms a page is
    live, so older pages enter the index organically as they're viewed."""
    _index_published(slug, gid, a_slug, ready=True)


def _entry_ready(entry: dict) -> bool:
    """Same >5-minute GitHub Pages rebuild buffer as resolve_published."""
    if entry.get("r"):
        return True
    now = datetime.now(timezone.utc).timestamp()
    return (now - (entry.get("ts") or 0)) > 300


def static_urls_for_results(results: list[dict], max_flag_checks: int = 12) -> dict[int, str]:
    """
    For a /search result list, returns {result_index: "/summary/<slug>/"}
    for results whose static page is confirmably live. COLLISION-SAFE BY
    DESIGN: two different books can share a title (and therefore a base
    slug — see publish_book's suffixing), so a result is annotated ONLY on
    a positive identity match — the stored gid equals the result's
    google_id/ISBN, or the stored author_slug equals the result's author
    (full-credits or primary-author form, since search rows may carry
    translator co-credits). Anything ambiguous stays on the dynamic link,
    which resolves the exact edition by full identifiers: a slower right
    link beats a fast wrong one.

    Cost: one index read (L1-cached) + at most max_flag_checks legacy flag
    reads for early results the index doesn't know yet (pages published
    before the index existed).
    """
    out: dict[int, str] = {}
    try:
        import slug as slug_mod
        idx = cache.get_key(_PUBLISHED_INDEX_KEY)
        if not isinstance(idx, dict):
            idx = {}
        flag_budget = max_flag_checks
        for i, r in enumerate(results):
            title = (r.get("title") or "").strip()
            author = (r.get("author") or "").strip()
            if not title:
                continue
            base = slug_mod.book_slug(title)
            if not base:
                continue
            row_gids = {g for g in (r.get("google_id"), r.get("isbn_13"), r.get("isbn_10")) if g}
            row_a_full = slug_mod.author_slug(author)
            row_a_primary = slug_mod.author_slug(author.split(",")[0].strip())
            row_authors = {a for a in (row_a_full, row_a_primary) if a}
            candidates = [base] + [f"{base}-{a}" for a in row_authors]

            matched = None
            for slug_c in dict.fromkeys(candidates):
                entry = idx.get(slug_c)
                if isinstance(entry, dict) and _entry_ready(entry):
                    gid_ok = entry.get("g") and entry["g"] in row_gids
                    author_ok = entry.get("a") and entry["a"] in row_authors
                    if gid_ok or author_ok:
                        matched = slug_c
                        break
            if not matched and flag_budget > 0:
                # Legacy fallback: flags written before the index existed.
                # Old flags carry only gid — identity via gid alone there.
                for slug_c in dict.fromkeys(candidates):
                    flag_budget -= 1
                    flag = cache.get_key(f"published:{slug_c}")
                    if isinstance(flag, dict) and _entry_ready(flag):
                        gid_ok = flag.get("gid") and flag["gid"] in row_gids
                        author_ok = flag.get("a") and flag["a"] in row_authors
                        if gid_ok or author_ok:
                            matched = slug_c
                            break
                    if flag_budget <= 0:
                        break
            if matched:
                out[i] = f"/summary/{matched}/"
    except Exception as e:
        log.warning(f"static_urls_for_results failed (non-fatal): {e}")
    return out


def _same_book(content: str, title: str, a_slug: str) -> bool:
    """Is an existing page the SAME book (not just a slug collision)? Matches
    when its front-matter title + author_slug both equal this book's — the
    reliable signal when there's no google/isbn id to compare (front-matter
    values are json.dumps'd, so json.loads round-trips them)."""
    try:
        tm = re.search(r'^title:\s*(.+)$', content, re.MULTILINE)
        am = re.search(r'^author_slug:\s*(.+)$', content, re.MULTILINE)
        t = json.loads(tm.group(1)) if tm else ""
        a = json.loads(am.group(1)) if am else ""
        return bool(t) and t.strip().lower() == (title or "").strip().lower() and a == a_slug
    except Exception:
        return False


def _mark_published(book_slug: str, gid: str, a_slug: str = "") -> None:
    ts = datetime.now(timezone.utc).timestamp()
    v = PUBLISH_CONTENT_VERSION
    # "a" (author_slug) joined the flags for /search static-link identity
    # checks — two books sharing a title must never swap static pages.
    cache.set_key(f"published:{book_slug}", {"gid": gid, "ts": ts, "v": v, "a": a_slug})
    if gid:
        cache.set_key(f"published_gid:{gid}", {"slug": book_slug, "ts": ts, "v": v, "a": a_slug})
    _index_published(book_slug, gid, a_slug)


def _character_markdown(character: dict, photo_url: str, wikipedia_url: str,
                        bio: str, books: list[dict]) -> str:
    lines = [
        "---",
        "layout: character",
        f"name: {_yaml_str(character.get('name'))}",
        f"slug: {_yaml_str(character.get('slug'))}",
        f"role: {_yaml_str(character.get('role') or '')}",
        f"source: {_yaml_str(character.get('source') or '')}",
        f"photo_url: {_yaml_str(photo_url)}",
        f"wikipedia_url: {_yaml_str(wikipedia_url)}",
        f"books: {_yaml_json(books)}",
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z')}",
        "---",
        "",
        bio,
        "",
    ]
    return "\n".join(lines)


def publish_character(character: dict, book_title: str, book_slug: str) -> None:
    """
    Commits _characters/<slug>.md the first time a character appears.
    Mirrors publish_author exactly (dedupe flags, file-exists gate, never
    raises — runs in BackgroundTasks). Known v1 limitation, same as
    authors: publish-once — a recurring character's "books" list freezes
    at first publish.

    Grounding per source:
    - wikidata: Wikipedia extract (fetched by the P674 sitelink's exact
      title, no text gate needed — the claim IS the provenance) rewritten
      by Gemini strictly from that extract.
    - fandom:  the wiki-extracted description is used as-is (already
      extraction-only, never invented — see fandom.py's prompt contract).
    """
    try:
        if not is_enabled():
            return
        name = (character.get("name") or "").strip()
        c_slug = character.get("slug") or slug_mod.character_slug(name)
        if not name or not c_slug:
            return

        if cache.get_key(f"published_character:{c_slug}"):
            return
        path = f"_characters/{c_slug}.md"
        exists, _, _ = _file_exists(path)
        if exists:
            cache.set_key(f"published_character:{c_slug}", {"ts": datetime.now(timezone.utc).timestamp()})
            return

        photo_url, wikipedia_url, bio = "", "", ""
        if character.get("source") == "wikidata" and character.get("wikipedia_title"):
            wiki = _fetch_wikipedia_author(character["wikipedia_title"], gate_re=None)
            if wiki.get("extract"):
                bio = _write_character_bio(name, book_title, wiki["extract"])
                photo_url = wiki.get("photo_url", "")
                wikipedia_url = wiki.get("wikipedia_url", "")
        if not bio:
            # Fandom description (extraction-only) or the neutral template —
            # never an invented bio.
            bio = (character.get("description") or "").strip() \
                or f"{name} is a character in {book_title}."

        books = [{"title": book_title, "slug": book_slug}]
        md = _character_markdown(character, photo_url, wikipedia_url, bio, books)
        if _create_file(path, md, f"Add character page: {name}"):
            log.info(f"Published character page: {path}")
        cache.set_key(f"published_character:{c_slug}", {"ts": datetime.now(timezone.utc).timestamp()})
    except Exception as e:
        log.warning(f"publish_character failed (non-fatal): {e}")


def _write_character_bio(name: str, book_title: str, extract: str) -> str:
    """Gemini rewrite grounded STRICTLY on the Wikipedia extract."""
    import gemini_client
    prompt = f"""Rewrite the following Wikipedia extract as a clean 1-2 paragraph description of the fictional character {name} (from "{book_title}") for a book website. Use ONLY facts present in the extract — do not add anything from outside it. Plain text, no markdown, no headings.

EXTRACT:
\"\"\"{extract[:2000]}\"\"\""""
    try:
        return gemini_client.generate(prompt).strip()
    except Exception as e:
        log.warning(f"Character bio generation failed for '{name}': {e}")
        return ""


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
        exists, _, _ = _file_exists(path)
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
