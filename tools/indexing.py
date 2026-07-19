"""
tools/indexing.py — engagement-based indexing promotion for static book pages.

The deferred-indexing policy (see github_publisher.py / the bookhub repo's
collection defaults) ships every generated page with noindex:true unless it
carries a free Gutenberg text. This module is the SECOND half of that
policy: a page that has earned real community engagement gets promoted to
indexable — the engagement is the evidence Google should see it.

Not every interaction counts (owner requirement). Quality gates:
  - A comment counts only if: approved (pre-moderation), length >=
    INDEX_MIN_COMMENT_CHARS, contains no links, one per user.
  - A reader recommendation counts only if: approved, its reason contains
    no links, AND a strict Gemini yes/no judgment says the reason is a
    coherent, clearly-written explanation (refuses when unsure — the
    codebase's usual grounding posture; verdicts are cached per rec so
    re-runs never re-bill).
  - A star rating counts as-is: structurally one per user (doc id == uid)
    and carries no free text to launder spam through.
Promotion rule: qualified items >= INDEX_PROMOTE_THRESHOLD AND distinct
users >= INDEX_PROMOTE_MIN_USERS (one person rating + commenting +
recommending must not be able to promote a page alone).

Data access: Firestore's public REST API (runQuery), UNAUTHENTICATED —
exactly like the site's own client-side JS. The security rules are the
authorization layer: comments/reader_recs queries MUST filter
approved==true (rules deny anything broader), ratings are public-read.
The web API key used is the same one already published in the bookhub
repo's firebase.html (public by design).

The promotion write is a SURGICAL front-matter edit via
github_publisher._update_file — inserts noindex/sitemap/audit lines only,
never regenerates content, so a Gemini-written body can't be clobbered.

Triggered by .github/workflows/promote-indexing.yml (daily cron) — Render
free tier has no cron of its own. Protected by the PROMOTE_SECRET header:
each run can cost Gemini calls (rec-reason judgments) and GitHub API
writes, so it must not be publicly triggerable. ?dry=true evaluates and
reports without writing anything.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Header, HTTPException, Query

import cache
import github_publisher

log = logging.getLogger("bookhub-api.tools.indexing")

router = APIRouter(prefix="/indexing")

# Same public web config as bookhub/_includes/firebase.html.
FIREBASE_PROJECT = os.environ.get("FIREBASE_PROJECT_ID", "bookhub-42d9a")
FIREBASE_WEB_KEY = os.environ.get("FIREBASE_WEB_API_KEY",
                                  "AIzaSyB9nLUuq4e4bxFVlpSX3OwEqeKVzOWhhSs")
_FS_BASE = (f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
            f"/databases/(default)/documents")

PROMOTE_SECRET = os.environ.get("PROMOTE_SECRET", "")

# Tunables — env-overridable so the bar can move without a deploy.
THRESHOLD = int(os.environ.get("INDEX_PROMOTE_THRESHOLD", 3))
MIN_USERS = int(os.environ.get("INDEX_PROMOTE_MIN_USERS", 2))
MIN_COMMENT_CHARS = int(os.environ.get("INDEX_MIN_COMMENT_CHARS", 40))
SCAN_LIMIT = int(os.environ.get("INDEX_SCAN_LIMIT", 30))
# Anchor requirement: at least this many of the qualified items must be
# TEXT content (comment or rec). Ratings deliberately have no
# email-verified gate in firestore.rules (low-stakes personal signal) —
# which means rating-only engagement could be farmed with throwaway
# unverified accounts. Text content IS verified-gated (and pre-moderated
# by the owner), so requiring one text anchor means ratings can count
# toward the threshold but can never promote a page on their own.
MIN_TEXT_ANCHORS = int(os.environ.get("INDEX_PROMOTE_MIN_TEXT", 1))

# Links in community text disqualify it (spam vector). Deliberately broad:
# bare domains count as links too.
_LINK_RE = re.compile(
    r"https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|ly|me|info|biz|ru|cn)\b",
    re.IGNORECASE,
)


# ── Firestore REST reads (rules-compliant, unauthenticated) ──

def _run_query(book_key: str, collection: str, approved_filter: bool) -> list[dict]:
    """runQuery on books/{book_key}/{collection}. Returns document dicts
    ({name, fields}). approved_filter adds where approved==true — REQUIRED
    for comments/reader_recs (rules deny broader queries), omitted for
    ratings (public read)."""
    query: dict = {"from": [{"collectionId": collection}], "limit": 200}
    if approved_filter:
        query["where"] = {
            "fieldFilter": {
                "field": {"fieldPath": "approved"},
                "op": "EQUAL",
                "value": {"booleanValue": True},
            }
        }
    url = f"{_FS_BASE}/books/{book_key}:runQuery"
    try:
        r = httpx.post(url, params={"key": FIREBASE_WEB_KEY},
                       json={"structuredQuery": query}, timeout=10.0)
        if r.status_code != 200:
            log.warning(f"Firestore query {collection} for '{book_key}' -> {r.status_code}: {r.text[:150]}")
            return []
        docs = []
        for row in r.json():
            doc = row.get("document")
            if doc:
                docs.append(doc)
        return docs
    except Exception as e:
        log.warning(f"Firestore query {collection} for '{book_key}' failed: {e}")
        return []


def _fv(fields: dict, name: str) -> str:
    """Extract a Firestore REST field value (string variants we use)."""
    v = fields.get(name) or {}
    return v.get("stringValue") or ""


# ── Quality gates ────────────────────────────────────────────

def _comment_qualifies(text: str) -> bool:
    text = (text or "").strip()
    return len(text) >= MIN_COMMENT_CHARS and not _LINK_RE.search(text)


def _rec_reason_acceptable(rec_id: str, reason: str) -> bool:
    """Strict Gemini yes/no: is this recommendation reason coherent and
    clearly written? Verdict cached per rec id (+reason, so an edited
    reason re-judges) — re-runs never re-bill. Refuses on any doubt or on
    model failure: an unjudged rec simply doesn't count today and gets
    retried on the next run (no data beats wrong data, applied to scoring)."""
    reason = (reason or "").strip()
    if not reason or _LINK_RE.search(reason):
        return False
    cache_key = ("rec_reason_verdict_v1", rec_id, reason[:120])
    cached = cache.get(*cache_key)
    if cached is not None:
        return bool(cached.get("ok"))

    import gemini_client
    prompt = f"""You are a strict content-quality judge for a book site. A reader recommended a book as similar to another and gave this reason:

\"\"\"{reason[:600]}\"\"\"

Answer ONLY this: is the reason a coherent, clearly written explanation of why the books are similar — actual reasoning a human wrote with care? Answer false for: gibberish, random characters, spam, self-promotion, or empty generic praise with no reasoning (e.g. just "good book", "nice"). If you are not sure, answer false.

Return ONLY JSON: {{"acceptable": true_or_false}}"""
    try:
        raw = gemini_client.generate(prompt)
        data = gemini_client.parse_json_response(raw)
        ok = bool(isinstance(data, dict) and data.get("acceptable") is True)
        cache.set({"ok": ok}, *cache_key, ttl=86400 * 90)
        return ok
    except Exception as e:
        log.warning(f"Rec-reason judgment failed for '{rec_id}': {e}")
        return False  # uncached — retried next run


# ── Scoring ──────────────────────────────────────────────────

def score_book(book_key: str) -> dict:
    """Counts QUALIFIED engagement for one canonical book key. Returns
    {qualified, users, breakdown} — pure read, no side effects."""
    users: set[str] = set()
    q_comments = q_recs = q_ratings = 0

    seen_comment_uids: set[str] = set()
    for doc in _run_query(book_key, "comments", approved_filter=True):
        f = doc.get("fields", {})
        uid = _fv(f, "uid")
        if uid in seen_comment_uids:
            continue  # one qualified comment per user
        if _comment_qualifies(_fv(f, "text")):
            seen_comment_uids.add(uid)
            users.add(uid)
            q_comments += 1

    seen_rec_uids: set[str] = set()
    for doc in _run_query(book_key, "reader_recs", approved_filter=True):
        f = doc.get("fields", {})
        uid = _fv(f, "uid")
        if uid in seen_rec_uids:
            continue
        rec_id = (doc.get("name") or "").rsplit("/", 1)[-1]
        if _rec_reason_acceptable(rec_id, _fv(f, "reason")):
            seen_rec_uids.add(uid)
            users.add(uid)
            q_recs += 1

    for doc in _run_query(book_key, "ratings", approved_filter=False):
        uid = (doc.get("name") or "").rsplit("/", 1)[-1]  # doc id == uid
        users.add(uid)
        q_ratings += 1

    qualified = q_comments + q_recs + q_ratings
    return {
        "qualified": qualified,
        "users": len(users),
        "breakdown": {"comments": q_comments, "recs": q_recs, "ratings": q_ratings},
        "promote": (qualified >= THRESHOLD
                    and len(users) >= MIN_USERS
                    and (q_comments + q_recs) >= MIN_TEXT_ANCHORS),
    }


# ── Page scan + surgical promotion write ─────────────────────

_CANONICAL_RE = re.compile(r"^canonical_id:\s*(.+)$", re.MULTILINE)
_NOINDEX_FALSE_RE = re.compile(r"^noindex:\s*false\s*$", re.MULTILINE)


def _promote_page(path: str, content: str, sha: str, title: str) -> bool:
    """Insert noindex/sitemap/audit lines into the front matter, right
    before the closing delimiter. Surgical: everything else byte-identical."""
    head, sep, rest = content.partition("\n---\n")
    if not sep:
        log.warning(f"Front matter delimiter not found in {path} — skipping promote.")
        return False
    promoted = (head
                + "\nnoindex: false"
                + "\nsitemap: true"
                + f"\nindex_promoted: engagement {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
                + sep + rest)
    return github_publisher._update_file(
        path, promoted, f"Promote to index (earned engagement): {title}", sha)


@router.post("/promote")
def promote(x_promote_secret: str = Header(default=""),
            dry: bool = Query(default=False)):
    if not PROMOTE_SECRET or x_promote_secret != PROMOTE_SECRET:
        raise HTTPException(status_code=403, detail="bad or missing secret")
    if not github_publisher.GITHUB_PAT:
        raise HTTPException(status_code=503, detail="GITHUB_PAT not configured")

    # Full page list from the repo (source of truth; the Redis index only
    # knows pages seen since it existed).
    try:
        r = httpx.get(github_publisher._contents_url("_books"),
                      headers=github_publisher._HEADERS,
                      params={"ref": github_publisher.GITHUB_BRANCH}, timeout=15.0)
        r.raise_for_status()
        pages = [it["name"] for it in r.json() if it.get("name", "").endswith(".md")]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not list _books: {e}")

    report = {"scanned": 0, "promoted": [], "eligible_dry": [], "skipped_indexed": 0,
              "below_threshold": {}, "dry": dry}
    for name in pages:
        if report["scanned"] >= SCAN_LIMIT:
            break
        slug = name[:-3]
        # Cheap skip: pages already known indexed (promoted earlier, or
        # Gutenberg-indexed at publish). 30-day flag — re-verified from the
        # file after expiry, so a manual revert isn't masked forever.
        if cache.get_key(f"idx_done:{slug}"):
            report["skipped_indexed"] += 1
            continue
        report["scanned"] += 1

        path = f"_books/{name}"
        exists, content, sha = github_publisher._file_exists(path)
        if not exists:
            continue
        if _NOINDEX_FALSE_RE.search(content):
            cache.set_key(f"idx_done:{slug}", {"ts": datetime.now(timezone.utc).timestamp()},
                          ttl=86400 * 30)
            report["skipped_indexed"] += 1
            report["scanned"] -= 1  # didn't really cost a scan decision
            continue

        m = _CANONICAL_RE.search(content)
        try:
            book_key = json.loads(m.group(1)) if m else slug
        except Exception:
            book_key = slug

        score = score_book(book_key)
        if score["promote"]:
            tm = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
            try:
                title = json.loads(tm.group(1)) if tm else slug
            except Exception:
                title = slug
            if dry:
                report["eligible_dry"].append({"slug": slug, **score})
            elif _promote_page(path, content, sha, title):
                cache.set_key(f"idx_done:{slug}", {"ts": datetime.now(timezone.utc).timestamp()},
                              ttl=86400 * 30)
                report["promoted"].append({"slug": slug, **score})
                log.info(f"Promoted to index: {slug} ({score['qualified']} qualified, {score['users']} users)")
        elif score["qualified"] > 0:
            report["below_threshold"][slug] = score

    return report
