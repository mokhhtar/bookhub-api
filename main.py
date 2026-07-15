"""
BookHub API — FastAPI application entrypoint.

This file is intentionally thin: it only wires up CORS, mounts each
tool's router, and exposes /health. All actual logic lives in
tools/<tool_name>.py — each tool is a fully independent module with its
own request model, prompt, and route. Tools do not import each other.

Currently active:
  tools/summary.py  → POST /summary   (priority #1 — see plan)

Tools below are STUBS pending the same rebuild treatment as summary.py
(grounded via book_data.py + Gemini 3.1 Flash-Lite). They are commented
out of the router includes until rebuilt, so the API only exposes what
has actually been hardened.
"""

import os
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

import gemini_client
from tools import summary as summary_tool
from tools import fandom as fandom_tool
from tools import daily as daily_tool
from tools import pdfchat as pdfchat_tool
from tools import nyt as nyt_tool
from tools import reader as reader_tool
from tools import quiz as quiz_tool
# from tools import recommend as recommend_tool   # pending rebuild
# from tools import questions as questions_tool   # pending rebuild
# from tools import compare as compare_tool       # pending rebuild
# from tools import similar as similar_tool       # folded into summary.py's similar_books

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bookhub-api")

app = FastAPI(title="Litheca API", version="2.0.0")

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

# Compress responses >1KB (summary payloads are large HTML/JSON — typically
# 70%+ smaller gzipped, a big win on Render's small free-tier egress).
app.add_middleware(GZipMiddleware, minimum_size=1024)


class _NoGzipForStream:
    """
    Strip Accept-Encoding for the SSE route BEFORE GZipMiddleware sees it.
    Gzip buffers streamed chunks (in the middleware and at proxies), which
    defeats the whole point of /summary/stream — first tokens on screen in
    seconds. Added AFTER GZipMiddleware so it wraps it (outermost runs first).
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").endswith("/summary/stream"):
            scope = dict(scope)
            scope["headers"] = [
                (k, v) for (k, v) in scope["headers"] if k.lower() != b"accept-encoding"
            ]
        await self.app(scope, receive, send)


app.add_middleware(_NoGzipForStream)

# Browser/CDN cache windows for cacheable GET endpoints (first matching
# prefix wins). These mirror how volatile each payload actually is; the
# server-side Redis cache stays the source of truth — this just stops the
# SAME browser re-downloading an identical response on every visit.
_CACHE_RULES: list[tuple[str, int]] = [
    ("/read/", 7 * 86400),       # public-domain book text — immutable
    ("/resolve/", 7 * 86400),    # share-slug → book mapping — stable
    ("/search", 86400),          # catalog search results
    ("/author/", 86400),         # /author/works + /author/info — both stable
    ("/nyt/", 3600),             # weekly lists; hourly is plenty
    ("/daily", 3600),            # day-scoped payload; re-check hourly
]


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    response = await call_next(request)
    if (
        request.method == "GET"
        and response.status_code == 200
        and "cache-control" not in response.headers
    ):
        path = request.url.path
        for prefix, ttl in _CACHE_RULES:
            if path.startswith(prefix):
                response.headers["Cache-Control"] = f"public, max-age={ttl}"
                break
    return response

# ── Mount each tool's router independently ─────────────────
app.include_router(summary_tool.router, tags=["summary"])
app.include_router(fandom_tool.router, tags=["fandom"])
app.include_router(daily_tool.router, tags=["daily"])
app.include_router(pdfchat_tool.router, tags=["pdfchat"])
app.include_router(nyt_tool.router, tags=["nyt"])
app.include_router(reader_tool.router, tags=["reader"])
app.include_router(quiz_tool.router, tags=["quiz"])


@app.get("/health")
def health():
    """Used by UptimeRobot / cron-job.org to keep the Render instance awake."""
    import github_publisher
    import cache
    return {
        "status": "ok",
        "model": gemini_client.MODEL_NAME,
        "configured": gemini_client.is_configured(),
        "amazon_api_configured": bool(os.environ.get("AMAZON_CREDENTIAL_ID") and os.environ.get("AMAZON_CREDENTIAL_SECRET")),
        # Static-page publishing to the Jekyll repo (GITHUB_PUBLISH_ENABLED
        # + PAT). Surfaced here so flipping the env var is verifiable at a glance.
        "publishing": github_publisher.is_enabled(),
        # True while the dev switch DISABLE_RESPONSE_CACHE is on (summaries
        # recompute fresh every request).
        "response_cache_disabled": cache.RESPONSE_CACHE_DISABLED,
    }


@app.get("/models")
def list_models():
    if not gemini_client.is_configured():
        return {"error": "Client not initialized"}
    try:
        models = [m.name for m in gemini_client._client.models.list()]
        return {"models": models}
    except Exception as e:
        return {"error": str(e)}


@app.get("/")
def root():
    return {
        "name": "Litheca API",
        "version": "2.0.0",
        "active_endpoints": ["/summary", "/daily", "/pdfchat/check", "/pdfchat/ingest", "/pdfchat/chat", "/pdfchat/quiz", "/fandom/resolve", "/fandom/universe", "/health", "/models"],
        "note": "Other tools (recommend, questions, compare) are being rebuilt "
                "with the same grounding pipeline as /summary before re-enabling.",
        "docs": "/docs",
    }
