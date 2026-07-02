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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import gemini_client
from tools import summary as summary_tool
from tools import fandom as fandom_tool
# from tools import recommend as recommend_tool   # pending rebuild
# from tools import questions as questions_tool   # pending rebuild
# from tools import compare as compare_tool       # pending rebuild
# from tools import similar as similar_tool       # folded into summary.py's similar_books

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bookhub-api")

app = FastAPI(title="BookHub API", version="2.0.0")

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

# ── Startup event to clear cache ───────────────────────────
@app.on_event("startup")
def clear_cache_on_startup():
    try:
        import shutil
        from cache import CACHE_DIR
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
            CACHE_DIR.mkdir(exist_ok=True)
            log.info("Disk cache cleared successfully on startup.")
    except Exception as e:
        log.warning(f"Failed to clear cache on startup: {e}")


# ── Mount each tool's router independently ─────────────────
app.include_router(summary_tool.router, tags=["summary"])
app.include_router(fandom_tool.router, tags=["fandom"])


@app.get("/health")
def health():
    """Used by UptimeRobot / cron-job.org to keep the Render instance awake."""
    return {
        "status": "ok",
        "model": gemini_client.MODEL_NAME,
        "configured": gemini_client.is_configured(),
        "amazon_api_configured": bool(os.environ.get("AMAZON_CREDENTIAL_ID") and os.environ.get("AMAZON_CREDENTIAL_SECRET")),
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
        "name": "BookHub API",
        "version": "2.0.0",
        "active_endpoints": ["/summary", "/fandom/resolve", "/fandom/universe", "/health", "/models"],
        "note": "Other tools (recommend, questions, compare) are being rebuilt "
                "with the same grounding pipeline as /summary before re-enabling.",
        "docs": "/docs",
    }
