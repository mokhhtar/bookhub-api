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

Multi-model fallback: FALLBACK_MODEL_NAME (gemini-3.5-flash-lite) is a
SEPARATE model from MODEL_NAME with its OWN free-tier quota bucket —
confirmed live 2026-08-25 (a fresh 15 RPM / 250K TPM / 500 RPD allowance,
unused, alongside gemini-3.1-flash-lite's own). Every configured key is
tried against MODEL_NAME first, in full, before any key is tried against
FALLBACK_MODEL_NAME — a second model is extra daily quota, not a reason
to abandon the primary model early. Only once every key has failed on
BOTH models does the Groq fallback below run.

Groq fallback: if every Gemini key fails on every Gemini model, GROQ_API_KEY
(optional) is tried last via Groq's OpenAI-compatible REST endpoint over
httpx (already a dependency — no new SDK). Same prompt string reaches Groq
as reached Gemini; this module never rewrites prompts per-provider, so
output shape stays comparable and downstream verification (e.g.
quiz_core.py's supporting_quote match) still guards whichever backend
answered.
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
# A distinct model, a distinct free-tier quota bucket. Tried only after
# every key has exhausted MODEL_NAME — see the module docstring.
FALLBACK_MODEL_NAME = "gemini-3.5-flash-lite"

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

# WAS llama-3.3-70b-versatile, WHICH NO LONGER EXISTS FOR THIS ACCOUNT.
# Verified live 2026-08-17: that id and llama-3.1-8b-instant both answer
# 404 "The model ... does not exist or you do not have access to it",
# while `GET /v1/models` lists 13 models and none of them is a Llama chat
# model. Groq removed them.
#
# This failed SILENTLY, which is the part worth remembering. The whole
# point of this fallback is to carry the site when every Gemini key is
# spent, and `_groq_generate` is deliberately fail-open — it logs a
# warning and returns None. So a dead model does not look like an
# outage; it looks exactly like "the AI had nothing to say", which is a
# normal, expected answer everywhere in this codebase. Nothing surfaces.
# Found only because an unrelated harvest script called the same path
# directly and raised.
#
# gpt-oss-120b rather than the 20b: the fallback only ever runs after
# every Gemini key has failed, so it is rare and quality matters more
# than latency. Both the plain and the streaming path were re-verified
# against it before this line changed.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
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


def check_groq():
    """Diagnostics only: a real, minimal call straight to Groq — bypassing
    Gemini entirely — so connectivity can be confirmed without waiting for
    every Gemini key to fail first. Exists because this repo has a known
    precedent (Gutendex) of an external API being blocked from Render's
    datacenter IP while working fine locally; this is how that gets
    checked for Groq specifically, on demand rather than automatically."""
    if not GROQ_API_KEY:
        return {"configured": False}
    text = _groq_generate("Reply with exactly one word: OK", DEFAULT_CONFIG)
    return {"configured": True, "reachable": bool(text), "model": GROQ_MODEL}


def generate(prompt: str, config: genai_types.GenerateContentConfig = None) -> str:
    """Single shared call path. Every tool module calls this — never the SDK directly.

    Tries each configured Gemini key against MODEL_NAME first, in full;
    only once every key has failed there does the same key sweep repeat
    against FALLBACK_MODEL_NAME (a second, independent free-tier quota
    bucket). Falls through to Groq only once every key has failed on both
    models. A key that errors (quota, rate limit, transient outage) is
    logged and skipped rather than breaking the whole request, mirroring
    this repo's other provider fallback chains.
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="AI service not configured.")
    cfg = config or DEFAULT_CONFIG
    for model in (MODEL_NAME, FALLBACK_MODEL_NAME):
        for i, client in enumerate(_clients):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=cfg,
                )
                text = (response.text or "").strip()
                if text:
                    if model != MODEL_NAME or i > 0:
                        log.warning(f"Fell through to {model} key #{i + 1} for a response.")
                    return text
                log.warning(f"{model} key #{i + 1} returned an empty response.")
            except Exception as e:
                log.warning(f"{model} key #{i + 1} failed: {e}")
    groq_text = _groq_generate(prompt, cfg)
    if groq_text:
        log.warning("All Gemini keys failed on every model — Groq fallback answered.")
        return groq_text
    log.error("All Gemini keys, both models, and the Groq fallback failed or returned empty responses.")
    raise HTTPException(status_code=502, detail="AI generation failed: all providers exhausted.")


def generate_stream(prompt: str, config: genai_types.GenerateContentConfig = None):
    """
    Streaming variant of generate(): yields text chunks as Gemini produces
    them. Used by /summary/stream so the reader sees text within seconds
    instead of waiting for the full generation.

    Falls through to the next Gemini key, then the next Gemini model
    (FALLBACK_MODEL_NAME — a second, independent free-tier quota bucket,
    tried only once every key has failed on MODEL_NAME), and finally to
    Groq, only while *starting* the stream (before any chunk has been
    yielded to the caller) — once bytes are on their way to the client we
    don't switch backends mid-stream; a mid-stream error just propagates,
    same as before.

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
    for model in (MODEL_NAME, FALLBACK_MODEL_NAME):
        if iterator is not None:
            break
        for i, client in enumerate(_clients):
            try:
                it = iter(client.models.generate_content_stream(
                    model=model,
                    contents=prompt,
                    config=cfg,
                ))
                first = next(it)  # forces the real request; raises on a bad key
                iterator = it
                first_chunk_text = getattr(first, "text", None)
                if model != MODEL_NAME or i > 0:
                    log.warning(f"Fell through to {model} key #{i + 1} to start a stream.")
                break
            except StopIteration:
                iterator = iter(())  # started fine, just an empty stream
                if model != MODEL_NAME or i > 0:
                    log.warning(f"Fell through to {model} key #{i + 1} to start a stream.")
                break
            except Exception as e:
                log.warning(f"{model} key #{i + 1} stream setup failed: {e}")
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
        log.warning("All Gemini keys failed on every model to start a stream — Groq fallback started one.")
        for chunk in groq_stream:
            yield chunk
        return
    log.error("All Gemini keys, both models, and the Groq fallback failed to start a stream.")
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
