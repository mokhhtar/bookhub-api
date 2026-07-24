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
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from google.genai import types as genai_types

import cache
import gemini_client
# Quiz core (chunking, quote-verified MCQ generation, rate limiting) moved to
# tools/quiz_core.py so the per-BOOK quiz route (tools/quiz.py) can share it.
from tools.quiz_core import _chunk_text, _generate_quiz, _rate_limit, _client_ip

log = logging.getLogger("bookhub-api.tools.pdfchat")

router = APIRouter(prefix="/pdfchat")

# ── Constants (env-overridable) ──────────────────────────────
TTL_SECONDS = int(os.environ.get("PDFCHAT_TTL_SECONDS", 172800))  # 48h
MAX_TEXT_CHARS = int(os.environ.get("PDFCHAT_MAX_TEXT_CHARS", 1_200_000))
MIN_TEXT_CHARS = 500
# CHUNK_CHARS / QUIZ_SAMPLE_CHUNKS now live in tools/quiz_core.py
CHUNKS_PER_BLOCK = 64          # ~192KB JSON per key — under Upstash's ~1MB cap
RETRIEVAL_TOP_K = 8
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


def _retrieve_chunks(question: str, chunks: list[str], k: int = RETRIEVAL_TOP_K,
                      history: Optional[list[dict]] = None) -> list[tuple[int, str]]:
    """
    Returns up to k (index, chunk) pairs, re-sorted into book order, plus the
    immediate neighbors of the single best-scoring chunk (chunks are stored
    with NO overlap, so a fact can be split right across a chunk boundary).

    Follow-up questions ("what happened to him next?", "why did she do that?")
    carry almost no retrievable keywords on their own — pull terms from the
    last user turn too when the current question is sparse (<=3 terms), so
    multi-turn chat doesn't go blind on pronouns/back-references the way a
    single-shot question wouldn't.
    """
    terms = _question_terms(question)
    if history and len(terms) <= 3:
        for h in reversed(history):
            if h.get("role") == "user":
                terms = _question_terms(str(h.get("content", "")))[:6] + terms
                break
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
    top_indices = {i for _, i in scored[:k]}

    # Give the single best hit its neighbors for continuity across the
    # (non-overlapping) chunk boundary — cheap, and only for the top hit so
    # the context doesn't balloon.
    if scored:
        best = scored[0][1]
        for neighbor in (best - 1, best + 1):
            if 0 <= neighbor < n:
                top_indices.add(neighbor)

    top = sorted(top_indices)
    return [(i, chunks[i]) for i in top]


# ── Prompts ──────────────────────────────────────────────────
def _generate_digest(chunks: list[str], title: str) -> str:
    """One-time condensed outline. Input sampled to ~60K chars — NEVER the full text."""
    n = len(chunks)
    take = min(40, n)
    indices = sorted({round(i * (n - 1) / max(take - 1, 1)) for i in range(take)})
    sampled = "\n\n".join(f"[Excerpt {i}]\n{chunks[i][:1500]}" for i in indices)
    prompt = f"""You are reading sampled excerpts from a book{f' titled "{title}"' if title else ""}. Write a thorough DIGEST (up to 4500 characters, plain text) covering ONLY what the excerpts show: probable title/author, what kind of book it is, its main subject, structure/major parts, EVERY named person/place/term you see with a one-line descriptor, and the overall arc in some detail. Be specific and concrete rather than vague — this digest is the only context available for answering detailed questions later. No outside knowledge, no invention.

EXCERPTS:
{sampled}"""
    config = genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=2000)
    return gemini_client.generate(prompt, config).strip()[:4800]


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


# ── Endpoints ────────────────────────────────────────────────
@router.post("/check")
def check(req: CheckRequest):
    # Existence ONLY — deliberately does not return the stored meta. doc_id is a
    # content hash shared across users (dedup), so returning the first
    # uploader's chosen title/filename would leak it to anyone who ingests the
    # same file (C-01 / IDOR). The caller already holds its own metadata: the
    # file it is opening, or its local-library entry.
    did = _validate_ids(req.doc_id, req.client_id)
    return {"exists": isinstance(cache.get_key(f"pdf:meta:{did}"), dict)}


@router.post("/ingest")
def ingest(req: IngestRequest, request: Request):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("ingest", LIMIT_INGEST_DAILY, _client_ip(request))

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

    # Dedup fast path: someone already ingested this exact text. Reuse the
    # shared stored text, but echo the CALLER'S OWN metadata (from this request)
    # — never the first uploader's stored title/filename (C-01 / IDOR).
    existing = cache.get_key(f"pdf:meta:{did}")
    if isinstance(existing, dict):
        return {"status": "exists", "meta": {
            "title": req.title or req.filename or "Untitled document",
            "filename": req.filename,
            "pages": req.pages,
            "chars": len(text),
        }}

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
def chat(req: ChatRequest, request: Request):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("chat", LIMIT_CHAT_DAILY, _client_ip(request))

    _, chunks = _load_doc(did)
    digest = _get_digest(did)
    retrieved = _retrieve_chunks(req.question, chunks, history=req.history)
    prompt = _build_chat_prompt(digest, retrieved, req.history, req.question)

    config = genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=1536)
    answer = gemini_client.generate(prompt, config)

    sources = [{"chunk_index": i, "snippet": c[:200]} for i, c in retrieved]
    if answer.strip() == NOT_FOUND_PHRASE:
        sources = []  # nothing actually supported the answer
    return {"answer": answer, "sources": sources}


@router.post("/quiz")
def quiz(req: QuizRequest, request: Request):
    did = _validate_ids(req.doc_id, req.client_id)
    _rate_limit("quiz", LIMIT_QUIZ_DAILY, _client_ip(request))

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
