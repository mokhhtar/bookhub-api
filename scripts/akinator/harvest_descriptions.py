"""
scripts/akinator/harvest_descriptions.py — the text an extractor can read.

WHY. The game already has a question "does it take place at sea?" and it is
NOT ASKED, because Open Library's subject strings flag it on **1.3%** of the
corpus — below the 5% floor, so feature selection drops it. The signal is
real (Moby Dick, Treasure Island, Robinson Crusoe, The Sea-Wolf all carry
it) and the coverage is not: Twenty Thousand Leagues Under the Sea is
missed, Little Women is flagged.

That is the shape of every evocative question the owner asked for — sea,
secret organisations, organised magic. The catalogue knows a little and
records it inconsistently. **Prose is where those facts actually live**, so
this fetches the prose.

WHAT WAS MEASURED BEFORE WRITING THIS:

    first_sentence (already fetched)   41%, median 114 chars  — too thin
    OL works-endpoint description      80%, median 762 chars (~127 words)
    our own published pages           100% of 129, median 713 words

Four books in five have real description text. It is not in the search
results — only the per-work endpoint carries it — so this is one request per
book, about 85 minutes for 5,000 at a polite rate. Monthly, alongside the
other harvests, and resumable like all of them.

Output: data/akinator_descriptions.json — `{work_key: text}`. Its own file,
so a partial run can never damage the corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

# Below this a "description" is a fragment or a publisher's one-liner, and
# an extractor reading it would be guessing from almost nothing.
MIN_CHARS = 120
# Above this we are paying for tokens on a full plot summary. The opening is
# where a description says what kind of story this is.
MAX_CHARS = 4000


def _text_of(value) -> str:
    """OL descriptions arrive as a bare string or as {type, value}."""
    if isinstance(value, dict):
        value = value.get("value")
    return (value or "").strip() if isinstance(value, str) else ""


def fetch_one(work_key: str, timeout: int = 25) -> str:
    url = f"https://openlibrary.org{work_key}.json"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    text = _text_of(data.get("description"))
    if len(text) < MIN_CHARS:
        # Some works carry the blurb under `first_sentence` instead.
        alt = _text_of(data.get("first_sentence"))
        if len(alt) > len(text):
            text = alt
    return text[:MAX_CHARS] if len(text) >= MIN_CHARS else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    docs = []
    with open(CORPUS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    docs = docs[:args.limit]

    out: dict[str, str] = {}
    asked: set[str] = set()
    asked_path = args.out.replace(".json", "_asked.json")
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            out = json.load(fh)
    if os.path.exists(asked_path):
        with open(asked_path, encoding="utf-8") as fh:
            asked = set(json.load(fh))
    # Asked is tracked apart from found: a book with no description would
    # otherwise be re-requested on every run, forever. Same drift that the
    # cover fetcher's resume had to be fixed for.
    if asked:
        print(f"Resuming: {len(asked)} asked, {len(out)} with text.\n")

    todo = [d for d in docs if d.get("key") and d["key"] not in asked]
    print(f"{len(docs)} books, {len(todo)} still to ask about.\n")

    failures = 0
    for i, doc in enumerate(todo, 1):
        key = doc["key"]
        try:
            text = fetch_one(key)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures >= 6:
                print("\n! six failures in a row; stopping. Re-run to resume.",
                      file=sys.stderr)
                break
            time.sleep(3 * failures)
            continue

        asked.add(key)
        if text:
            out[key] = text

        # Checkpoint often. At 200 the first smoke test was killed at ~175
        # items and saved nothing at all — a resumable job that only becomes
        # resumable after three minutes is not resumable.
        if i % 50 == 0 or i == len(todo):
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False)
            with open(asked_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(asked), fh)
            print(f"  {i:>5}/{len(todo)} asked   {len(out)} with text "
                  f"({len(out) * 100 // max(1, len(asked))}%)")
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    with open(asked_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(asked), fh)
    print(f"\n{len(out)} descriptions -> {args.out}")


if __name__ == "__main__":
    main()
