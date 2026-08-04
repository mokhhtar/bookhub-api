"""
gemini_client.py — single shared AI client used by every tool module.

Model: gemini-3.1-flash-lite (confirmed live name as of June 2026 — the
       successor to the now-retired 2.0 Flash-Lite line; NOT the
       "-preview" suffixed variant, which is being discontinued).

Thinking level "low": book summarization/extraction from grounded context
is not a hard reasoning task — we want fast, cheap, instruction-following
behavior, not deep multi-step thinking. "low" is the right tier per
Google's guidance for high-frequency, lightweight tasks.

Temperature 0.3: lower than the previous 0.7. We are NOT asking the model
to be creative — we are asking it to faithfully summarize GIVEN context.
Lower temperature reduces the model's tendency to embellish beyond the
grounding data it was given.

Multi-key fallback: GEMINI_API_KEYS (comma-separated, same split idiom as
main.py's ALLOWED_ORIGINS) lets Render run more than one Gemini key so a
single key hitting its quota doesn't take every AI endpoint down at once.
Falls back to the single GEMINI_API_KEY var if GEMINI_API_KEYS is unset.
Every key is tried in order; each attempt is wrapped so a failure never
raises past this module — same fail-open shape as book_data.py's Google
Books -> Open Library chain and cache.py's L1 -> L2 layering. Only when
every key has failed does generate()/generate_stream() raise.
"""

import os
import logging
from fastapi import HTTPException
from google import genai
from google.genai import types as genai_types

log = logging.getLogger("bookhub-api.gemini")

MODEL_NAME = "gemini-3.1-flash-lite"

_raw_keys = os.environ.get("GEMINI_API_KEYS", "").strip()
if _raw_keys:
    GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
else:
    _single_key = os.environ.get("GEMINI_API_KEY", "").strip()
    GEMINI_API_KEYS = [_single_key] if _single_key else []

if not GEMINI_API_KEYS:
    log.warning("No Gemini API key configured (GEMINI_API_KEYS/GEMINI_API_KEY) — AI endpoints will return 503 until configured.")
elif len(GEMINI_API_KEYS) > 1:
    log.info(f"{len(GEMINI_API_KEYS)} Gemini API keys configured — will fall through on failure.")

_clients = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]

DEFAULT_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.3,
    max_output_tokens=4096,
)


def is_configured() -> bool:
    return len(_clients) > 0


def list_gemini_models():
    """Live model list from the first configured Gemini key. Diagnostics only."""
    if not _clients:
        raise HTTPException(status_code=503, detail="AI service not configured.")
    return [m.name for m in _clients[0].models.list()]


def generate(prompt: str, config: genai_types.GenerateContentConfig = None) -> str:
    """Single shared call path. Every tool module calls this — never the SDK directly.

    Tries each configured Gemini key in order; a key that errors (quota,
    rate limit, transient outage) is logged and skipped rather than
    breaking the whole request, mirroring this repo's other provider
    fallback chains.
    """
    if not _clients:
        raise HTTPException(status_code=503, detail="AI service not configured.")
    cfg = config or DEFAULT_CONFIG
    for i, client in enumerate(_clients):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=cfg,
            )
            text = (response.text or "").strip()
            if text:
                if i > 0:
                    log.warning(f"Gemini key #1..#{i} failed — key #{i + 1} answered.")
                return text
            log.warning(f"Gemini key #{i + 1} returned an empty response.")
        except Exception as e:
            log.warning(f"Gemini key #{i + 1} failed: {e}")
    log.error("All Gemini keys failed or returned empty responses.")
    raise HTTPException(status_code=502, detail="AI generation failed: all Gemini keys exhausted.")


def generate_stream(prompt: str, config: genai_types.GenerateContentConfig = None):
    """
    Streaming variant of generate(): yields text chunks as Gemini produces
    them. Used by /summary/stream so the reader sees text within seconds
    instead of waiting for the full generation.

    Falls through to the next Gemini key only while *starting* the stream
    (before any chunk has been yielded to the caller) — once bytes are on
    their way to the client we don't switch keys mid-stream; a mid-stream
    error just propagates, same as before.
    """
    if not _clients:
        raise HTTPException(status_code=503, detail="AI service not configured.")
    cfg = config or DEFAULT_CONFIG
    stream = None
    for i, client in enumerate(_clients):
        try:
            stream = client.models.generate_content_stream(
                model=MODEL_NAME,
                contents=prompt,
                config=cfg,
            )
            if i > 0:
                log.warning(f"Gemini key #1..#{i} failed to start a stream — key #{i + 1} started one.")
            break
        except Exception as e:
            log.warning(f"Gemini key #{i + 1} stream setup failed: {e}")
    if stream is None:
        log.error("All Gemini keys failed to start a stream.")
        raise HTTPException(status_code=502, detail="AI generation failed: all Gemini keys exhausted.")
    for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            yield text


def parse_json_response(text: str):
    """Gemini sometimes wraps JSON in ```json fences — strip them before parsing."""
    import json
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.error(f"Failed to parse JSON from Gemini: {text[:200]}")
        raise HTTPException(status_code=502, detail="AI returned an unexpected format. Please try again.")
