"""
BookHub API — FastAPI backend powering the AI tools on bookhub.

Endpoints:
  POST /summary     — book summary at quick/medium/deep depth
  POST /questions    — 10 discussion questions
  POST /recommend    — 5 recommendations based on favorite books
  POST /similar       — 4 books similar to a given title
  POST /compare      — side-by-side comparison of two books
  GET  /health         — uptime check (used by UptimeRobot ping)

All AI calls go through Gemini 1.5 Flash (free tier: 1500 req/day) and are
cached for 30 days so repeat requests for the same book cost zero API calls.
"""

import os
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from google.genai import types as genai_types

import cache
import prompts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bookhub-api")

# ── Gemini setup ────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY is not set — requests will fail until configured on Render.")

MODEL_NAME = "gemini-1.5-flash"
_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GENERATION_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.7,
    max_output_tokens=1024,
)

# ── App setup ───────────────────────────────────────────────
app = FastAPI(title="BookHub API", version="1.0.0")

# CORS — allow your GitHub Pages origin (and localhost for dev).
raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
if not raw_origins or raw_origins.strip() in ("", "*"):
    ALLOWED_ORIGINS = ["*"]
else:
    ALLOWED_ORIGINS = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

log.info(f"CORS Allowed Origins: {ALLOWED_ORIGINS}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Request / response models ──────────────────────────────
class SummaryRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: Optional[str] = Field(default="", max_length=200)
    depth: str = Field(default="quick", pattern="^(quick|medium|deep)$")


class QuestionsRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: Optional[str] = Field(default="", max_length=200)


class RecommendRequest(BaseModel):
    books: str = Field(..., min_length=1, max_length=500)
    mood: Optional[str] = Field(default="", max_length=200)


class SimilarRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    genre: Optional[str] = Field(default="", max_length=100)


class CompareRequest(BaseModel):
    book_a: str = Field(..., min_length=1, max_length=200)
    book_b: str = Field(..., min_length=1, max_length=200)


# ── Helpers ─────────────────────────────────────────────────
def _call_gemini(prompt: str) -> str:
    if not _client:
        raise HTTPException(status_code=503, detail="AI service not configured.")
    try:
        response = _client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=GENERATION_CONFIG,
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"Gemini call failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {str(e)}")


def _parse_json_response(text: str):
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


def _amazon_url(title: str) -> str:
    tag = os.environ.get("AMAZON_TAG", "yourtag-20")
    import urllib.parse
    q = urllib.parse.quote(title)
    return f"https://www.amazon.com/s?k={q}&tag={tag}"


# ── Routes ──────────────────────────────────────────────────

@app.get("/health")
def health():
    """Used by UptimeRobot / cron-job.org to keep the Render instance awake."""
    return {"status": "ok", "model": MODEL_NAME, "configured": _client is not None}


@app.post("/summary")
def summary(req: SummaryRequest):
    cached = cache.get("summary", req.title, req.author, req.depth)
    if cached:
        return cached

    prompt = prompts.summary_prompt(req.title, req.author, req.depth)
    text = _call_gemini(prompt)

    result = {
        "title": req.title,
        "author": req.author,
        "depth": req.depth,
        "summary": text,
        "amazon_url": _amazon_url(req.title),
    }
    cache.set(result, "summary", req.title, req.author, req.depth)
    return result


@app.post("/questions")
def questions(req: QuestionsRequest):
    cached = cache.get("questions", req.title, req.author)
    if cached:
        return cached

    prompt = prompts.questions_prompt(req.title, req.author)
    text = _call_gemini(prompt)
    parsed = _parse_json_response(text)

    if not isinstance(parsed, list):
        raise HTTPException(status_code=502, detail="Unexpected response format.")

    result = {"title": req.title, "questions": parsed[:10]}
    cache.set(result, "questions", req.title, req.author)
    return result


@app.post("/recommend")
def recommend(req: RecommendRequest):
    cached = cache.get("recommend", req.books, req.mood)
    if cached:
        return cached

    prompt = prompts.recommend_prompt(req.books, req.mood)
    text = _call_gemini(prompt)
    parsed = _parse_json_response(text)

    if not isinstance(parsed, list):
        raise HTTPException(status_code=502, detail="Unexpected response format.")

    result = {"recommendations": parsed[:5]}
    cache.set(result, "recommend", req.books, req.mood)
    return result


@app.post("/similar")
def similar(req: SimilarRequest):
    cached = cache.get("similar", req.title, req.genre)
    if cached:
        return cached

    prompt = prompts.similar_prompt(req.title, req.genre)
    text = _call_gemini(prompt)
    parsed = _parse_json_response(text)

    if not isinstance(parsed, list):
        raise HTTPException(status_code=502, detail="Unexpected response format.")

    result = {"books": parsed[:4]}
    cache.set(result, "similar", req.title, req.genre)
    return result


@app.post("/compare")
def compare(req: CompareRequest):
    cached = cache.get("compare", req.book_a, req.book_b)
    if cached:
        return cached

    prompt = prompts.compare_prompt(req.book_a, req.book_b)
    text = _call_gemini(prompt)
    parsed = _parse_json_response(text)

    if not isinstance(parsed, dict) or "rows" not in parsed:
        raise HTTPException(status_code=502, detail="Unexpected response format.")

    cache.set(parsed, "compare", req.book_a, req.book_b)
    return parsed


@app.get("/")
def root():
    return {
        "name": "BookHub API",
        "endpoints": ["/summary", "/questions", "/recommend", "/similar", "/compare", "/health"],
        "docs": "/docs",
    }
