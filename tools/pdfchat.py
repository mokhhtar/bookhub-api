"""
tools/pdfchat.py — PDF Chat & Quiz: an AI agent over user-uploaded PDFs.

The browser extracts the PDF's text with pdf.js and sends ONLY the text here
(the server never parses PDFs — protects the 512MB Render instance and needs
no PDF dependencies). Documents are identified by the SHA-256 of their text,
so a book any user already ingested is reused for free (dedup), and stored
EPHEMERALLY in Redis (48h TTL, chunked across keys to respect Upstash's ~1MB
REST request cap) — no permanent storage of full book text.

Grounding-first, like every tool here:
- Chat answers come ONLY from the stored text (top-K keyword-retrieved chunks
  + a one-time condensed digest). If the answer isn't in the material, the
  model must reply with an exact not-found phrase — never outside knowledge.
- Quiz questions each carry a supporting quote that is VERIFIED (normalized
  substring match) to exist in the source chunks; unverified questions are
  dropped, never shown.

Self-contained module (tools do not import each other).
"""
import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from google.genai import types as genai_types

import cache
import gemini_client

log = logging.getLogger("bookhub-api.tools.pdfchat")

router = APIRouter(prefix="/pdfchat")

# ── Constants (env-overridable) ──────────────────────────────
TTL_SECONDS = int(os.environ.get("PDFCHAT_TTL_SECONDS", 172800))  # 48h
MAX_TEXT_CHARS = int(os.environ.get("PDFCHAT_MAX_TEXT_CHARS", 1_200_000))
MIN_TEXT_CHARS = 500
CHUNK_CHARS = 3000
CHUNKS_PER_BLOCK = 64          # ~192KB JSON per key — under Upstash's ~1MB cap
RETRIEVAL_TOP_K = 6
QUIZ_SAMPLE_CHUNKS = 12
QUIZ_MAX_COUNT = 10
MAX_QUESTION_CHARS = 500
MAX_HISTORY_TURNS = 8
LIMIT_CHAT_DAILY = int(os.environ.get("PDFCHAT_CHAT_DAILY", 30))
LIMIT_QUIZ_DAILY = int(os.environ.get("PDFCHAT_QUIZ_DAILY", 3))
LIMIT_INGEST_DAILY = int(os.environ.get("PDFCHAT_INGEST_DAILY", 5))
NOT_FOUND_PHRASE = "I couldn't find that in this book."

_DOC_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_CLIENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "what",
    "who", "whom", "whose", "which", "where", "why", "how", "is", "are", "was",
    "were", "be", "been", "being", "am", "do", "does", "did", "have", "has",
    "had", "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "of", "in", "on", "at", "to", "for", "with", "by", "from", "about",
    "into", "over", "under", "again", "there", "here", "this", "that", "these",
    "those", "it", "its", "he", "she", "they", "them", "his", "her", "their",
    "you", "your", "we", "our", "me", "my", "i", "not", "no", "so", "as", "than",
}


# ── Request models ───────────────────────────────────────────
class CheckRequest(BaseModel):
    doc_id: str = Field(..., min_length=64, max_length=64)
    client_id: str = Field(..., max_length=40)


class IngestRequest(BaseModel):
    doc_id: str = Field(..., min_length=64, max_length=64)
    client_id: str = Field(..., max_length=40)
    text: str = Field(..., max_length=MAX_TEXT_CHARS + 1000)
    title: str = Field(default="", max_length=200)
    filename: str = Field(default="", max_length=200)
    pages: Optional[int] = Field(default=None, ge=1, le=5000)


class ChatRequest(BaseModel):
    doc_id: str = Field(..., min_length=64, max_length=64)
    client_id: str = Field(..., max_length=40)
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    history: list[dict] = Field(default_factory=list)


class QuizRequest(BaseModel):
    doc_id: str = Field(..., min_length=64, max_length=64)
    client_id: str = Field(..., max_length=40)
    count: int = Field(default=5, ge=1, le=QUIZ_MAX_COUNT)


# ── Helpers ──────────────────────────────────────────────────
def _err(status: int, code: str, message: str, **extra):
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _validate_ids(doc_id: str, client_id: str) -> str:
    """Returns the short doc key prefix; raises 400 on malformed ids."""
    if not _DOC_ID_RE.match(doc_id or ""):
        raise _err(400, "bad_request", "Invalid document id.")
    if not _CLIENT_ID_RE.match(client_id or ""):
        raise _err(400, "bad_request", "Invalid client id.")
    return doc_id[:32]


def _rate_limit(kind: str, limit: int, client_id: str) -> None:
    """Daily per-client counter. Fail-open: Redis trouble => allow."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    count = cache.incr_key(f"pdf:rl:{kind}:{day}:{client_id}", ttl=90000)
    if count is not None and count > limit:
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


def _load_doc(did: str) -> tuple[dict, list[str]]:
    """Loads meta + all chunks; raises 410 if anything expired/missing."""
    meta = cache.get_key(f"pdf:meta:{did}")
    if not isinstance(meta, dict):
        raise _err(410, "doc_expired",
                   "This book's session has expired. Please re-upload the PDF.")
    chunks: list[str] = []
    for i in range(int(meta.get("n_blocks", 0))):
        block = cache.get_key(f"pdf:block:{did}:{i}")
        if not isinstance(block, list):
            raise _err(410, "doc_expired",
                       "This book's session has expired. Please re-upload the PDF.")
        chunks.extend(block)
    if not chunks:
        raise _err(410, "doc_expired",
                   "This book's session has expired. Please re-upload the PDF.")
    return meta, chunks


def _get_digest(did: str) -> str:
    d = cache.get_key(f"pdf:digest:{did}")
    return (d or {}).get("digest", "") if isinstance(d, dict) else ""


# ── Retrieval (pure stdlib tf-idf-ish scoring) ───────────────
def _question_terms(question: str) -> list[str]:
    terms = []
    for tok in re.split(r"[^a-z0-9]+", question.lower()):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if tok.endswith(suffix) and len(tok) - len(suffix) >= 4:
                tok = tok[: -len(suffix)]
                break
        terms.append(tok)
    return terms


def _retrieve_chunks(question: str, chunks: list[str], k: int = RETRIEVAL_TOP_K) -> list[tuple[int, str]]:
    """Returns up to k (index, chunk) pairs, re-sorted into book order."""
    terms = _question_terms(question)
    if not terms:
        return []
    lowered = [c.lower() for c in chunks]
    n = len(chunks)

    tf: list[dict] = []
    df: dict = {t: 0 for t in terms}
    patterns = {t: re.compile(r"\b" + re.escape(t) + r"\w*") for t in set(terms)}
    for lc in lowered:
        counts = {}
        for t in set(terms):
            c = len(patterns[t].findall(lc))
            counts[t] = c
            if c > 0:
                df[t] += 1
        tf.append(counts)

    idf = {t: math.log(1 + n / (1 + df[t])) for t in df}
    bigrams = [f"{terms[i]} {terms[i+1]}" for i in range(len(terms) - 1)]

    scored = []
    for i in range(n):
        s = sum((1 + math.log(tf[i][t])) * idf[t] for t in set(terms) if tf[i][t] > 0)
        s += sum(2.0 for bg in bigrams if bg in lowered[i])
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    top = sorted(i for _, i in scored[:k])
    return [(i, chunks[i]) for i in top]


# ── Prompts ──────────────────────────────────────────────────
def _generate_digest(chunks: list[str], title: str) -> str:
    """One-time condensed outline. Input sampled to ~60K chars — NEVER the full text."""
    n = len(chunks)
    take = min(40, n)
    indices = sorted({round(i * (n - 1) / max(take - 1, 1)) for i in range(take)})
    sampled = "\n\n".join(f"[Excerpt {i}]\n{chunks[i][:1500]}" for i in indices)
    prompt = f"""You are reading sampled excerpts from a book{f' titled "{title}"' if title else ""}. Write a condensed DIGEST (max 3000 characters, plain text) covering ONLY what the excerpts show: probable title/author, what kind of book it is, its main subject, structure/major parts, key people/places/terms with one-line descriptors, and the overall arc. No outside knowledge, no invention.

EXCERPTS:
{sampled}"""
    config = genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=1200)
    return gemini_client.generate(prompt, config).strip()[:3200]


def _build_chat_prompt(digest: str, retrieved: list[tuple[int, str]],
                       history: list[dict], question: str) -> str:
    excerpts = "\n\n".join(f"[Chunk {i}]\n{c}" for i, c in retrieved) or "(none retrieved)"
    hist_lines = []
    for h in history[-MAX_HISTORY_TURNS * 2:]:
        role = "User" if h.get("role") == "user" else "Assistant"
        hist_lines.append(f"{role}: {str(h.get('content', ''))[:2000]}")
    hist = "\n".join(hist_lines) or "(no previous messages)"
    return f"""You answer questions about ONE specific book using ONLY the material provided below.

BOOK DIGEST:
{digest or "(no digest available)"}

EXCERPTS FROM THE BOOK:
{excerpts}

RULES:
- Answer ONLY from the digest and excerpts above.
- If the answer is not present in them, reply with exactly: "{NOT_FOUND_PHRASE}" and nothing else.
- Never use outside knowledge, even if you recognize this book.
- Reply in English with clean, concise markdown.

CONVERSATION HISTORY:
{hist}

USER QUESTION: {question}

ASSISTANT:"""


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

REQUIREMENTS for every question:
- Answerable purely from the excerpts (no outside knowledge).
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


# ── Endpoints ────────────────────────────────────────────────
@router.post("/check")
def check(req: CheckRequest):
    did = _validate_ids(req.doc_id, req.client_id)
    meta = cache.get_key(f"pdf:meta:{did}")
    if isinstance(meta, dict):
        return {"exists": True, "meta": meta}
    return {"exists": False}


@router.post("/ingest")
def ingest(req: IngestRequest):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("ingest", LIMIT_INGEST_DAILY, req.client_id)

    text = req.text
    if len(text) > MAX_TEXT_CHARS:
        raise _err(400, "too_large",
                   f"This book is too large (max ~{MAX_TEXT_CHARS:,} characters of text).")
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise _err(400, "no_text",
                   "Not enough readable text — this may be a scanned PDF without selectable text.")

    # The doc_id is shared across ALL users (dedup) — never trust the client's
    # hash. A poisoned hash would serve wrong content to everyone else.
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != req.doc_id:
        raise _err(400, "hash_mismatch", "Document fingerprint mismatch — please retry the upload.")

    # Dedup fast path: someone already ingested this exact text.
    existing = cache.get_key(f"pdf:meta:{did}")
    if isinstance(existing, dict):
        return {"status": "exists", "meta": existing}

    if not cache.acquire_lock(f"pdf:lock:ingest:{did}", ttl=180):
        raise _err(409, "ingest_in_progress",
                   "This book is being prepared by another request — try again shortly.")

    try:
        chunks = _chunk_text(text)
        n_blocks = (len(chunks) + CHUNKS_PER_BLOCK - 1) // CHUNKS_PER_BLOCK
        for i in range(n_blocks):
            block = chunks[i * CHUNKS_PER_BLOCK:(i + 1) * CHUNKS_PER_BLOCK]
            if not cache.set_key_strict(f"pdf:block:{did}:{i}", block, ttl=TTL_SECONDS):
                raise _err(500, "storage_failed", "Temporary storage problem — please try again.")

        # Digest is an enhancement, not a requirement: on failure store empty
        # and continue — chat still works from retrieval alone.
        digest = ""
        try:
            digest = _generate_digest(chunks, req.title)
        except Exception as e:
            log.warning(f"Digest generation failed for {did}: {e}")
        cache.set_key(f"pdf:digest:{did}", {"digest": digest}, ttl=TTL_SECONDS)

        meta = {
            "title": req.title or req.filename or "Untitled document",
            "filename": req.filename,
            "pages": req.pages,
            "chars": len(text),
            "n_chunks": len(chunks),
            "n_blocks": n_blocks,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Meta last — its existence is the "document ready" commit marker.
        if not cache.set_key_strict(f"pdf:meta:{did}", meta, ttl=TTL_SECONDS, l1=True):
            raise _err(500, "storage_failed", "Temporary storage problem — please try again.")
        return {"status": "ready", "meta": meta}
    except HTTPException:
        cache.delete_key(f"pdf:lock:ingest:{did}")
        raise
    except Exception as e:
        cache.delete_key(f"pdf:lock:ingest:{did}")
        log.error(f"Ingest failed for {did}: {e}")
        raise _err(500, "storage_failed", "Something went wrong preparing this book — please try again.")


@router.post("/chat")
def chat(req: ChatRequest):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("chat", LIMIT_CHAT_DAILY, req.client_id)

    _, chunks = _load_doc(did)
    digest = _get_digest(did)
    retrieved = _retrieve_chunks(req.question, chunks)
    prompt = _build_chat_prompt(digest, retrieved, req.history, req.question)

    config = genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=1024)
    answer = gemini_client.generate(prompt, config)

    sources = [{"chunk_index": i, "snippet": c[:200]} for i, c in retrieved]
    if answer.strip() == NOT_FOUND_PHRASE:
        sources = []  # nothing actually supported the answer
    return {"answer": answer, "sources": sources}


@router.post("/quiz")
def quiz(req: QuizRequest):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("quiz", LIMIT_QUIZ_DAILY, req.client_id)

    _, chunks = _load_doc(did)
    digest = _get_digest(did)

    questions = []
    for attempt in range(2):  # one retry if nothing verifies
        try:
            questions = _generate_quiz(digest, chunks, req.count)
        except Exception as e:
            log.warning(f"Quiz generation attempt {attempt + 1} failed for {did}: {e}")
            questions = []
        if questions:
            break
    if not questions:
        raise _err(502, "quiz_failed",
                   "Couldn't build a verified quiz right now — please try again in a minute.")
    return {"questions": questions, "requested": req.count, "verified": len(questions)}
