"""
Simple in-memory + optional file-backed cache.
Avoids re-calling Gemini for the same (title, depth) pair.

For production at scale, swap this for Upstash Redis (free tier, 10k req/day) —
see the REDIS section at the bottom, commented out and ready to enable.
"""
import json
import time
import hashlib
import os
from pathlib import Path
from typing import Optional

CACHE_DIR = Path("/tmp/bookhub_cache")
CACHE_DIR.mkdir(exist_ok=True)

# In-memory layer (fastest, cleared on restart)
_mem_cache: dict[str, tuple[float, any, float]] = {}

# TTL: 30 days — book summaries don't change
TTL_SECONDS = 60 * 60 * 24 * 30


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def get(*parts: str) -> Optional[any]:
    # Hard-disabled for now as requested
    return None


def set(data: any, *parts: str, ttl: Optional[int] = None) -> None:
    # Hard-disabled for now as requested
    return


# ── OPTIONAL: Upstash Redis (uncomment when you want shared/persistent cache) ──
# import redis
# from urllib.parse import urlparse
#
# _redis_url = os.environ.get("UPSTASH_REDIS_URL")
# _redis = redis.from_url(_redis_url) if _redis_url else None
#
# def get(*parts):
#     if not _redis: return None
#     key = _key(*parts)
#     raw = _redis.get(key)
#     return json.loads(raw) if raw else None
#
# def set(data, *parts):
#     if not _redis: return
#     key = _key(*parts)
#     _redis.setex(key, TTL_SECONDS, json.dumps(data))
