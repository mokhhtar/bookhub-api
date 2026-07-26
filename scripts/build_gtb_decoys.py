"""
scripts/build_gtb_decoys.py — builds the decoy half of the game's answer picker.

    python scripts/build_gtb_decoys.py --target 300

WHY DECOYS EXIST. "Guess the Book" answers come from tools/gtb_pool.py, which
is small by necessity (every entry is hand-verified). If the picker listed only
those books, the player would be choosing one of ~48 in six guesses — a much
easier game than intended, and one that leaks the answer set. The picker
therefore also carries a few hundred real classics that are never answers.

WHERE THEY COME FROM. Project Gutenberg's own catalog, via Gutendex, sorted by
download count. Title and author are copied from the catalog record — nothing
here is written from memory, so a decoy cannot be a book that does not exist.
(Gutendex 403s Render's datacenter IP but answers fine from a laptop, which is
all this needs: it runs locally and its output is committed.)

THE ONE REAL HAZARD is a decoy that is the SAME WORK as a pool book under a
different edition title — "Moby Dick; Or, The Whale" beside our "Moby-Dick".
A player picking that entry would be naming the right book and be told they
are wrong. _same_work() below is what prevents it, and it is the reason this
is a curation script with a committed output rather than a fetch at play time.

Output: data/gtb_decoys.json, merged with the pool by make_gtb_puzzles.py when
it writes the game's titles.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slug import author_slug, book_slug  # noqa: E402
from tools.gtb_pool import GTB_POOL  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "data", "gtb_decoys.json")
UA = {"User-Agent": "Litheca/1.0 (mokhhtar@github.com)"}

# Reference works, compilations and anything that isn't a single narrative a
# player could plausibly name as "the book".
SKIP_TITLE_PATTERNS = re.compile(
    r"complete works|collected works|\bworks of\b|\bvol\.?\s*\d|volume\s+[ivx\d]|"
    r"\bindex\b|dictionary|encyclopedia|encyclopaedia|bible|testament|"
    r"anthology|selected (stories|poems|works)|\bpapers\b|catalog",
    re.I,
)
SKIP_AUTHORS = {"various", "anonymous", "unknown"}
TITLE_STOPWORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "to"}


def tidy_title(raw: str) -> str:
    """Catalog titles carry edition subtitles and hard line breaks."""
    title = raw.replace("\r\n", " ").replace("\n", " ")
    title = re.split(r"[;:]", title)[0]
    return re.sub(r"\s+", " ", title).strip()


def tidy_author(raw: str) -> str:
    """'Melville, Herman' → 'Herman Melville'; 'Forster, E. M. (Edward Morgan)'
    → 'E. M. Forster'."""
    name = re.sub(r"\([^)]*\)", "", raw).strip().rstrip(",")
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    return re.sub(r"\s+", " ", name).strip()


def title_words(title: str) -> set[str]:
    """Significant words, crudely singularised. Without the plural strip,
    "Twenty Thousand Leagues Under the Sea" and "…Under the Seas" read as two
    different books and both reached the picker — the same trap as the
    Dostoyevsky/Dostoevsky spelling, one letter further along."""
    words = set()
    for w in re.split(r"[^a-z0-9]+", title.lower()):
        if not w or w in TITLE_STOPWORDS:
            continue
        words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return words


def surname(author: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z]+", author) if p]
    return parts[-1].lower() if parts else ""


def _same_surname(a: str, b: str) -> bool:
    """Transliterations differ between catalogs: Gutendex says "Dostoyevsky"
    where the pool says "Dostoevsky", and an exact comparison let Crime and
    Punishment into the picker twice — one of which would have been marked
    wrong on its own puzzle day. Same first five letters is enough here,
    because this only decides ties that already share a title."""
    x, y = surname(a), surname(b)
    if not x or not y:
        return False
    return x == y or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5])


def _same_work(title_a: str, author_a: str, title_b: str, author_b: str) -> bool:
    """Same author, and one title's significant words contain the other's —
    catches "Moby Dick" vs "Moby-Dick; Or, The Whale" and "Adventures of
    Huckleberry Finn" vs "The Adventures of Huckleberry Finn"."""
    if not _same_surname(author_a, author_b):
        return False
    wa, wb = title_words(title_a), title_words(title_b)
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa


def fetch_popular(target: int) -> list[dict]:
    """Walk Gutendex's popularity ranking until enough usable rows are found."""
    url = "https://gutendex.com/books/"
    params = {"languages": "en", "sort": "popular"}
    rows: list[dict] = []
    seen_ids: set[str] = set()
    pool_pairs = [(e.title, e.author) for e in GTB_POOL]
    pool_ids = {e.canonical_id for e in GTB_POOL}

    while url and len(rows) < target:
        # Gutendex times out often enough that an unguarded loop crashed
        # mid-pagination and left the PREVIOUS run's file in place — stale
        # decoys that still contained a duplicate of a pool book.
        payload = None
        for attempt in (1, 2, 3):
            try:
                r = httpx.get(url, params=params if "?" not in url else None,
                              headers=UA, timeout=45.0, follow_redirects=True)
                if r.status_code != 200:
                    print(f"  Gutendex returned {r.status_code} — stopping early")
                    break
                payload = r.json()
                break
            except Exception as e:
                print(f"  page fetch failed (attempt {attempt}): {type(e).__name__}")
        if payload is None:
            print("  giving up on further pages")
            break

        for book in payload.get("results", []):
            authors = book.get("authors") or []
            if not authors:
                continue
            author = tidy_author(authors[0].get("name", ""))
            title = tidy_title(book.get("title", ""))
            if not author or not title or len(title) < 3:
                continue
            if author.lower() in SKIP_AUTHORS or SKIP_TITLE_PATTERNS.search(title):
                continue

            canonical = f"{book_slug(title)}-{author_slug(author)}"
            if not canonical.strip("-") or canonical in seen_ids or canonical in pool_ids:
                continue
            # The hazard this whole script exists to avoid.
            if any(_same_work(title, author, pt, pa) for pt, pa in pool_pairs):
                print(f"  skipped (same work as a pool book): {title} — {author}")
                continue

            seen_ids.add(canonical)
            rows.append({"id": canonical, "title": title, "author": author})
            if len(rows) >= target:
                break

        url = payload.get("next")
        params = None
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=300, help="how many decoys to collect")
    args = ap.parse_args()

    print(f"Fetching {args.target} decoy titles from Gutendex (popularity order)…")
    rows = fetch_popular(args.target)
    if len(rows) < args.target * 0.6:
        print(f"Only {len(rows)} usable rows — refusing to write a thin picker.")
        return 1

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    # Belt and braces: anything sharing a pool book's exact title words, even
    # under a different author name, is worth a human glance before it ships.
    pool_titles = {frozenset(title_words(e.title)): e for e in GTB_POOL}
    for row in rows:
        match = pool_titles.get(frozenset(title_words(row["title"])))
        if match:
            print(f"  CHECK BY EYE — '{row['title']} — {row['author']}' has the same "
                  f"title as the pool's '{match.title} — {match.author}'")

    print(f"\nWrote {len(rows)} decoys to {OUT_PATH}")
    print("Sample:")
    for row in rows[:8]:
        print(f"  {row['title']} — {row['author']}")
    print("\nThese are picker entries only; they are never answers. Skim the list "
          "for anything that isn't a book a reader would name.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
