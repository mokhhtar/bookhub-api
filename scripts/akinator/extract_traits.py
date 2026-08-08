"""
scripts/akinator/extract_traits.py — run the model over the descriptions.

    python scripts/akinator/extract_traits.py --calibrate   # measure first
    python scripts/akinator/extract_traits.py --limit 5000

MODEL: `gemini-3.1-flash-lite` through the project's shared client —
temperature 0.3, thinking "low", rotating across GEMINI_API_KEYS, falling
back to Groq when every key fails. Free tier throughout, nothing new added.
That tier is not a compromise: this is classification against a fixed list
from supplied text, which `gemini_client.py`'s own docstring describes as
"not a hard reasoning task… fast, cheap, instruction-following". Every call
is BUILD TIME, baked into static files, never in the play path.

CALIBRATE BEFORE TRUSTING IT. `--calibrate` runs the extractor over the
books we publish and compares its labels against the themes a human already
reviewed on those pages. That is a real accuracy number on answers we
already own, and it is the reason not to argue about model choice in the
abstract — if flash-lite disagrees badly there, that measurement is the
argument for a bigger model. Not before.

WHAT A DISAGREEMENT MEANS, and why the number is not a simple accuracy. Our
page themes are editorial and sparse — a book about a sea voyage may simply
not have "the sea" among its four listed themes. So a model label our pages
lack is not necessarily wrong. The number worth watching is the reverse:
**themes our pages DO assert that the model misses**, which is a real
failure to read.

Output: data/akinator_traits.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from features import normalize                      # noqa: E402
from site_books import load_site_books              # noqa: E402
from traits import (OUT_PATH, TRAITS, build_prompt,  # noqa: E402
                    parse_response)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
DESC_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")

# Which of our editorial theme phrases assert which trait. Used ONLY by
# --calibrate, to check the model against a human judgment — never to
# produce labels, because it would inherit the sparsity that makes our
# themes unusable as features in the first place.
CALIBRATION_HINTS = {
    "t:sea": ["sea", "ocean", "voyage", "sailing", "maritime", "shipwreck"],
    "t:magic": ["magic", "sorcery", "witch", "supernatural", "enchant"],
    "t:war": ["war", "battle", "conflict", "military"],
    "t:romance": ["love", "romance", "courtship"],
    "t:survival": ["survival", "endurance", "isolation"],
    "t:family": ["family", "parent", "sibling", "marriage"],
    "t:child": ["childhood", "coming of age", "growing up", "innocence"],
    "t:funny": ["satire", "humor", "humour", "comic", "absurd"],
    "t:detective": ["mystery", "detective", "investigation", "crime"],
    "t:realevents": ["historical", "biography", "memoir", "true story"],
}


def _generate(prompt: str) -> str:
    from gemini_client import generate
    return generate(prompt)


def extract_one(title: str, author: str, text: str) -> list[str] | None:
    """Labels for one book, or None when the call failed.

    None and [] are different and both are kept: a failed call should be
    retried on the next run, while a genuine empty answer should not.
    """
    try:
        return parse_response(_generate(build_prompt(title, author, text)))
    except Exception as exc:  # noqa: BLE001
        print(f"    ! {title[:40]}: {str(exc)[:70]}", file=sys.stderr)
        return None


def calibrate(delay: float) -> None:
    """Measure the model against themes a human already reviewed."""
    pages = [p for p in load_site_books() if p.get("themes") and p.get("prose")]
    print(f"Calibrating on {len(pages)} published pages with themes.\n")

    hit = miss = extra = 0
    for i, p in enumerate(pages, 1):
        labels = extract_one(p["title"], p["author"], p["prose"][:4000])
        if labels is None:
            continue
        got = set(labels)

        # What our human-reviewed themes assert.
        normed = [normalize(t) for t in p["themes"]]
        expected = {
            key for key, needles in CALIBRATION_HINTS.items()
            if any(any(n in t for n in needles) for t in normed)
        }

        hit += len(expected & got)
        miss += len(expected - got)
        extra += len(got - expected)

        if i % 10 == 0 or i == len(pages):
            total = hit + miss
            print(f"  {i:>4}/{len(pages)}   agreed {hit}, "
                  f"MISSED {miss} ({miss * 100 // max(1, total)}% of asserted), "
                  f"model-only {extra}")
        time.sleep(delay)

    total = hit + miss
    print()
    print("CALIBRATION")
    print(f"  themes our pages assert AND the model found : {hit}")
    print(f"  themes our pages assert and the model MISSED: {miss} "
          f"({miss * 100 // max(1, total)}%)")
    print(f"  labels only the model gave                  : {extra}")
    print()
    print("  The miss rate is the number that matters — those are human")
    print("  judgments the model failed to read. Model-only labels are")
    print("  often correct: our themes list four per book and are sparse")
    print("  by design, so 'the sea' can be true and simply unlisted.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    if args.calibrate:
        calibrate(args.delay)
        return

    if not os.path.exists(DESC_PATH):
        print("No descriptions yet — run harvest_descriptions.py first.")
        return
    with open(DESC_PATH, encoding="utf-8") as fh:
        descriptions = json.load(fh)

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

    out: dict[str, list[str]] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        out = saved.get("traits", saved)
        print(f"Resuming with {len(out)} books already labelled.")

    todo = [d for d in docs
            if d.get("key") in descriptions and d["key"] not in out]
    print(f"{len(todo)} books with a description and no labels yet.\n")

    for i, doc in enumerate(todo, 1):
        key = doc["key"]
        labels = extract_one(doc.get("title") or "",
                             (doc.get("author_name") or [""])[0],
                             descriptions[key])
        if labels is None:
            continue          # failed call: leave it for the next run
        out[key] = labels

        if i % 25 == 0 or i == len(todo):
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"traits": out}, fh, ensure_ascii=False)
            labelled = sum(1 for v in out.values() if v)
            print(f"  {i:>5}/{len(todo)}   {len(out)} done, "
                  f"{labelled} with at least one label")
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"traits": out}, fh, ensure_ascii=False)

    counts: dict[str, int] = {}
    for labels in out.values():
        for l in labels:
            counts[l] = counts.get(l, 0) + 1
    print(f"\n{len(out)} books labelled -> {args.out}")
    n = max(1, len(out))
    for key, (question, _defn) in TRAITS.items():
        c = counts.get(key, 0)
        print(f"  {c * 100 // n:>3}%  {key:<14} {question}")


if __name__ == "__main__":
    main()
