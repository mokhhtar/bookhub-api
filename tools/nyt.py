"""
tools/nyt.py — NYT Books API integration (needs the free NYT_API_KEY).

The per-title best-sellers/history endpoint is broken on NYT's side as of
mid-2026 (returns "invalid date" for every request, even bare ones), so
everything here is driven by lists/overview.json: the CURRENT week's
snapshot of every NYT bestseller list. ONE request covers every book on
the site — exactly what NYT's tight 500/day budget wants — cached 24h
under a single raw key and shared by:

  - GET /nyt/weekly            — homepage "NYT Bestsellers this week" rail
  - tools/summary.py           — the per-book bestseller badge
"""
import logging
import os
import re

import httpx
from fastapi import APIRouter, Response

import cache

log = logging.getLogger("bookhub-api.tools.nyt")

router = APIRouter()

NYT_OVERVIEW_API = "https://api.nytimes.com/svc/books/v3/lists/overview.json"
_UA_HEADERS = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

# Flagship lists shown on the homepage rail, in display order.
WEEKLY_LISTS = [
    "hardcover-fiction",
    "hardcover-nonfiction",
    "trade-fiction-paperback",
    "advice-how-to-and-miscellaneous",
    "young-adult-hardcover",
]


def norm_match(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def overview() -> list[dict]:
    """Flattened current-week snapshot of all NYT bestseller lists."""
    cached = cache.get_key("nyt:overview:v2")
    if cached is not None:
        return cached
    api_key = os.environ.get("NYT_API_KEY")
    if not api_key:
        return []  # not configured — never cache this as an empty snapshot
    flat = []
    try:
        r = httpx.get(NYT_OVERVIEW_API, params={"api-key": api_key},
                      headers=_UA_HEADERS, timeout=10.0)
        if r.status_code != 200:
            log.warning(f"NYT overview returned {r.status_code}")
            cache.set_key("nyt:overview:v2", [], ttl=3600)  # brief backoff (incl. 429)
            return []
        for lst in (r.json().get("results") or {}).get("lists") or []:
            for b in lst.get("books") or []:
                flat.append({
                    "title": b.get("title") or "",
                    "author": b.get("author") or "",
                    "rank": b.get("rank"),
                    "weeks_on_list": b.get("weeks_on_list"),
                    "list_name": lst.get("display_name") or lst.get("list_name") or "Best Sellers",
                    "list_name_encoded": lst.get("list_name_encoded") or "",
                    "book_image": b.get("book_image") or "",
                    "isbn_13": b.get("primary_isbn13") or "",
                    "review_url": b.get("book_review_link") or None,
                })
        cache.set_key("nyt:overview:v2", flat, ttl=86400)
    except Exception as e:
        log.warning(f"NYT overview fetch failed: {e}")
    return flat


@router.options("/nyt/weekly")
def weekly_options():
    # Uptime monitors probe with OPTIONS/HEAD — answer cheaply (same lesson
    # as /daily: never run the data pipeline for a liveness ping).
    return Response(status_code=204)


@router.head("/nyt/weekly")
def weekly_head():
    return Response(status_code=200)


@router.get("/nyt/weekly")
def weekly():
    """
    Homepage rail payload: top books of the current week's flagship lists
    (top 3 of each, capped at 12 total). Costs zero extra NYT requests —
    it reads the same cached snapshot the summary badge uses.
    """
    flat = overview()
    if not flat:
        return {"books": []}
    picks = []
    for slug in WEEKLY_LISTS:
        in_list = sorted(
            (b for b in flat if b["list_name_encoded"] == slug and b.get("rank")),
            key=lambda b: b["rank"],
        )
        picks.extend(in_list[:3])
    seen: set[str] = set()
    books = []
    for b in picks:
        key = norm_match(b["title"]) + "|" + norm_match(b["author"])
        if key in seen:
            continue  # same book charting on two lists
        seen.add(key)
        books.append(b)
        if len(books) >= 12:
            break
    return {"books": books}
