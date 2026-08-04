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

from fastapi import FastAPI, Request, HTTPException
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
from tools import indexing as indexing_tool
# from tools import recommend as recommend_tool   # pending rebuild
# from tools import questions as questions_tool   # pending rebuild
# from tools import compare as compare_tool       # pending rebuild
# from tools import similar as similar_tool       # folded into summary.py's similar_books

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bookhub-api")

# Diagnostic surfaces (verbose /health, /models, the / route listing, and the
# auto-generated API docs) disclose configuration and a full route/schema map
# that only aids reconnaissance. Gate them behind an explicit opt-in; prod
# stays minimal (M-07). Set EXPOSE_DIAGNOSTICS=true in the dashboard to inspect.
EXPOSE_DIAGNOSTICS = os.environ.get("EXPOSE_DIAGNOSTICS", "false").lower() == "true"

app = FastAPI(
    title="Litheca API",
    version="2.0.0",
    docs_url="/docs" if EXPOSE_DIAGNOSTICS else None,
    redoc_url="/redoc" if EXPOSE_DIAGNOSTICS else None,
    openapi_url="/openapi.json" if EXPOSE_DIAGNOSTICS else None,
)

# Explicit CORS allow-list. NEVER fall back to "*" (L-08): an unset, blank, or
# bare "*" env value must fail SAFE to the known production origins, not open
# the API to every website. Override with a comma-separated ALLOWED_ORIGINS in
# the dashboard (already set in prod); for a non-standard local port, add it
# there rather than reintroducing a wildcard.
_DEFAULT_ORIGINS = [
    "http://localhost:4000",       # Jekyll dev server
    "https://mokhhtar.github.io",  # GitHub Pages
    "https://litheca.com",
    "https://www.litheca.com",
]
raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
if raw_origins and raw_origins != "*":
    ALLOWED_ORIGINS = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
else:
    ALLOWED_ORIGINS = list(_DEFAULT_ORIGINS)

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
app.include_router(indexing_tool.router, tags=["indexing"])


@app.get("/health")
def health():
    """Liveness probe for Render's health check + the keep-warm workflow.
    Minimal by default; the verbose config block is gated behind
    EXPOSE_DIAGNOSTICS so production doesn't disclose it (M-07). Always 200 so
    the keep-warm ping still wakes the instance."""
    if not EXPOSE_DIAGNOSTICS:
        return {"status": "ok"}
    import github_publisher
    import cache
    return {
        "status": "ok",
        "model": gemini_client.MODEL_NAME,
        "configured": gemini_client.is_configured(),
        # Multi-key/multi-provider fallback status — counts and booleans
        # only, never the key values themselves.
        "gemini_keys_configured": len(gemini_client.GEMINI_API_KEYS),
        "groq_configured": gemini_client.GROQ_API_KEY is not None,
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
    # Restricted in production (M-07): enumerates provider models AND makes a
    # live upstream call on every hit (to Gemini AND, if configured, Groq).
    # 404 unless diagnostics are enabled. Never hit by the keep-warm workflow.
    if not EXPOSE_DIAGNOSTICS:
        raise HTTPException(status_code=404, detail="Not found")
    result = {}
    try:
        result["models"] = gemini_client.list_gemini_models()
    except Exception as e:
        result["gemini_error"] = str(e)
    result["groq"] = gemini_client.check_groq()
    return result


@app.get("/")
def root():
    # Minimal by default (M-07) — the full route list + build notes only aid
    # recon. Verbose form behind EXPOSE_DIAGNOSTICS.
    if not EXPOSE_DIAGNOSTICS:
        return {"name": "Litheca API", "status": "ok"}
    return {
        "name": "Litheca API",
        "version": "2.0.0",
        "active_endpoints": ["/summary", "/daily", "/pdfchat/check", "/pdfchat/ingest", "/pdfchat/chat", "/pdfchat/quiz", "/fandom/resolve", "/fandom/universe", "/health", "/models"],
        "note": "Other tools (recommend, questions, compare) are being rebuilt "
                "with the same grounding pipeline as /summary before re-enabling.",
        "docs": "/docs",
    }
