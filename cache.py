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
    if os.environ.get("DISABLE_CACHE", "true").lower() == "true":
        return None

    key = _key(*parts)

    # 1. memory
    if key in _mem_cache:
        ts, data, ttl = _mem_cache[key]
        if time.time() - ts < ttl:
            return data
        del _mem_cache[key]

    # 2. disk (survives within the same container instance / cold start)
    file = CACHE_DIR / f"{key}.json"
    if file.exists():
        try:
            payload = json.loads(file.read_text())
            data = payload["data"]
            ttl = payload.get("ttl", TTL_SECONDS)
            if time.time() - payload["ts"] < ttl:
                _mem_cache[key] = (payload["ts"], data, ttl)
                return data
        except Exception:
            pass

    return None


def set(data: any, *parts: str, ttl: Optional[int] = None) -> None:
    """ttl: override the default 30-day TTL for this entry (e.g. shorter TTL for search or 'not found' results)."""
    if os.environ.get("DISABLE_CACHE", "true").lower() == "true":
        return

    key = _key(*parts)
    ts = time.time()
    actual_ttl = ttl if ttl is not None else TTL_SECONDS
    _mem_cache[key] = (ts, data, actual_ttl)
    try:
        file = CACHE_DIR / f"{key}.json"
        file.write_text(json.dumps({"ts": ts, "data": data, "ttl": actual_ttl}))
    except Exception:
        pass  # disk cache is best-effort only


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
