"""
scripts/promote_books_batch.py — staged indexing rollout for book pages.

    python scripts/promote_books_batch.py --min-score 9 --limit 20 --dry-run
    python scripts/promote_books_batch.py --min-score 9 --limit 20

Turning 89 unindexed pages on at once is not the risk people assume — Google
does not penalise volume; news sites publish hundreds a day. The real risk is
letting THIN pages into the index, where they drag the whole site's quality
classification. So the gate here is completeness, not count, and the batching
exists for a second reason: it produces a readable signal. If batch one
doesn't get indexed, batch two would repeat the same mistake at scale.

Scoring uses what a page actually carries, because prose length turned out to
be useless as a discriminator — across all 91 pages the median body is 713
words and the spread between the richest and thinnest page is barely 20 words.
The structured fields are what differ:

    free Gutenberg text   3   (a standalone reason to visit, and the reason
                               github_publisher's v3 rule grants indexing)
    verified quotes       2
    characters            2
    pre-generated quiz    1
    ratings               1
    chapter list          1
    body >= 1200 words    2   (>= 800 words: 1)

Writes the same two lines tools/indexing.py writes when a page earns indexing
through engagement, inserted the same way — immediately before the closing
front-matter delimiter, everything else byte-identical. Safe against a later
republish because github_publisher._carried_index_state now carries an
existing page's indexing state forward instead of recomputing it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re

SITE_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "bookhub"))
BOOKS_DIR = os.path.join(SITE_ROOT, "_books")


def _field(head: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.*)$", head, re.M)
    return m.group(1).strip() if m else ""


def score(markdown: str) -> int:
    head = markdown.split("---", 2)[1] if markdown.count("---") >= 2 else markdown
    body = markdown.split("---", 2)[2] if markdown.count("---") >= 2 else ""
    words = len(re.sub(r"<[^>]+>", " ", body).split())

    points = 0
    if "project_gutenberg" in _field(head, "free_ebook"):
        points += 3
    if _field(head, "quotes") not in ("", "{}", "null"):
        points += 2
    if _field(head, "characters") not in ("", "[]"):
        points += 2
    if _field(head, "quiz") not in ("", "[]"):
        points += 1
    if _field(head, "ratings") not in ("", "{}", "null"):
        points += 1
    if _field(head, "chapters") not in ("", "[]"):
        points += 1
    points += 2 if words >= 1200 else (1 if words >= 800 else 0)
    return points


def already_indexed(markdown: str) -> bool:
    return bool(re.search(r"^noindex:\s*false\s*$", markdown, re.M))


def promote(path: str, markdown: str, batch: str) -> str:
    """Same surgical insert as tools/indexing.py's _mark_indexable."""
    head, sep, rest = markdown.partition("\n---\n")
    if not sep:
        raise ValueError(f"no front-matter delimiter in {path}")
    return (head
            + "\nnoindex: false"
            + "\nsitemap: true"
            + f"\nindex_promoted: batch {batch} {dt.date.today().isoformat()}"
            + sep + rest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=int, default=9)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--batch", default="1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    candidates, skipped = [], 0
    for name in sorted(os.listdir(BOOKS_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(BOOKS_DIR, name)
        markdown = open(path, encoding="utf-8").read()
        if already_indexed(markdown):
            skipped += 1
            continue
        s = score(markdown)
        if s >= args.min_score:
            candidates.append((s, name, path, markdown))

    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen = candidates[:args.limit]

    print(f"{skipped} page(s) already indexed · {len(candidates)} qualify at "
          f"score >= {args.min_score} · promoting {len(chosen)}"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")
    for s, name, path, markdown in chosen:
        print(f"  [{s:>2}] {name[:-3]}")
        if not args.dry_run:
            open(path, "w", encoding="utf-8").write(promote(path, markdown, args.batch))

    left = len(candidates) - len(chosen)
    if left:
        print(f"\n{left} more qualify but were held back for a later batch.")
    print("\nNext: rebuild, confirm sitemap-books.xml lists exactly these plus the "
          "already-indexed ones, and do NOT run the next batch until Search Console "
          "shows most of this one indexed. A batch that isn't getting indexed is a "
          "quality signal, and running the next one would repeat the mistake.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
