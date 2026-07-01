"""
gemini_client.py — single shared Gemini client used by every tool module.

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
"""

import os
import logging
from fastapi import HTTPException
from google import genai
from google.genai import types as genai_types

log = logging.getLogger("bookhub-api.gemini")

MODEL_NAME = "gemini-3.1-flash-lite"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY is not set — AI endpoints will return 503 until configured.")

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

DEFAULT_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.3,
    max_output_tokens=4096,
)


def is_configured() -> bool:
    return _client is not None


def generate(prompt: str, config: genai_types.GenerateContentConfig = None) -> str:
    """Single shared call path. Every tool module calls this — never the SDK directly."""
    if not _client:
        raise HTTPException(status_code=503, detail="AI service not configured.")
    try:
        response = _client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config or DEFAULT_CONFIG,
        )
        text = (response.text or "").strip()
        if not text:
            raise HTTPException(status_code=502, detail="AI returned an empty response.")
        return text
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Gemini call failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {str(e)}")


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