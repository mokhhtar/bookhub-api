"""
cache.py — two-layer cache: in-memory L1 + Upstash Redis (REST) L2.

L1 (_mem_cache) is fastest and free but per-process and cleared on restart —
on Render's free tier the process sleeps/restarts often, which is exactly why
L2 exists. L2 talks to Upstash over its REST API with plain httpx (no redis
client dependency, no TCP connection pools to go stale across sleep/wake).

Fail-open by design: if UPSTASH_REDIS_REST_URL/TOKEN are unset or any call
errors, the cache silently behaves as a miss/no-op — callers never break.

Two key families:
  - get()/set()          — hashed keys built from arbitrary string parts
                           (summaries, search results, awards).
  - get_key()/set_key()  — raw deterministic keys the caller controls
                           (e.g. "daily:2026-07-05", "published:{slug}").
"""
import json
import time
import hashlib
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("bookhub-api.cache")

# In-memory layer (fastest, cleared on restart)
_mem_cache: dict[str, tuple[float, any]] = {}  # key -> (expires_at_epoch, data)
_MEM_MAX_ENTRIES = 512  # bound memory on the small Render instance

# TTL: 30 days — book summaries don't change
TTL_SECONDS = 60 * 60 * 24 * 30

UPSTASH_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
_HEADERS = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

# Dev switch: when true, the HASHED-key API (get/set) becomes a no-op so every
# summary / search / ratings / categories / awards request recomputes fresh —
# handy while iterating so stale cached responses don't hide changes. It does
# NOT touch the RAW-key API (get_key/set_key/incr_key/acquire_lock/delete_key),
# so PDF-chat storage, daily picks, rate limits and publish flags keep working.
RESPONSE_CACHE_DISABLED = os.environ.get("DISABLE_RESPONSE_CACHE", "").lower() in ("1", "true", "yes")

if not (UPSTASH_URL and UPSTASH_TOKEN):
    log.warning("Upstash env vars not set — cache runs in-memory only (misses after restarts).")
if RESPONSE_CACHE_DISABLED:
    log.warning("DISABLE_RESPONSE_CACHE is on — summary/search/ratings responses are NOT cached.")


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ── L1: in-memory ────────────────────────────────────────────

def _mem_get(key: str) -> Optional[any]:
    hit = _mem_cache.get(key)
    if not hit:
        return None
    expires_at, data = hit
    if time.time() > expires_at:
        _mem_cache.pop(key, None)
        return None
    return data


def _mem_set(key: str, data: any, ttl: int) -> None:
    if len(_mem_cache) >= _MEM_MAX_ENTRIES:
        # Drop the soonest-to-expire entry — cheap, good-enough eviction.
        oldest = min(_mem_cache, key=lambda k: _mem_cache[k][0])
        _mem_cache.pop(oldest, None)
    _mem_cache[key] = (time.time() + ttl, data)


# ── L2: Upstash Redis over REST ──────────────────────────────

def _redis_get(key: str) -> Optional[any]:
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    try:
        r = httpx.get(f"{UPSTASH_URL}/get/{key}", headers=_HEADERS, timeout=4.0)
        if r.status_code != 200:
            return None
        raw = r.json().get("result")
        return json.loads(raw) if raw else None
    except Exception as e:
        log.warning(f"Redis GET failed for '{key}': {e}")
        return None


def _redis_set(key: str, data: any, ttl: Optional[int]) -> None:
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return
    try:
        value = json.dumps(data, ensure_ascii=False)
        url = f"{UPSTASH_URL}/set/{key}"
        if ttl:
            url += f"?EX={int(ttl)}"
        # POST body = value (Upstash REST accepts the value as the request body)
        r = httpx.post(url, headers=_HEADERS, content=value.encode("utf-8"), timeout=4.0)
        if r.status_code != 200:
            log.warning(f"Redis SET returned {r.status_code} for '{key}'")
    except Exception as e:
        log.warning(f"Redis SET failed for '{key}': {e}")


def _redis_setnx(key: str, data: any, ttl: int) -> bool:
    """
    SET key value NX EX ttl — returns True if the key was newly set (lock
    acquired). Uses Upstash's JSON-command-array endpoint (POST to the base
    URL with ["SET", key, value, "EX", ttl, "NX"]) rather than combining NX
    and EX as query-string params on the path-based /set/{key} endpoint —
    that combination isn't reliably supported and was silently making every
    lock acquisition fail (every first-time caller got "already locked").
    """
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return True  # no shared store → behave as if lock acquired
    try:
        value = json.dumps(data, ensure_ascii=False)
        command = ["SET", key, value, "EX", str(int(ttl)), "NX"]
        r = httpx.post(UPSTASH_URL, headers={**_HEADERS, "Content-Type": "application/json"},
                       json=command, timeout=4.0)
        if r.status_code != 200:
            log.warning(f"Redis SETNX returned {r.status_code} for '{key}': {r.text[:200]}")
            return True  # fail-open — never let a broken lock endpoint block real users
        return r.json().get("result") == "OK"
    except Exception as e:
        log.warning(f"Redis SETNX failed for '{key}': {e}")
        return True


# ── Public API: hashed keys ──────────────────────────────────

def get(*parts: str) -> Optional[any]:
    if RESPONSE_CACHE_DISABLED:
        return None
    key = _key(*parts)
    data = _mem_get(key)
    if data is not None:
        return data
    data = _redis_get(key)
    if data is not None:
        _mem_set(key, data, TTL_SECONDS)
    return data


def set(data: any, *parts: str, ttl: Optional[int] = None) -> None:
    if RESPONSE_CACHE_DISABLED:
        return
    key = _key(*parts)
    effective_ttl = ttl or TTL_SECONDS
    _mem_set(key, data, effective_ttl)
    _redis_set(key, data, effective_ttl)


# ── Public API: raw deterministic keys ───────────────────────

def get_key(key: str) -> Optional[any]:
    data = _mem_get(key)
    if data is not None:
        return data
    data = _redis_get(key)
    if data is not None:
        _mem_set(key, data, 3600)  # short L1 for raw keys; L2 owns the real TTL
    return data


def set_key(key: str, data: any, ttl: Optional[int] = None) -> None:
    _mem_set(key, data, ttl or TTL_SECONDS)
    _redis_set(key, data, ttl or TTL_SECONDS)


def acquire_lock(key: str, ttl: int = 120) -> bool:
    """Best-effort distributed lock (SET NX EX). True = caller may proceed."""
    return _redis_setnx(key, {"locked": True}, ttl)


def set_key_strict(key: str, data: any, ttl: Optional[int] = None, l1: bool = False) -> bool:
    """
    Like set_key but returns whether the Redis write actually succeeded.
    For writes that must NOT be silently dropped (e.g. a PDF text block —
    a half-stored document must abort, not pretend success). l1=False by
    default so large payloads don't flood the small in-memory layer.

    Without Upstash configured (local dev), falls back to L1-only and reports
    success — data survives only until restart, which is fine for dev.
    """
    effective_ttl = ttl or TTL_SECONDS
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        _mem_set(key, data, effective_ttl)
        return True
    try:
        value = json.dumps(data, ensure_ascii=False)
        r = httpx.post(f"{UPSTASH_URL}/set/{key}?EX={int(effective_ttl)}",
                       headers=_HEADERS, content=value.encode("utf-8"), timeout=8.0)
        if r.status_code != 200:
            log.warning(f"Redis strict SET returned {r.status_code} for '{key}'")
            return False
        if l1:
            _mem_set(key, data, effective_ttl)
        return True
    except Exception as e:
        log.warning(f"Redis strict SET failed for '{key}': {e}")
        return False


def incr_key(key: str, ttl: int) -> Optional[int]:
    """
    Atomic counter (INCR + EXPIRE on first increment). Returns the new count,
    or None on any failure — callers treat None as fail-open "allow", matching
    the rest of this module's philosophy.
    """
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    try:
        r = httpx.post(f"{UPSTASH_URL}/incr/{key}", headers=_HEADERS, timeout=4.0)
        if r.status_code != 200:
            return None
        count = r.json().get("result")
        if count == 1:
            httpx.post(f"{UPSTASH_URL}/expire/{key}/{int(ttl)}", headers=_HEADERS, timeout=4.0)
        return int(count) if count is not None else None
    except Exception as e:
        log.warning(f"Redis INCR failed for '{key}': {e}")
        return None


def delete_key(key: str) -> None:
    """Best-effort DEL (also evicts L1) — e.g. releasing an ingest lock on failure."""
    _mem_cache.pop(key, None)
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return
    try:
        httpx.post(f"{UPSTASH_URL}/del/{key}", headers=_HEADERS, timeout=4.0)
    except Exception as e:
        log.warning(f"Redis DEL failed for '{key}': {e}")
