"""
scripts/akinator/shipped_docs.py — the book list, from a URL instead of the
local corpus.

    from shipped_docs import load_shipped_docs
    docs = load_shipped_docs("https://litheca.com/games/data/akinator/books.json")

WHY THIS EXISTS. `harvest_descriptions.py` and `extract_traits.py` both read
their book list from `data/akinator_corpus.jsonl` — gitignored, local-only,
never on Render, never in any repo (`akinator_sync.py`'s own docstring:
"Render has no corpus either"). A GitHub Actions runner checking out this
repo has none of it, so a scheduled catch-up job cannot run either script at
all today, incrementally or otherwise.

The shipped `books.json` (≈590 KB, already public, already committed to the
bookhub repo) carries everything either script actually needs: key, title,
author, and popularity — enough to reproduce the same "most popular first"
ordering both scripts already sort by. Using it also means the catch-up job
targets exactly the population these harvests are meant to serve: the books
actually live, not the ~19,890-book candidate pool the local corpus holds.

This is the smallest possible translation — `books.json`'s packed row shape
(`{"k","t","a","y","p","r","w","c"}`, see `build_matrix.py`'s own writer) to
the `{"key","title","author_name","readinglog_count"}` shape both scripts
already read off a corpus line — so a URL can stand in for the local file
everywhere it is read, without either script needing to know which source it
got. Neither script's downstream logic (resume, batching, the asked-set)
changes at all: they only ever look at those four fields.
"""
from __future__ import annotations

import json


def load_shipped_docs(source: str) -> list[dict]:
    """`source` is a URL (http/https) or a local path to a books.json file.

    Sorted by popularity, descending — the same ordering
    `harvest_descriptions.py`/`extract_traits.py` already impose on the
    local corpus, so a partial or interrupted run still covers the books a
    player is most likely thinking of first.
    """
    if source.startswith("http://") or source.startswith("https://"):
        import httpx
        resp = httpx.get(source, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        rows = resp.json()
    else:
        with open(source, encoding="utf-8") as fh:
            rows = json.load(fh)

    if not isinstance(rows, list):
        raise ValueError(f"{source} did not contain a JSON list of books")

    docs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("k")
        if not key:
            continue
        author = row.get("a")
        docs.append({
            "key": key,
            "title": row.get("t") or "",
            "author_name": [author] if author else [],
            "readinglog_count": row.get("p") or 0,
        })
    docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    return docs
