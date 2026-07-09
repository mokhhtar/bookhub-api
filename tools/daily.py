"""
tools/daily.py — GET /daily: the homepage's three daily picks.

  book   — a book tied to a REAL historical event that happened on today's
           date (grounded on Wikimedia's On-this-day feed; the book itself
           is verified against Google Books before being shown).
  author — an author actually BORN on today's date (the person, date, photo
           and description all come straight from the Wikimedia births feed —
           Gemini only writes a blurb from the provided extract).
  quote  — a famous book quote; the BOOK is verified via Google Books, and a
           small curated list guarantees the card never comes up empty.

The whole payload is computed once per UTC day (first visitor pays ~5-10s),
cached in Redis under daily:YYYY-MM-DD, and shared by every visitor. Each
top-level section is nullable — the homepage renders whatever verified.
"""
import logging
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Response

import cache
import book_data
import gemini_client

log = logging.getLogger("bookhub-api.tools.daily")

router = APIRouter()

WIKI_FEED = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday"
HEADERS = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

_WRITER_RE = re.compile(
    r"writer|novelist|author|poet|playwright|essayist", re.IGNORECASE
)

# Curated fallback quotes — every book pre-verified by hand. Used when none
# of Gemini's daily candidates survive verification, so the card never fails.
# (Upgrade path: grow this to 366 and drop the Gemini step entirely.)
CURATED_QUOTES = [
    {"text": "It is our choices, Harry, that show what we truly are, far more than our abilities.",
     "book_title": "Harry Potter and the Chamber of Secrets", "author": "J.K. Rowling"},
    {"text": "Not all those who wander are lost.",
     "book_title": "The Fellowship of the Ring", "author": "J.R.R. Tolkien"},
    {"text": "It was the best of times, it was the worst of times.",
     "book_title": "A Tale of Two Cities", "author": "Charles Dickens"},
    {"text": "All animals are equal, but some animals are more equal than others.",
     "book_title": "Animal Farm", "author": "George Orwell"},
    {"text": "The only way out of the labyrinth of suffering is to forgive.",
     "book_title": "Looking for Alaska", "author": "John Green"},
    {"text": "It does not do to dwell on dreams and forget to live.",
     "book_title": "Harry Potter and the Sorcerer's Stone", "author": "J.K. Rowling"},
    {"text": "Whatever our souls are made of, his and mine are the same.",
     "book_title": "Wuthering Heights", "author": "Emily Bronte"},
    {"text": "So we beat on, boats against the current, borne back ceaselessly into the past.",
     "book_title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
    {"text": "War is peace. Freedom is slavery. Ignorance is strength.",
     "book_title": "1984", "author": "George Orwell"},
    {"text": "There is some good in this world, and it's worth fighting for.",
     "book_title": "The Two Towers", "author": "J.R.R. Tolkien"},
]


def _fetch_onthisday(kind: str, mm: str, dd: str) -> list[dict]:
    try:
        r = httpx.get(f"{WIKI_FEED}/{kind}/{mm}/{dd}", headers=HEADERS,
                      timeout=10.0, follow_redirects=True)
        if r.status_code == 200:
            return r.json().get(kind, []) or []
    except Exception as e:
        log.warning(f"Wikimedia on-this-day '{kind}' fetch failed: {e}")
    return []


def _pick_book_of_day(events: list[dict]) -> dict | None:
    """Gemini relates a REAL event to 3 candidate books; first verified wins."""
    if not events:
        return None
    trimmed = [
        {"i": i, "year": e.get("year"), "text": (e.get("text") or "")[:200]}
        for i, e in enumerate(events[:25])
    ]
    listing = "\n".join(f"[{t['i']}] ({t['year']}) {t['text']}" for t in trimmed)
    prompt = f"""Here are real historical events that happened on today's date:

{listing}

Pick the events most connectable to a well-known, actually-published BOOK (fiction or nonfiction) a general reader would enjoy. Return 3 ranked candidates as JSON:
{{"candidates": [{{"event_index": 0, "title": "...", "author": "...", "connection": "one sentence linking the event to the book, referencing only the event text above"}}]}}
Real, widely available books only. Return ONLY the JSON."""
    try:
        data = gemini_client.parse_json_response(gemini_client.generate(prompt))
    except Exception as e:
        log.warning(f"Book-of-day proposal failed: {e}")
        return None

    for cand in (data.get("candidates") or [])[:3]:
        if not isinstance(cand, dict):
            continue
        idx = cand.get("event_index")
        if not isinstance(idx, int) or not (0 <= idx < len(events)):
            continue
        verified = book_data.verify_book_exists(cand.get("title", ""), cand.get("author", ""))
        if verified:
            ev = events[idx]
            return {
                **verified,
                "event": {"year": ev.get("year"), "text": (ev.get("text") or "")[:300]},
                "connection": (cand.get("connection") or "")[:300],
            }
    return None


def _pick_author_of_day(births: list[dict]) -> dict | None:
    """
    Deterministic grounding: keep only birthday entries whose Wikipedia page
    description says the person is a writer. Person, birth year, photo and
    description are all REAL feed data; Gemini only writes a blurb from the
    provided extract (and may propose one notable book, shown only if verified).
    """
    writers = []
    for b in births:
        for p in b.get("pages", []) or []:
            desc = p.get("description", "") or ""
            if _WRITER_RE.search(desc):
                writers.append({
                    "name": p.get("titles", {}).get("normalized") or p.get("title", ""),
                    "born_year": b.get("year"),
                    "description": desc,
                    "extract": (p.get("extract") or "")[:1500],
                    "photo_url": (p.get("thumbnail") or {}).get("source", ""),
                    "wikipedia_url": ((p.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
                })
                break
    if not writers:
        return None

    # Let Gemini pick the most notable writer and write the grounded blurb.
    listing = "\n".join(f"[{i}] {w['name']} (b. {w['born_year']}) — {w['description']}"
                        for i, w in enumerate(writers[:15]))
    prompt = f"""These real authors were born on today's date:

{listing}

Pick the single most notable/interesting one for a book-lover audience. Then, using ONLY the extract below for that author, write a 1-2 sentence blurb. Optionally name their single most famous book.

EXTRACTS:
{chr(10).join(f"[{i}] {w['extract'][:400]}" for i, w in enumerate(writers[:15]))}

Return ONLY JSON: {{"index": 0, "blurb": "...", "notable_book_title": "..." or null}}"""
    try:
        data = gemini_client.parse_json_response(gemini_client.generate(prompt))
        idx = data.get("index")
        chosen = writers[idx] if isinstance(idx, int) and 0 <= idx < len(writers) else writers[0]
        blurb = (data.get("blurb") or "")[:400]
        notable = None
        if data.get("notable_book_title"):
            notable = book_data.verify_book_exists(data["notable_book_title"], chosen["name"])
    except Exception as e:
        log.warning(f"Author-of-day selection failed: {e}")
        chosen, blurb, notable = writers[0], "", None

    result = {k: v for k, v in chosen.items() if k != "extract"}
    result["blurb"] = blurb or chosen["description"]
    result["notable_book"] = notable
    return result


_QUOTE_HISTORY_KEY = "daily:quote_history"


def _normalize_quote(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _pick_quote_of_day(date_str: str) -> dict | None:
    """
    Gemini proposes 3 widely-attested quotes; book verified. Curated fallback.

    At temperature 0.3 (see gemini_client.py), asking for "the" famous book
    quote reliably converges on the single most iconic answer in the
    language (the Dickens "best of times" opening) regardless of the date —
    unlike book/author, which are grounded in real per-day Wikimedia feed
    data, quote selection is unconstrained, so nothing forced variety. We
    track the last 30 days of served quotes in Redis, tell Gemini to avoid
    them, and skip any candidate (Gemini or curated) that repeats one —
    falling back to a repeat only if every candidate is exhausted, so the
    card never goes blank.
    """
    history: list[str] = cache.get_key(_QUOTE_HISTORY_KEY) or []
    recent = {_normalize_quote(t) for t in history}
    avoid_listing = "; ".join(t[:100] for t in history[-15:])

    prompt = f"""Give 3 famous, widely-attested quotes FROM BOOKS (today is {date_str}, vary your picks by date). Only quotes that are verifiably famous and commonly attributed — no obscure or invented ones.
{f"Do NOT repeat any of these already-used quotes: {avoid_listing}" if avoid_listing else ""}

Return ONLY JSON: {{"quotes": [{{"text": "...", "book_title": "...", "author": "..."}}]}}"""
    candidates = []
    try:
        data = gemini_client.parse_json_response(gemini_client.generate(prompt))
        candidates = [q for q in (data.get("quotes") or []) if isinstance(q, dict)][:3]
    except Exception as e:
        log.warning(f"Quote-of-day proposal failed: {e}")

    # Curated fallback: walk the whole pool starting from the day-seeded
    # index (rather than just appending one entry) so a repeat-skip has
    # somewhere else to go instead of falling straight to None.
    day_index = sum(ord(c) for c in date_str) % len(CURATED_QUOTES)
    candidates += [CURATED_QUOTES[(day_index + i) % len(CURATED_QUOTES)] for i in range(len(CURATED_QUOTES))]

    repeat_fallback = None
    for q in candidates:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        verified = book_data.verify_book_exists(q.get("book_title", ""), q.get("author", ""))
        if not verified:
            continue
        result = {"text": text[:400], "book": verified, "source": "gemini+google_books"}
        # Containment, not equality: Gemini re-proposes famous quotes in
        # longer/shorter variants ("...best of times." vs "...age of
        # foolishness...") — any overlap with a recent entry is a repeat.
        norm_new = _normalize_quote(text)
        is_repeat = any(norm_new == r or norm_new in r or r in norm_new for r in recent if r)
        if not is_repeat:
            history.append(text)
            cache.set_key(_QUOTE_HISTORY_KEY, history[-30:], ttl=60 * 60 * 24 * 45)
            return result
        repeat_fallback = repeat_fallback or result

    return repeat_fallback


def _build_daily(date_str: str, mm: str, dd: str) -> dict:
    events = _fetch_onthisday("events", mm, dd)
    births = _fetch_onthisday("births", mm, dd)
    return {
        "date": date_str,
        "book": _pick_book_of_day(events),
        "author": _pick_author_of_day(births),
        "quote": _pick_quote_of_day(date_str),
    }


@router.options("/daily")
def daily_options():
    # Some uptime monitors probe with OPTIONS instead of GET — respond
    # cheaply instead of 405ing.
    return Response(status_code=204)


@router.head("/daily")
def daily_head():
    # Some uptime monitors default to HEAD. Respond cheaply — do NOT run the
    # full build pipeline (Wikimedia + Gemini + Google Books) on every liveness
    # ping, that would burn API quota for no reason.
    return Response(status_code=200)


@router.get("/daily")
def daily():
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    key = f"daily:{date_str}"

    cached = cache.get_key(key)
    if cached:
        return cached

    # One builder per day across instances; losers of the race just build too
    # (idempotent, only costs a few extra API calls in a rare tie).
    cache.acquire_lock(f"daily:lock:{date_str}", ttl=120)

    payload = _build_daily(date_str, now.strftime("%m"), now.strftime("%d"))
    # Cache even a partially-null payload for the whole day (48h TTL covers
    # timezone stragglers) — but if EVERYTHING failed, only cache briefly so
    # a transient outage doesn't blank the homepage all day.
    all_failed = not (payload.get("book") or payload.get("author") or payload.get("quote"))
    cache.set_key(key, payload, ttl=600 if all_failed else 60 * 60 * 48)
    return payload
