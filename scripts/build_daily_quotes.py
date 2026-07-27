"""
scripts/build_daily_quotes.py — builds the daily quote well from our own pages.

    python scripts/build_daily_quotes.py

WHY. tools/daily.py ships 10 hand-written fallback quotes, and its own comment
admits the upgrade path: "grow this to 366". Ten is not a well, it is a
puddle — the history tracker skips anything served in the last 30 days, so a
pool of 10 exhausts itself in under two weeks and then has no choice but to
repeat.

WHERE THE QUOTES COME FROM. The `quotes` block already committed in the site's
_books/*.md pages: real, attributed Wikiquote text that resolve_wikiquote_quotes
fetched and the book pages already display. Nothing is written from memory and
nothing is generated, so this well is grounded by construction and every entry
points at a book page we actually publish.

LICENCE. Wikiquote is CC BY-SA. Each row carries the `source_url` of the page
it came from so the card can attribute it exactly the way _layouts/book.html
already does ("Sourced verbatim from Wikiquote (CC BY-SA)"). Don't drop that
field — it is the attribution.

Output: data/daily_quotes.json, loaded by tools/daily.py at import.
"""
from __future__ import annotations

import argparse
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_ROOT = os.path.abspath(os.path.join(REPO_ROOT, "..", "bookhub"))
OUT_PATH = os.path.join(REPO_ROOT, "data", "daily_quotes.json")

# A quote has to work as a standalone pull-quote on the homepage.
MIN_CHARS = 45
MAX_CHARS = 230


def _front(head: str, key: str) -> str:
    m = re.search(rf'^{key}:\s*"?([^"\n]+)"?\s*$', head, re.M)
    return m.group(1).strip() if m else ""


def _usable(text: str) -> bool:
    """Reject anything that reads as a fragment rather than a line.

    Wikiquote entries are often elided ("He desires to paint you the
    dreamiest… What is the chief element he employs?") or start mid-sentence.
    Both look broken set as a pull-quote, and no amount of styling fixes a
    quote with a hole in the middle.
    """
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if "..." in text or "…" in text:
        return False
    if not text[0].isupper() and text[0] not in "\"'“‘":
        return False
    return text.rstrip()[-1] in ".!?\"'”’"


def collect() -> list[dict]:
    books_dir = os.path.join(SITE_ROOT, "_books")
    rows: list[dict] = []
    seen: set[str] = set()
    for name in sorted(os.listdir(books_dir)):
        if not name.endswith(".md"):
            continue
        head = open(os.path.join(books_dir, name), encoding="utf-8").read()
        m = re.search(r"^quotes:\s*(\{.*\})\s*$", head, re.M)
        if not m:
            continue
        try:
            block = json.loads(m.group(1))
        except Exception:
            continue
        title, author = _front(head, "title"), _front(head, "author")
        slug = _front(head, "slug")
        if not (title and author and slug):
            continue
        for text in (block.get("texts") or []):
            text = (text or "").strip()
            key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
            if not _usable(text) or key in seen:
                continue
            seen.add(key)
            rows.append({
                "text": text,
                "book_title": title,
                "author": author,
                "book_url": f"/summary/{slug}/",
                # Carried so the same rows can back the book card's fallback
                # (see _book_from_our_shelf) without a catalog lookup.
                "cover_url": _front(head, "cover_url"),
                "source_url": block.get("source_url") or "",
                "license": block.get("license") or "CC BY-SA",
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=40,
                    help="refuse to write a well smaller than this")
    args = ap.parse_args()

    rows = collect()
    if len(rows) < args.min:
        print(f"Only {len(rows)} usable quotes — refusing to write a well that "
              f"would start repeating within weeks.")
        return 1

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    books = len({r["book_title"] for r in rows})
    print(f"Wrote {len(rows)} quotes from {books} books to {OUT_PATH}")
    print(f"At one a day with a 30-day no-repeat window, that is "
          f"{len(rows) // 30} full cycles before any repeat is even possible.\n")
    for row in rows[:6]:
        print(f"  “{row['text'][:64]}…”  — {row['book_title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
