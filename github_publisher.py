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
from html import escape as _html_escape
from html.parser import HTMLParser

import httpx

import cache
import slug as slug_mod

log = logging.getLogger("bookhub-api.github_publisher")

GITHUB_API = "https://api.github.com"
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "mokhhtar/bookhub")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PUBLISH_ENABLED = os.environ.get("GITHUB_PUBLISH_ENABLED", "false").lower() == "true"
# Pre-launch stance: publish pages but grant NO new indexing. Suppresses the
# v3 free-ebook rule below for pages being created; it never demotes a page
# that is already indexed (see _carried_index_state). Flip to false at launch,
# then run a promotion pass — clearing the flag alone won't retro-index
# anything, since an up-to-date page is never rewritten.
DEFER_INDEXING = os.environ.get("DEFER_INDEXING", "false").lower() == "true"

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
# v5: lets a catalog mistyping reach pages that already exist. Publishing is
# create-only and the Redis flag short-circuits before the repo is consulted,
# so "Hg Wells" and "WHITE FANG / JACK LONDON" were frozen onto public pages
# with no route to a correction. Safe to bump only because a republish now
# carries the page's indexing state (_carried_index_state) and its original
# date (_carried_date) forward instead of regenerating both — without those,
# this bump would have silently demoted indexed pages and re-dated all 68.
# v6: the Wikiquote resolver was picking DISAMBIGUATION pages, so published
# pages carry film credits under "Notable Quotes" — "Dracula, the 1897 novel
# by Bram Stoker…", "a film directed by Tod Browning…" — beside a line saying
# the text is sourced verbatim and not AI-generated. Two of them were already
# promoted into Google. Fixing the resolver healed the API response but not
# the committed page, because publishing is create-only and those pages sit at
# the current version. This bump is the only route to the file itself.
# v7: same resolver, the next layer down. `redirects=1` was followed blindly,
# so a title that redirects elsewhere on Wikiquote published THAT page's
# quotes: Kidnapped's five are about crime as a topic, and eight more books
# carry their AUTHOR's quotes — "Around the World in Eighty Days" and "The
# Mysterious Island" show the same five, in French. 17 of the 60 committed
# pages with quotes are affected. Unlike v6's film credits, these texts read
# as ordinary quotations, so nothing on the page reveals them; re-resolving is
# the only thing that can.
# v8: v7's landing-page test stripped the parenthetical qualifier so a work
# could match its own adaptation — "Little Women" landed on "Little Women
# (2019 film)" and that page published Gerwig's screenplay as Alcott's prose.
# v9: the free-ebook resolver trusted Open Library's ebook_access for Archive
# scans. It called a lending copy "public", so Huckleberry Finn's page linked
# an item that answers 401 to a download, under the heading "Public domain.
# Free to download". Published pages carry that link until rewritten.
# v10: Archive-sourced pages gained a verified PDF download. Publishing is
# create-only, so the seven pages that can carry one only get it when the
# file is rewritten.
# v11: two quiz corrections reach the committed pages. Three books whose quiz
# came from a fan wiki turn out to have their real Gutenberg text available —
# Les Miserables, Peter Pan, The Count of Monte Cristo — and a quiz from the
# book itself beats one from a summary of it with no trade at all. The other
# five have no text, and their wiki pages are now filtered for ones that
# declare they also cover a screen adaptation: V for Vendetta's quiz asked
# about a television show that exists only in the 2006 film.
PUBLISH_CONTENT_VERSION = 11


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
_FREE_EBOOK_LINE_RE = re.compile(r"^free_ebook: (\{.*\})\s*$", re.MULTILINE)
_QUOTES_LINE_RE = re.compile(r"^quotes: (\{.*\})\s*$", re.MULTILINE)

# Every versioned payload a page bakes in, so adding the NEXT one is a line
# here rather than a fifth rediscovery of the same bug. `quotes` was the
# fourth sighting: the staleness test had been generalised past
# content_version for free_ebook alone, so a page at the current
# content_version holding quotes from the wrong work was frozen exactly the
# way Peter Pan was. Five published pages were — Jane Eyre carried lyrics
# from "Jane Eyre: The Musical", Persuasion carried a modern political
# quotation about persuasion the rhetorical topic, and both pages read as
# current to every check we had.
_VERSIONED_PAYLOADS = (
    ("free_ebook", _FREE_EBOOK_LINE_RE, "_FREE_EBOOK_PAYLOAD_VERSION"),
    ("quotes", _QUOTES_LINE_RE, "_WQ_PAYLOAD_VERSION"),
)


def _page_content_version(markdown: str) -> int:
    """Parse `content_version:` from a page's front-matter. Absent → 1 (the
    original pre-versioning format)."""
    m = _CONTENT_VERSION_RE.search(markdown or "")
    return int(m.group(1)) if m else 1


def _current_payload_version(name: str) -> int:
    # Lazy: tools.summary imports this module, so a module-level import here
    # would be circular.
    import tools.summary as summary
    return getattr(summary, name)


def _free_ebook_payload_version() -> int:
    return _current_payload_version("_FREE_EBOOK_PAYLOAD_VERSION")


def _payload_version_of(free_ebook) -> int:
    """The free_ebook payload version a page (or a /summary result) was built
    from. No free_ebook at all is not staleness — there is no payload to be
    out of date — so it counts as current."""
    if not isinstance(free_ebook, dict):
        return _free_ebook_payload_version()
    return int(free_ebook.get("v", 0) or 0)


def _published_payload_versions(result: dict) -> dict:
    """The version each versioned payload in this /summary result carries.

    A field that is absent or null counts as CURRENT — there is no payload to
    be out of date — which is the same rule _payload_version_of applies, and
    the reason a book with no free ebook is not republished forever."""
    out = {}
    for name, _line_re, attr in _VERSIONED_PAYLOADS:
        payload = result.get(name)
        out[name] = (int(payload.get("v", 0) or 0) if isinstance(payload, dict)
                     else _current_payload_version(attr))
    return out


def _page_is_stale(markdown: str) -> bool:
    """Whether a published page needs rewriting.

    content_version alone is not the test, because it records the FORMAT the
    page was written in, not whether what was written was right. A page
    refreshed in the window between a resolver fix and the cache heal that
    makes it take gets stamped with the current version while still holding
    the old payload — and is then frozen forever, since every later check
    reads the stamp and stops. Peter Pan sat at v11 carrying the poisoned v2
    internet_archive ebook for exactly that reason: correct format, wrong book.

    So the payload's own version is part of the question. This is the same
    shape as the sub-cache bug underneath it (tools/summary.py's `force`): a
    freshness check that consults a proxy for the data instead of the data.

    Asked of EVERY versioned payload, not just free_ebook — see
    _VERSIONED_PAYLOADS. Checking one of them was the same mistake one field
    over, and it froze five pages holding another work's quotations.
    """
    if _page_content_version(markdown) < PUBLISH_CONTENT_VERSION:
        return True
    for _name, line_re, version_attr in _VERSIONED_PAYLOADS:
        m = line_re.search(markdown or "")
        if not m:
            continue           # `null` or absent — no payload to be out of date
        try:
            payload = json.loads(m.group(1))
        except Exception:
            return True        # unparseable: rewriting regenerates the line
        if not isinstance(payload, dict):
            continue
        if int(payload.get("v", 0) or 0) < _current_payload_version(version_attr):
            return True
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


# ── Summary-HTML sanitizer (persistent-page XSS boundary) ────
# The summary body is model/backend-generated HTML written RAW into the
# published _books/*.md (kramdown passes block HTML straight through) and
# rendered on the static page with no client-side sanitizer. So an injected
# <script>/<img onerror>/<svg onload> reaching a summary would bake a
# permanent stored-XSS into a public page. Mirror the frontend's DOMPurify
# boundary (summary.html sanitizeHtml): allow ONLY the closed formatting
# vocabulary the summaries actually use — verified against every _books/*.md:
# p, strong, li, h2, h3, em, ul (+ br/ol/b/i as harmless supersets) — and
# emit ZERO attributes, so every event handler / url attribute is dropped.
# Stdlib-only (no new Render dependency); safe precisely because the tag set
# is closed and attribute-free.
_ALLOWED_HTML_TAGS = {"h2", "h3", "p", "ul", "ol", "li", "strong", "em", "b", "i", "br"}
_VOID_HTML_TAGS = {"br"}


class _SummaryHTMLSanitizer(HTMLParser):
    """Keeps allow-listed tags (attribute-free) and escapes all text; drops
    everything else. Text of a disallowed tag (e.g. <script>alert()</script>)
    survives only as escaped, inert text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _ALLOWED_HTML_TAGS:
            self._out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag in _ALLOWED_HTML_TAGS:
            self._out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _ALLOWED_HTML_TAGS and tag not in _VOID_HTML_TAGS:
            self._out.append(f"</{tag}>")

    def handle_data(self, data):
        self._out.append(_html_escape(data, quote=False))

    def handle_comment(self, data):
        pass  # drop comments (a classic mXSS vector)

    def result(self) -> str:
        return "".join(self._out)


def _sanitize_summary_html(html: str) -> str:
    if not html:
        return ""
    try:
        p = _SummaryHTMLSanitizer()
        p.feed(html)
        p.close()
        return p.result()
    except Exception as e:
        # Fail CLOSED: on any parser trouble, never emit raw HTML — strip to
        # escaped plain text rather than risk baking unsanitized markup.
        log.warning(f"summary-HTML sanitize failed, falling back to text: {e}")
        return _html_escape(_strip_tags(html), quote=False)


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


_INDEX_LINE_RE = re.compile(r"^(noindex|sitemap):\s*(true|false)\s*$", re.MULTILINE)


def _carried_index_state(existing_markdown: str | None) -> list[str] | None:
    """The indexing lines an already-published page is carrying, or None if it
    carries none (and so should be decided fresh).

    Republish regenerates the WHOLE front matter from _book_markdown, which
    only ever knew the free-ebook rule. That silently destroys any indexing a
    page earned some other way — and there are two such ways:
      · tools/indexing.py promotes an engaged page by surgically inserting
        noindex:false. The next version bump wiped it. The second half of the
        indexing policy was being erased every time we shipped a feature.
      · with DEFER_INDEXING on, a page indexed under the old rule would be
        demoted the moment any v-bump rewrote it.
    So a rewrite now carries the existing state forward verbatim. Indexing is
    granted deliberately and removed deliberately — never as a side effect of
    shipping something else.
    """
    if not existing_markdown:
        return None
    found = {key: value for key, value in _INDEX_LINE_RE.findall(existing_markdown)}
    if not found:
        return None
    return [f"{key}: {found[key]}" for key in ("noindex", "sitemap") if key in found]


_DATE_LINE_RE = re.compile(r"^date:\s*(.+?)\s*$", re.MULTILINE)


def _carried_date(existing_markdown: str | None) -> str | None:
    """The `date:` an already-published page carries, or None if it has none.

    A republish is a correction, not a new publication. Regenerating the date
    would push every refreshed page back to the top of the homepage's "Just
    summarized" list, turning it into "most recently rewritten" — which is
    both wrong and the exact clutter a version bump would otherwise cause
    across 68 pages at once."""
    if not existing_markdown:
        return None
    m = _DATE_LINE_RE.search(existing_markdown)
    return m.group(1) if m else None


def _book_markdown(result: dict, book_slug: str, a_slug: str,
                   existing_markdown: str | None = None) -> str:
    # Sanitize at the page boundary — this HTML is written raw into the public
    # static page (see _sanitize_summary_html). description is derived from the
    # sanitized text, which is fine (still plain text after tag-strip).
    summary_html = _sanitize_summary_html(result.get("summary", "") or "")
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
        # A page that ALREADY carries an indexing decision keeps it, whoever
        # made it; DEFER_INDEXING withholds the grant from new pages only.
        *(_carried_index_state(existing_markdown)
          or ([] if DEFER_INDEXING else
              ["noindex: false", "sitemap: true"]
              if (result.get("free_ebook") or {}).get("source") == "project_gutenberg"
              else [])),
        f"quotes: {_yaml_json(result.get('quotes'))}",
        f"quiz: {_yaml_json(quiz_questions)}",
        f"quiz_source: {_yaml_str(quiz_source)}",
        f"nyt: {_yaml_json(result.get('nyt'))}",
        f"editions: {_yaml_json(result.get('editions'))}",
        "characters: " + _yaml_json([
            {"name": c.get("name"), "slug": c.get("slug"), "role": c.get("role") or ""}
            for c in (result.get("characters") or []) if c.get("slug")
        ]),
        f"date: {_carried_date(existing_markdown) or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z')}",
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
        #
        # And only trust it while the response hasn't just proved it wrong.
        # `static_page` is computed by resolve_published from the repo itself
        # on the way out of /summary, so False means there is demonstrably no
        # page. A flag can outlive the page it describes: deleting a page to
        # correct a bad slug left a CURRENT-version flag behind, publish_book
        # returned here every time, and that page could never be recreated —
        # Ivanhoe sat unpublishable while Mutiny on the Bounty, whose flag was
        # a version behind, recovered on the first request.
        trust_flags = result.get("static_page") is not False
        if trust_flags:
            if gid and _flag_is_current(cache.get_key(f"published_gid:{gid}")):
                return
            if _flag_is_current(cache.get_key(f"published:{book_slug}")):
                return

        a_slug = slug_mod.author_slug(result.get("author") or "")
        pv = _published_payload_versions(result)

        # Dedupe layer 2 + collision handling: the repo itself.
        path = f"_books/{book_slug}.md"
        exists, content, sha = _file_exists(path)
        if exists:
            if (gid and gid in content) or _same_book(content, title, a_slug):
                # Same book already published. Refresh it in place only if it
                # is stale — an older content format, OR a current format
                # holding an out-of-date payload (see _page_is_stale).
                if _page_is_stale(content):
                    md = _book_markdown(result, book_slug, a_slug, content)
                    if _update_file(path, md,
                                    f"Refresh book page to v{PUBLISH_CONTENT_VERSION}: {title}", sha):
                        log.info(f"Refreshed stale book page ({path}) to v{PUBLISH_CONTENT_VERSION}")
                _mark_published(book_slug, gid, a_slug, pv)
                return
            # Different book shares the slug — suffix with author, then -2.
            for candidate in (f"{book_slug}-{a_slug}", f"{book_slug}-2"):
                if not candidate:
                    continue
                c_exists, c_content, c_sha = _file_exists(f"_books/{candidate}.md")
                if c_exists:
                    if (gid and gid in c_content) or _same_book(c_content, title, a_slug):
                        if _page_is_stale(c_content):
                            md = _book_markdown(result, candidate, a_slug, c_content)
                            if _update_file(f"_books/{candidate}.md", md,
                                            f"Refresh book page to v{PUBLISH_CONTENT_VERSION}: {title}", c_sha):
                                log.info(f"Refreshed stale book page (_books/{candidate}.md)")
                        _mark_published(candidate, gid, a_slug, pv)
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
        _mark_published(book_slug, gid, a_slug, pv)  # set flags even on benign-skip
    except Exception as e:
        log.warning(f"publish_book failed (non-fatal): {e}")


def _flag_is_current(flag) -> bool:
    """A published:* / published_gid:* flag counts as 'done' only if it records
    a content version >= the current one AND the version of EVERY versioned
    payload the page was published with. Absent flag or a pre-versioning flag
    (no 'v', treated as 1) is stale → allow the repo check to refresh.

    The payload half matters because this flag is checked BEFORE the repo is
    read: without it, a page published from a stale payload short-circuits here
    and _page_is_stale never gets to see it. Flags written before this record
    existed have none, so they are distrusted once — one repo read per page,
    after which the flag carries it and the short-circuit resumes. That sweep
    is the point, not a cost to avoid: it is how the pages already frozen in
    that state get re-examined.

    It recorded free_ebook's version and nothing else, which is why fixing
    _page_is_stale alone left twenty pages still quoting the wrong work: the
    flag said done and the corrected test never ran. Same bug, one layer out,
    for the same reason — third sighting of this shape. Reading the whole of
    _VERSIONED_PAYLOADS is what stops there being a fourth.
    """
    if not (isinstance(flag, dict) and flag.get("v", 1) >= PUBLISH_CONTENT_VERSION):
        return False
    published = flag.get("pv")
    if not isinstance(published, dict):
        return False        # pre-'pv' flag: distrust once, then it carries
    return all(published.get(name, 0) >= _current_payload_version(attr)
               for name, _line_re, attr in _VERSIONED_PAYLOADS)


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


def _mark_published(book_slug: str, gid: str, a_slug: str = "",
                    pv: dict | None = None) -> None:
    ts = datetime.now(timezone.utc).timestamp()
    v = PUBLISH_CONTENT_VERSION
    # "a" (author_slug) joined the flags for /search static-link identity
    # checks — two books sharing a title must never swap static pages.
    # "pv" is the version of every versioned payload the page was written
    # from, so a page built on a stale one cannot be short-circuited as done.
    # It records what was PUBLISHED, never the current constants — writing the
    # constants here would make the flag agree with itself forever.
    entry = {"gid": gid, "ts": ts, "v": v, "a": a_slug, "pv": pv or {}}
    cache.set_key(f"published:{book_slug}", entry)
    if gid:
        cache.set_key(f"published_gid:{gid}", {**entry, "slug": book_slug})
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
