"""
tools/quiz_core.py — shared quiz-generation core, extracted from pdfchat.py.

Pure library functions with NO route/storage coupling, shared by:
  - tools/pdfchat.py  — per-upload PDF quizzes (the original home)
  - tools/quiz.py     — per-BOOK quizzes for Gutenberg/Fandom-grounded titles

The critical property preserved here is the anti-hallucination contract:
every generated question carries a supporting_quote that is VERIFIED
(normalized substring match) to exist in the supplied source chunks —
unverified questions are dropped, never shown.
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from google.genai import types as genai_types

import cache
import gemini_client

log = logging.getLogger("bookhub-api.tools.quiz_core")

CHUNK_CHARS = 3000
QUIZ_SAMPLE_CHUNKS = 16


def _err(status: int, code: str, message: str, **extra):
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _client_ip(request: Request) -> str:
    """The abuse-control identity for a request: the real client IP, keyed so
    a caller CANNOT mint fresh quota at will (unlike a body-supplied
    client_id, which is trivially rotated).

    Render runs uvicorn without --proxy-headers, so request.client.host is the
    shared Render proxy, not the caller. The real client IP is what Render's
    single proxy appended to X-Forwarded-For — the RIGHTMOST entry. (The
    leftmost hops are client-supplied and therefore spoofable; taking the
    rightmost is what makes this spoof-resistant for our single-proxy
    topology: the API is addressed directly at *.onrender.com, no CDN in
    front.) Falls back to the socket peer if the header is absent (local dev)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return getattr(request.client, "host", "unknown") or "unknown"


# Process-local degrade counter for when Redis is unavailable (see
# _rate_limit). Single Render instance => this is still a real global cap.
# Cleared on day-rollover; hard-capped in size so a spoofed-IP flood during a
# Redis outage can't grow it without bound.
_LOCAL_COUNTS: dict[str, int] = {}
_LOCAL_DAY: dict[str, str] = {"day": ""}
_LOCAL_MAX_KEYS = 100_000


def _rate_limit(kind: str, limit: int, client_id: str, namespace: str = "pdf") -> None:
    """Daily per-client counter enforced on the Redis store, DEGRADING to a
    process-local counter when Redis is unavailable — never failing fully
    open (an expensive Gemini/publish route must stay capped even during a
    cache outage). `client_id` should be a server-derived identity
    (see _client_ip), NOT a value the caller can rotate at will.

    `namespace` keeps quota surfaces separate (pdf-chat's per-upload quizzes
    vs. shared per-book quizzes are different cost profiles)."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"{namespace}:rl:{kind}:{day}:{client_id}"
    count = cache.incr_key(key, ttl=90000)
    if count is None:
        # Redis unreachable — fall back to an in-process counter rather than
        # allowing unlimited expensive calls. Reset the map when the UTC day
        # rolls over, and bound its size defensively.
        if _LOCAL_DAY["day"] != day:
            _LOCAL_DAY["day"] = day
            _LOCAL_COUNTS.clear()
        if len(_LOCAL_COUNTS) >= _LOCAL_MAX_KEYS and key not in _LOCAL_COUNTS:
            _LOCAL_COUNTS.clear()  # crude bound; better to reset than to OOM
        count = _LOCAL_COUNTS.get(key, 0) + 1
        _LOCAL_COUNTS[key] = count
    if count > limit:
        raise _err(429, "rate_limited",
                   f"Daily {kind} limit reached ({limit}). Please come back tomorrow.",
                   kind=kind, limit=limit)


def _chunk_text(text: str) -> list[str]:
    """Normalize whitespace, then greedy-pack paragraphs into ~3000-char chunks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n\t]+", " ", text)          # collapse spaces (keep \n\t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > CHUNK_CHARS:
            # Oversized paragraph: split at the last sentence boundary in range.
            cut = para.rfind(". ", 0, CHUNK_CHARS)
            cut = cut + 1 if cut > CHUNK_CHARS // 4 else CHUNK_CHARS
            piece, para = para[:cut].strip(), para[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if not para:
            continue
        if current and len(current) + len(para) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)

    # Merge tiny trailing chunks into their predecessor.
    merged: list[str] = []
    for c in chunks:
        if merged and len(c) < 500 and len(merged[-1]) + len(c) < CHUNK_CHARS + 600:
            merged[-1] = f"{merged[-1]}\n\n{c}"
        else:
            merged.append(c)
    return merged


def _normalize_for_match(s: str) -> str:
    s = s.lower()
    s = (s.replace("‘", "'").replace("’", "'")
          .replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-")
          .replace("…", "..."))
    return re.sub(r"\s+", " ", s).strip()


def _generate_quiz(digest: str, chunks: list[str], count: int) -> list[dict]:
    """Generate count+3 MCQs from evenly-spread chunks, then verify quotes."""
    n = len(chunks)
    take = min(QUIZ_SAMPLE_CHUNKS, n)
    indices = sorted({round(i * (n - 1) / max(take - 1, 1)) for i in range(take)})
    sampled = {i: chunks[i] for i in indices}
    excerpts = "\n\n".join(f"[Chunk {i}]\n{c}" for i, c in sampled.items())

    prompt = f"""Create {count + 3} multiple-choice quiz questions about this book, based ONLY on the excerpts below.

BOOK DIGEST:
{digest or "(no digest)"}

EXCERPTS:
{excerpts}

BEFORE WRITING ANYTHING, DECIDE WHETHER YOU SHOULD:
- If the excerpts are not about the narrative of a book — if they are a
  filmography, a cast list, a production or publication history, a list of
  editions, a disambiguation page, an author biography, or an article about a
  FILM, TELEVISION or STAGE adaptation rather than the book itself — return an
  empty array `[]` and nothing else.
- If a passage describes events that happen in an ADAPTATION rather than in
  the book, do not build a question on it, even if the passage is right there
  in front of you. A wiki article often covers a book and its film in one
  page; the film's events are not this book's events.
- Returning `[]` is a correct and expected outcome. A missing quiz costs a
  reader nothing. A quiz about the wrong work teaches them something false
  and tells them we checked.

REQUIREMENTS for every question:
- Answerable purely from the excerpts (no outside knowledge).
- SELF-CONTAINED: a reader who has read the book but CANNOT see these excerpts must fully understand the question. Never write "in the excerpt", "in this passage", "according to the text", "in Chunk N", or refer to any page/section structure. Name the characters, places, and events explicitly instead.
- Prefer memorable, significant story content — major events, character motivations, relationships, settings — over incidental trivia that only makes sense right next to its source sentence.
- Exactly 4 answer options, one correct, three plausible but wrong.
- "supporting_quote": a VERBATIM quote (max 200 characters) copied EXACTLY from one excerpt, proving the correct answer.
- "chunk_index": the [Chunk N] number the quote came from.

Return ONLY a JSON array:
[{{"question": "...", "options": ["...","...","...","..."], "answer_index": 0, "supporting_quote": "...", "chunk_index": 0}}]"""
    config = genai_types.GenerateContentConfig(
        temperature=0.4, max_output_tokens=4096, response_mime_type="application/json"
    )
    raw = gemini_client.generate(prompt, config)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = gemini_client.parse_json_response(raw)
    if isinstance(data, dict):  # model may wrap the array
        data = data.get("questions") or next((v for v in data.values() if isinstance(v, list)), [])

    normalized_sampled = {i: _normalize_for_match(c) for i, c in sampled.items()}
    verified: list[dict] = []
    dropped = 0
    for q in data if isinstance(data, list) else []:
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        answer_index = q.get("answer_index")
        quote = _normalize_for_match(str(q.get("supporting_quote", "")))
        if (not isinstance(options, list) or len(options) != 4
                or len({str(o).strip() for o in options}) != 4
                or any(not str(o).strip() for o in options)
                or not isinstance(answer_index, int) or not (0 <= answer_index <= 3)
                or len(quote) < 15 or not q.get("question")):
            dropped += 1
            continue
        # Verify the quote genuinely appears in the source chunks.
        ci = q.get("chunk_index")
        found_in = None
        if isinstance(ci, int) and ci in normalized_sampled and quote in normalized_sampled[ci]:
            found_in = ci
        else:
            found_in = next((i for i, nc in normalized_sampled.items() if quote in nc), None)
        if found_in is None:
            dropped += 1
            continue
        verified.append({
            "question": str(q["question"])[:500],
            "options": [str(o)[:300] for o in options],
            "answer_index": answer_index,
            "supporting_quote": str(q.get("supporting_quote", ""))[:250],
            "chunk_index": found_in,
        })
        if len(verified) >= count:
            break
    if dropped:
        log.info(f"Quiz verification dropped {dropped} ungrounded/malformed question(s).")
    return verified
