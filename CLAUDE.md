# CLAUDE.md

Persistent instructions for AI coding agents working in this repo. Read
this before making changes — it encodes conventions and hard-won fixes
from real incidents, not aspirations.

## What this is

FastAPI backend powering **BookHub**, a set of AI-grounded book tools
(summaries, quizzes, character pages, etc.). Deployed on **Render's free
tier** at `bookhub-api-hnv7.onrender.com`. Every AI-facing feature follows
one rule above all others:

> **No data beats wrong data.** Every resolver either returns real,
> verified information or returns `None`/`[]`/absent — never a guess. The
> frontend hides the corresponding UI section when the field is empty.
> This applies to book facts, awards, ratings, quotes, characters, free-
> ebook links, quiz questions — everything. If you're tempted to have
> Gemini "fill in" something it isn't grounded on, stop and return `None`
> instead.

## The two-repo pair — read this first

BookHub is **two separate git repos that must be edited together** for
most features:
- **`bookhub-api`** (this repo) — the Python backend, source of truth for
  data and logic.
- **`bookhub`** — the Jekyll static site (GitHub Pages), sibling directory
  at `../bookhub` (or `E:\GitHub\bookhub` on this machine). Frontend HTML/
  JS, `_layouts/`, static-page collections, and `site.api_url` in
  `_config.yml` pointing back at this API.

**A new backend field is invisible until the frontend renders it.** When
you add something to a `/summary` response, you almost always need a
matching edit in `bookhub/summary.html` (dynamic page) AND
`bookhub/_layouts/book.html` (static published page) — check both, they
are two independent templates that must stay in sync by hand (no shared
partial/build step between the repos).

## Architecture reality check

`README.md` in this repo is **stale** — written for an early version with
only `/summary` + `/health` and a "tools never import each other" rule.
Neither is true anymore. Current reality:

```
main.py                 # mounts routers from tools/*.py (summary, fandom,
                         # daily, pdfchat, nyt, reader, quiz)
book_data.py             # SHARED grounding: Google Books → Open Library fallback
gemini_client.py         # SHARED Gemini client (gemini-3.1-flash-lite, temp 0.3)
cache.py                 # SHARED cache: in-memory L1 + Upstash Redis L2
github_publisher.py       # SHARED: publishes static pages to the bookhub repo
slug.py                   # SHARED: URL slug generation (book/author/character)
tools/
  summary.py               # the core tool — orchestrates ~13 concurrent resolvers
  fandom.py                 # Fandom-wiki chapter/character/quiz-text extraction
  fandom_catalog.py          # hand-verified per-series config for web novels
  reader.py                  # in-site public-domain reader (Gutenberg text)
  quiz_core.py                # shared quiz generation, used by pdfchat.py AND quiz.py
  quiz.py                     # POST /quiz/book (Gutenberg/Fandom-grounded quizzes)
  pdfchat.py                   # PDF Chat & Quiz (imports from quiz_core.py)
  nyt.py                       # NYT Books API integration
  daily.py                     # homepage daily picks (book/author/quote)
```

Tools mostly stay self-contained, but **this is no longer an absolute
rule** — `quiz_core.py` is deliberately shared between `pdfchat.py` and
`quiz.py` to avoid duplicating the quote-verification pipeline, and
`summary.py` imports directly from `fandom.py`, `reader.py`, and `nyt.py`
where the reuse is real. Prefer sharing a well-tested function over
copy-pasting it — but don't casually couple unrelated tools either.

## The four storage layers — know which one you're touching

1. **Render** — compute only. No persistent disk. Restarting/redeploying
   wipes anything not in one of the layers below.
2. **Upstash Redis** (via `cache.py`) — the actual "cache." Everything
   here has a TTL and can vanish; every feature must tolerate a cold
   cache. Free tier caps: ~1MB per REST request (chunk large payloads —
   see `pdfchat.py`'s block storage) and a daily command quota.
3. **Firebase** (Firestore + Auth) — user accounts, comments, ratings,
   likes, synced library. **This backend never touches Firebase** — it's
   entirely client-side JS in the `bookhub` repo talking to Firestore
   directly, authorized by `bookhub/firebase/firestore.rules`.
4. **The `bookhub` GitHub repo itself** (via `github_publisher.py`) — the
   only **permanent** storage in the whole system. Committed
   `_books/*.md` / `_authors/*.md` / `_characters/*.md` files never
   expire. Publishing is create-only and deduped (see that module's
   docstring) — a page is never overwritten after first publish. This is
   a known v1 limitation (e.g. a character's "appears in" list freezes at
   first publish) — don't silently work around it with update-in-place
   logic without discussing it; it risks clobbering a Gemini-written bio.

## Cache versioning discipline

Cache keys are tuples: `cache.get(*key)` / `cache.set(value, *key)`, first
element a version string, e.g. `("summary_v13", title, author, ...)`.

**Whenever you change what a cached value contains or how it's computed,
bump the version number** (`_v13` → `_v14`) and leave a one-line comment
explaining why, appended to the existing history comment above the key
(see `tools/summary.py`'s `cache_key` for the full changelog — don't
delete old entries, they're the audit trail). Old-version entries are
simply orphaned, not migrated — this is intentional; TTLs clean them up.

**Self-heal pattern**: when a cached `/summary` response predates a field
you're adding, backfill it on next read rather than forcing full
regeneration. Look at the self-heal block in `tools/summary.py`'s
`/summary` route for the exact pattern (check `"field" not in cached` for
brand-new keys vs. `cached.get("field") is None` for fields that already
existed but might legitimately be null — these are different checks with
different cost implications, see the comment there for why).

## Concurrency pattern

`_gather_extras()` in `tools/summary.py` runs ~13 independent resolvers
via `ThreadPoolExecutor`. **Every closure in that task dict must be
self-contained and never raise** — wrap risky calls in try/except
internally, or wrap the whole closure. One resolver's failure (a
timeout, a malformed API response) must never break the other 12 or the
overall response. When adding a new resolver, follow the existing
closures' shape exactly (e.g. `get_characters`, `get_nyt`) rather than
inventing a new pattern.

## Known infrastructure gotchas (learned the hard way)

- **Render free tier has no cron/worker dyno.** Scheduled jobs (cache
  warming, etc.) run via **GitHub Actions** hitting HTTP endpoints
  (`.github/workflows/warm-daily.yml`), not anything on Render itself.
- **Render sleeps after ~15 min idle** — cold start is 30-60s. The
  warm-daily workflow pings `/health` first to wake the instance before
  hitting real endpoints.
- **Render's GitHub deploy webhook can silently stop delivering** even
  with auto-deploy set to "On Commit" — this happened and cost two days
  of stale deploys before being noticed. `.github/workflows/
  trigger-render-deploy.yml` is a hard fallback: every push to `main`
  also curls Render's Deploy Hook URL directly (via the
  `RENDER_DEPLOY_HOOK` repo secret). If a push doesn't seem to be live,
  check this workflow's run status before assuming the code is wrong.
- **Some third-party APIs block Render's datacenter IP** even though they
  work fine from a residential connection or from Render's own build
  logs during local testing — confirmed for Gutendex (Project
  Gutenberg's own JSON API), which returns 403 from Render but 200
  locally. Open Library was used as a working substitute (see
  `resolve_free_ebook` in `tools/summary.py`). **Always verify a new
  external API integration against the live Render deployment**, not
  just local runs — a local success proves nothing about IP-based
  blocking.
- **No `python-dotenv` loading anywhere** despite it being in
  `requirements.txt` — env vars come from Render's dashboard in
  production. For local smoke tests against real APIs, manually parse
  `.env` in your test snippet (`for line in open('.env')...`) rather
  than assuming `load_dotenv()` runs.

## Testing workflow (follow this before every push)

1. **Syntax check**: `python -c "import ast; ast.parse(open('tools/whatever.py').read())"`
2. **Real smoke test**: call the new/changed function directly with real
   book titles and real API calls (not mocks) — this codebase has
   repeatedly caught real bugs this way (wrong data shapes, silently
   blocked APIs, empty results) that a mocked test would have missed.
   Print actual output and eyeball it.
3. **Import check**: `import main` to confirm router wiring doesn't
   crash the whole app (a bad import in one `tools/` module breaks
   everything, since `main.py` imports all of them eagerly).
4. Push, then verify the live Render deployment actually reflects the
   change (see the webhook gotcha above) before starting the next step.

Ship **one commit + push per logical step**, not one giant batch — this
repo's history is full of tightly-scoped commits with a "why" in the
message, not just a "what." Follow that pattern.

## Grounding conventions specific to this codebase

- A book must be verified via `book_data.py` (Google Books → Open Library
  fallback) before any Gemini call runs. Never prompt Gemini about a book
  "from memory" alone.
- Gemini prompts that extract/summarize from supplied text must
  explicitly instruct the model to return nothing (empty list, `null`,
  `"confident": false`) rather than invent plausible-looking content when
  the source text doesn't clearly contain what's being asked for. Search
  `tools/fandom.py` for `_build_character_extraction_prompt` and
  `_build_chapter_extraction_prompt` for the exact phrasing pattern to
  reuse.
- Quiz questions (`quiz_core.py`) must carry a `supporting_quote` that is
  **verified via normalized substring match** against the actual source
  text before being shown — this is the anti-hallucination mechanism for
  quizzes specifically; don't bypass it.

## Cover images — where they may come from

Verified against each provider's own terms, 2026-08-28.

**Never copy a cover into either repo.** Every book cover on the site is
**hotlinked** — the browser fetches it from the provider, we store only the
URL. That is the whole reason the licensing position is comfortable, so do
not "fix" a slow-loading cover by downloading and committing it.

Allowed sources, in order of preference:

1. **Open Library** — `covers.openlibrary.org/b/id/{cover_i}-M.jpg`. Open
   Library explicitly provides this API to "display covers on public-facing
   pages" and asks only for a courtesy link back. `/b/id/` (CoverID) is
   **not** rate-limited; lookups by ISBN and other ids are (100/IP/5min).
2. **Google Books** — the `imageLinks` thumbnail the API itself returns.
   Google's API terms forbid "permanent copies … or keep cached copies longer
   than permitted by the cache header", which is another reason the image
   itself is never stored: hotlinking keeps us on the right side of it.
3. **Nothing.** `cover_url` is `Optional` everywhere it is read and every
   template guards it. A book with no cover renders fine.

**Forbidden: any Fandom wiki image** (`*.wikia.nocookie.net`,
`*.fandom.com`). A wiki's TEXT is CC-BY-SA; its **images are not**. Fandom's
own help pages state that non-text media does not inherit the wiki licence,
that most images are user uploads under a fair-use rationale, that Fandom
"is unable to either give or deny permission for their reuse", and that they
run no licence verification. A fair-use claim for an encyclopedic wiki does
not transfer to this site. Two such entries existed and were removed
2026-08-28; `tools/fandom_admin.py`'s approve endpoint now refuses them with
a 400, because the review card's editable `cover_url` field sits next to the
very wiki page whose image is easiest to grab.

Never substitute a lookalike cover from a different edition or a different
book to fill a gap — that is the same "no data beats wrong data" rule this
file opens with, applied to images.

**Project Gutenberg** images (the only ones actually committed, in
`bookhub/assets/games/gtb/`) are fine: the texts are US public domain and
need no permission. Keep crediting them as the puzzle JSON already does —
and keep that credit as *attribution*, never styled as an endorsement, since
"Project Gutenberg" is a trademark and its licence has terms for trading on
the name.

**Amazon** is links only, never images (their PA-API image terms are far
stricter). The "As an Amazon Associate…" disclosure must stay next to the
link itself — it is in `_layouts/book.html`, not only on `/about` — and
links keep `rel="noopener sponsored"`.

## Sibling repo

Static site: `../bookhub` (Jekyll, GitHub Pages, auto-deploys from
`main` — no webhook fallback needed there, unlike this repo). Its own
`CLAUDE.md` covers frontend/Firebase conventions.
