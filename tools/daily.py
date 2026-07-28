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
import json
import logging
import os
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

# Word boundaries are load-bearing. Without them "writer" matched inside
# "singer-songwriter" and "screenwriter", and 6 of the 31 people who passed
# this gate on 2026-07-27 were musicians or television writers.
_WRITER_RE = re.compile(
    r"\b(?:writer|novelist|author|poet|playwright|essayist)\b", re.IGNORECASE
)

# Someone described as a novelist or a poet is a book author by trade. "Author"
# on its own is the weakest of the qualifying words — it is what Wikipedia
# writes for "game designer and author" — so it ranks below the specific ones.
_BOOK_TRADE_RE = re.compile(
    r"\b(?:novelist|poet|playwright|essayist|writer)\b", re.IGNORECASE
)


def _author_score(w: dict) -> int:
    """Cheap notability ranking from data already in the feed response.

    Needed because the feed is ordered by birth year DESCENDING and the code
    below only ever showed Gemini the first 15 candidates — so on a normal day
    the window stopped around 1927 and every classic author born earlier was
    dropped before the model ever saw them. Ranking first, truncating second,
    puts them back in contention.

    Deliberately no Wikipedia pageview lookup: that is one HTTP call per
    candidate (31 today) against a service that had just demonstrated it can
    time out, and these three signals come free in the response we already have.
    """
    score = 0
    if _BOOK_TRADE_RE.search(w.get("description") or ""):
        score += 3          # "novelist" beats a generic "author"
    if w.get("photo_url"):
        score += 2          # a maintained article usually has an image
    score += min(len(w.get("extract") or "") // 400, 4)   # longer article, bigger figure
    return score

# Curated fallback quotes — every book pre-verified by hand. Used when none
# of Gemini's daily candidates survive verification, so the card never fails.
#
# These ten are the FLOOR, not the well. The real pool is data/daily_quotes.json
# (built by scripts/build_daily_quotes.py from the Wikiquote blocks already
# committed in the site's book pages — real, attributed, and each one linking
# to a page we publish). Ten entries against a 30-day no-repeat window
# exhausts itself in under two weeks; the generated file carries 122.
_BASE_CURATED_QUOTES = [
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


def _load_quote_well() -> list[dict]:
    """The generated well, with the hardcoded ten appended as a floor.

    Missing or unreadable file → the ten still ship, so the card degrades to
    what it did before instead of to nothing. Read once at import; the file is
    committed alongside the code and never changes at runtime."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "daily_quotes.json")
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if isinstance(rows, list) and rows:
            log.info(f"Daily quote well loaded: {len(rows)} quotes from our own pages.")
            return rows + _BASE_CURATED_QUOTES
    except Exception as e:
        log.warning(f"Daily quote well unavailable ({e}) — falling back to the base ten.")
    return list(_BASE_CURATED_QUOTES)


CURATED_QUOTES = _load_quote_well()


def _fetch_onthisday(kind: str, mm: str, dd: str) -> list[dict]:
    try:
        r = httpx.get(f"{WIKI_FEED}/{kind}/{mm}/{dd}", headers=HEADERS,
                      timeout=10.0, follow_redirects=True)
        if r.status_code == 200:
            return r.json().get(kind, []) or []
    except Exception as e:
        log.warning(f"Wikimedia on-this-day '{kind}' fetch failed: {e}")
    return []


def _connection_holds(cand: dict, events: list[dict]) -> bool:
    """Second pass over the CLAIM, not just the book's existence.

    verify_book_exists only ever proved the title is a real book. Nothing
    checked that the link between the event and the book was true, so the card
    could pair a genuine event with a genuine book and a sentence connecting
    them that was invented — the one thing the grounding rule exists to
    prevent, hiding behind two verified halves.

    Refuses on anything but an explicit yes, including its own failure: a card
    that doesn't run beats a card that asserts something we can't stand behind.
    """
    idx = cand.get("event_index")
    if not isinstance(idx, int) or not (0 <= idx < len(events)):
        return False
    connection = (cand.get("connection") or "").strip()
    title = (cand.get("title") or "").strip()
    if not connection or not title:
        return False

    event_text = (events[idx].get("text") or "")[:300]
    prompt = f"""EVENT (real, from an encyclopaedia): {event_text}

BOOK: "{title}" by {cand.get("author", "")}

CLAIM: {connection}

Is this claim accurate — is the book genuinely connected to this event in the way described, and would a well-read person accept the link without objection?

Answer true ONLY if you are confident. A tenuous thematic association, a wrong author, a book that only sounds related, or anything you are unsure about must be false.

Return ONLY JSON: {{"accurate": true or false}}"""
    try:
        verdict = gemini_client.parse_json_response(gemini_client.generate(prompt))
        return verdict.get("accurate") is True
    except Exception as e:
        log.warning(f"Book-of-day connection check failed for '{title}': {e}")
        return False


def _book_from_our_shelf(date_str: str) -> dict | None:
    """Fallback when no event/book link survives the accuracy check.

    Some days simply have no event a book honestly connects to, and the
    alternative to this is a blank card every hour until midnight. Showing a
    book we publish is true on its own terms — it makes no claim about the
    date — and it sends the visitor to a real page of ours. Deterministic per
    day so it doesn't change under a reader mid-visit.
    """
    by_url: dict[str, dict] = {}
    for q in CURATED_QUOTES:
        if q.get("book_url"):
            by_url.setdefault(q["book_url"], q)
    if not by_url:
        return None
    shelf = sorted(by_url.values(), key=lambda q: q["book_url"])
    pick = shelf[sum(ord(c) for c in date_str) % len(shelf)]
    return {
        "title": pick["book_title"],
        "author": pick["author"],
        "cover_url": pick.get("cover_url") or "",
        "book_url": pick["book_url"],
        "source": "litheca_shelf",
    }


def _pick_book_of_day(events: list[dict], date_str: str) -> dict | None:
    """Gemini relates a REAL event to 3 candidate books; first verified wins."""
    if not events:
        return _book_from_our_shelf(date_str)
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
        return _book_from_our_shelf(date_str)

    for cand in (data.get("candidates") or [])[:3]:
        if not isinstance(cand, dict):
            continue
        if not _connection_holds(cand, events):
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
    return _book_from_our_shelf(date_str)


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

    # Rank BEFORE truncating. The feed arrives newest-birth-first, so slicing
    # the raw order cut the window off around 1927 on a typical day and no
    # classic author ever reached the model.
    writers.sort(key=_author_score, reverse=True)
    shortlist = writers[:15]

    # Let Gemini pick the most notable writer and write the grounded blurb.
    listing = "\n".join(f"[{i}] {w['name']} (b. {w['born_year']}) — {w['description']}"
                        for i, w in enumerate(shortlist))
    prompt = f"""These real authors were born on today's date:

{listing}

Pick the one whose BOOKS a reader is most likely to know. Prefer novelists, poets and playwrights over people who write in another medium — a game designer, screenwriter or musician described as an "author" is the wrong answer here even when their article is the longest. Then, using ONLY the extract below for that author, write a 1-2 sentence blurb. Optionally name their single most famous book.

EXTRACTS:
{chr(10).join(f"[{i}] {w['extract'][:400]}" for i, w in enumerate(shortlist))}

Return ONLY JSON: {{"index": 0, "blurb": "...", "notable_book_title": "..." or null}}"""
    try:
        data = gemini_client.parse_json_response(gemini_client.generate(prompt))
        idx = data.get("index")
        # Falling back to shortlist[0] means the best-ranked candidate; it used
        # to mean whoever the feed happened to list first, i.e. the most
        # recently born person on the list.
        chosen = shortlist[idx] if isinstance(idx, int) and 0 <= idx < len(shortlist) else shortlist[0]
        blurb = (data.get("blurb") or "")[:400]
        notable = None
        if data.get("notable_book_title"):
            notable = book_data.verify_book_exists(data["notable_book_title"], chosen["name"])
    except Exception as e:
        log.warning(f"Author-of-day selection failed: {e}")
        chosen, blurb, notable = shortlist[0], "", None

    result = {k: v for k, v in chosen.items() if k != "extract"}
    result["blurb"] = blurb or chosen["description"]
    result["notable_book"] = notable
    return result


_QUOTE_HISTORY_KEY = "daily:quote_history"


def _normalize_quote(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _pick_quote_of_day(date_str: str) -> dict | None:
    """
    OUR OWN WELL FIRST; Gemini only when the well can't produce a fresh one.

    That order used to be reversed, and it was not a taste question. A
    Gemini-proposed quote is checked with verify_book_exists — which proves
    the BOOK exists and says nothing about whether the quote is IN it. Nothing
    ever verified the text. Misattribution is the single most common failure
    mode in the whole genre of famous quotations (every "Mark Twain said"
    that he never said), and the card had no defence against it. This is the
    same "verified the name, not the claim" hole that _connection_holds
    closed for book-of-the-day.

    The well in data/daily_quotes.json is transcribed verbatim from the
    Wikiquote blocks on our own book pages, so its text is sourced, not
    recalled — and each entry links to a page we publish rather than to the
    dynamic summarizer.

    Cost note: with 122 entries against a 30-day no-repeat window the well
    covers about four months, so in practice the Gemini call below almost
    never runs. That is a saved API call a day, not just a safer quote.
    """
    history: list[str] = cache.get_key(_QUOTE_HISTORY_KEY) or []
    recent = {_normalize_quote(t) for t in history}

    # Walk the whole well from a day-seeded offset so a repeat-skip has
    # somewhere else to go instead of falling straight through to Gemini.
    day_index = sum(ord(c) for c in date_str) % len(CURATED_QUOTES)
    candidates = [CURATED_QUOTES[(day_index + i) % len(CURATED_QUOTES)]
                  for i in range(len(CURATED_QUOTES))]

    if all(_normalize_quote(c["text"]) in recent for c in candidates):
        # Only now is a proposal worth an API call.
        avoid_listing = "; ".join(t[:100] for t in history[-15:])
        prompt = f"""Give 3 famous, widely-attested quotes FROM BOOKS (today is {date_str}, vary your picks by date). Only quotes that are verifiably famous and commonly attributed — no obscure or invented ones.
{f"Do NOT repeat any of these already-used quotes: {avoid_listing}" if avoid_listing else ""}

Return ONLY JSON: {{"quotes": [{{"text": "...", "book_title": "...", "author": "..."}}]}}"""
        try:
            data = gemini_client.parse_json_response(gemini_client.generate(prompt))
            candidates += [q for q in (data.get("quotes") or []) if isinstance(q, dict)][:3]
        except Exception as e:
            log.warning(f"Quote-of-day proposal failed: {e}")

    # Gemini's proposals must be verified — they can be invented. The CURATED
    # list cannot: every entry was checked by hand before it was written here,
    # which is the entire point of it existing.
    #
    # Gating both behind the same live Google Books call is what actually
    # emptied this card. On 2026-07-27 that lookup was timing out (6s, no
    # retry) and 4 of 5 curated quotes "failed verification" — so the
    # guaranteed fallback guaranteed nothing, and the homepage lost its quote
    # band for two days. A network hiccup must never be able to veto data we
    # already verified ourselves.
    curated_texts = {(_normalize_quote(c["text"])) for c in CURATED_QUOTES}

    repeat_fallback = None
    for q in candidates:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        is_curated = _normalize_quote(text) in curated_texts
        verified = book_data.verify_book_exists(q.get("book_title", ""), q.get("author", ""))
        if not verified and not is_curated:
            continue
        if not verified:
            # Curated entry, catalog unreachable: show it with the title and
            # author we hand-checked, minus the catalog extras (cover, link).
            verified = {"title": q.get("book_title", ""), "author": q.get("author", "")}
        result = {"text": text[:400], "book": verified,
                  "source": "curated" if is_curated else "gemini+google_books"}
        # Quotes drawn from our own pages carry their page link and their
        # Wikiquote origin. book_url points the card at a book page we publish
        # instead of the dynamic summarizer; source_url IS the CC BY-SA
        # attribution the licence requires, the same one book pages render.
        if q.get("book_url"):
            result["book_url"] = q["book_url"]
        if q.get("source_url"):
            result["source_url"] = q["source_url"]
            result["license"] = q.get("license") or "CC BY-SA"
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

    cached = cache.get_key(key) or {}
    if cached.get("book") and cached.get("author") and cached.get("quote"):
        return cached

    # One builder per day across instances; losers of the race just build too
    # (idempotent, only costs a few extra API calls in a rare tie).
    cache.acquire_lock(f"daily:lock:{date_str}", ttl=120)

    # Only rebuild the sections that are actually missing. Each is an
    # independent grounded lookup, and they fail independently: on 2026-07-27
    # the Wikimedia feeds were fine and the author card was built, but Google
    # Books timed out, so book and quote came back null.
    #
    # The old code cached whatever it got for 48 HOURS unless EVERY section
    # failed, so one section surviving was enough to freeze the other two
    # empty for two days. Now a section that succeeded is kept and a section
    # that failed is retried on the next request — a slow minute at Google
    # Books costs one request, not a weekend.
    payload = dict(cached)
    payload["date"] = date_str
    missing = [k for k in ("book", "author", "quote") if not payload.get(k)]
    if missing:
        mm, dd = now.strftime("%m"), now.strftime("%d")
        events = _fetch_onthisday("events", mm, dd) if "book" in missing else []
        births = _fetch_onthisday("births", mm, dd) if "author" in missing else []
        builders = {
            "book": lambda: _pick_book_of_day(events, date_str),
            "author": lambda: _pick_author_of_day(births),
            "quote": lambda: _pick_quote_of_day(date_str),
        }
        for name in missing:
            try:
                payload[name] = builders[name]()
            except Exception as e:
                log.warning(f"Daily section '{name}' failed to build: {e}")
                payload[name] = None

    complete = all(payload.get(k) for k in ("book", "author", "quote"))
    cache.set_key(key, payload, ttl=60 * 60 * 48 if complete else 900)
    return payload
