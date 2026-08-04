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

Groq fallback: if every Gemini key fails, GROQ_API_KEY (optional) is tried
last via Groq's OpenAI-compatible REST endpoint over httpx (already a
dependency — no new SDK). Same prompt string reaches Groq as reached
Gemini; this module never rewrites prompts per-provider, so output shape
stays comparable and downstream verification (e.g. quiz_core.py's
supporting_quote match) still guards whichever backend answered.
"""

import os
import json
import logging
import httpx
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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip() or None
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

if GROQ_API_KEY:
    log.info(f"Groq fallback configured (model={GROQ_MODEL}) — used only once every Gemini key has failed.")

DEFAULT_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.3,
    max_output_tokens=4096,
)


def is_configured() -> bool:
    return len(_clients) > 0 or GROQ_API_KEY is not None


def _groq_generate(prompt: str, config: genai_types.GenerateContentConfig):
    """Final fallback once every Gemini key has failed. Never raises — mirrors
    the fail-open shape of the Gemini attempts above; returns None on any
    failure so the caller can report the same "all providers exhausted"
    error it always has."""
    if not GROQ_API_KEY:
        return None
    try:
        resp = httpx.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": config.temperature,
                "max_tokens": config.max_output_tokens,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or None
    except Exception as e:
        log.warning(f"Groq fallback failed: {e}")
        return None


def _groq_generate_stream(prompt: str, config: genai_types.GenerateContentConfig):
    """Streaming Groq fallback. Returns a generator on success, None if the
    stream couldn't even be started (mirrors _groq_generate's fail-open
    shape) — mirrors the Gemini streaming loop's "fail before first byte"
    contract in generate_stream()."""
    if not GROQ_API_KEY:
        return None
    client = httpx.Client(timeout=60.0)
    try:
        request = client.build_request(
            "POST", GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": config.temperature,
                "max_tokens": config.max_output_tokens,
                "stream": True,
            },
        )
        response = client.send(request, stream=True)
        response.raise_for_status()
    except Exception as e:
        log.warning(f"Groq stream setup failed: {e}")
        client.close()
        return None

    def _iter_groq_chunks():
        try:
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[len("data: "):].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content")
                except Exception:
                    continue
                if delta:
                    yield delta
        finally:
            response.close()
            client.close()

    return _iter_groq_chunks()


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
    fallback chains. Falls through to Groq only once every Gemini key has
    failed.
    """
    if not is_configured():
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
    groq_text = _groq_generate(prompt, cfg)
    if groq_text:
        log.warning("All Gemini keys failed — Groq fallback answered.")
        return groq_text
    log.error("All Gemini keys and the Groq fallback failed or returned empty responses.")
    raise HTTPException(status_code=502, detail="AI generation failed: all providers exhausted.")


def generate_stream(prompt: str, config: genai_types.GenerateContentConfig = None):
    """
    Streaming variant of generate(): yields text chunks as Gemini produces
    them. Used by /summary/stream so the reader sees text within seconds
    instead of waiting for the full generation.

    Falls through to the next Gemini key, and finally to Groq, only while
    *starting* the stream (before any chunk has been yielded to the
    caller) — once bytes are on their way to the client we don't switch
    backends mid-stream; a mid-stream error just propagates, same as
    before.

    Note: the SDK's generate_content_stream() is lazy — it doesn't make
    the actual network call (and so doesn't raise on e.g. an invalid key)
    until the returned iterator is first advanced. Pulling the first chunk
    inside the try below is what actually exercises the key; without it,
    a bad key's error would surface later in the unguarded loop instead
    of triggering fallback.
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="AI service not configured.")
    cfg = config or DEFAULT_CONFIG
    iterator = None
    first_chunk_text = None
    for i, client in enumerate(_clients):
        try:
            it = iter(client.models.generate_content_stream(
                model=MODEL_NAME,
                contents=prompt,
                config=cfg,
            ))
            first = next(it)  # forces the real request; raises on a bad key
            iterator = it
            first_chunk_text = getattr(first, "text", None)
            if i > 0:
                log.warning(f"Gemini key #1..#{i} failed to start a stream — key #{i + 1} started one.")
            break
        except StopIteration:
            iterator = iter(())  # started fine, just an empty stream
            if i > 0:
                log.warning(f"Gemini key #1..#{i} failed to start a stream — key #{i + 1} started one.")
            break
        except Exception as e:
            log.warning(f"Gemini key #{i + 1} stream setup failed: {e}")
    if iterator is not None:
        if first_chunk_text:
            yield first_chunk_text
        for chunk in iterator:
            text = getattr(chunk, "text", None)
            if text:
                yield text
        return
    groq_stream = _groq_generate_stream(prompt, cfg)
    if groq_stream is not None:
        log.warning("All Gemini keys failed to start a stream — Groq fallback started one.")
        for chunk in groq_stream:
            yield chunk
        return
    log.error("All Gemini keys and the Groq fallback failed to start a stream.")
    raise HTTPException(status_code=502, detail="AI generation failed: all providers exhausted.")


def parse_json_response(text: str):
    """Gemini sometimes wraps JSON in ```json fences — strip them before parsing."""
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
