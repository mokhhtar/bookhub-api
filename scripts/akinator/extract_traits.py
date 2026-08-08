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

from features import _compile, normalize            # noqa: E402
from site_books import load_site_books              # noqa: E402
from traits import (BATCH_SIZE, OUT_PATH, TRAITS,      # noqa: E402
                    build_batch_prompt, build_prompt,
                    parse_batch_response, parse_response)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
DESC_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")
CALIBRATION_PATH = os.path.join(REPO_ROOT, "data", "akinator_calibration.json")

# Which of our editorial theme phrases assert which trait. Used ONLY by
# --calibrate, to check the model against a human judgment — never to
# produce labels, because it would inherit the sparsity that makes our
# themes unusable as features in the first place.
#
# MATCHED WHOLE-WORD, via features._compile — the same matcher, and the same
# reason, as the subject rules. The first version of this table substring-
# matched and the resulting "40% missed" was not a measurement of the model:
# "sea" matched *Search for meaning* and *Nature and the seasons*, "war"
# matched *Cowardice* (co-war-dice). The model declining those is correct,
# and it was scored as a failure to read. A trailing '*' means stem.
#
# NEEDLES REMOVED, each because the theme does not assert the trait AS
# DEFINED in traits.py — not because the model disagreed with them:
#   t:war       "conflict"  — *Father-son conflict*, *Moral conflict* are
#                             not wars; the definition says armed conflict.
#   t:child     "innocence" — *American innocence*, *Corruption of
#                             innocence* say nothing about the protagonist's
#                             age, which is what the trait claims.
#   t:survival  "isolation" — *Social isolation*, *Isolation and madness*
#                             are not a hostile environment to survive.
#   t:detective "crime"     — *Crime and punishment*, *Poverty and crime*
#                             assert no investigation.
#   t:funny     "absurd"    — *The absurdity of existence* is Camus, not a
#                             book written to be humorous.
# "warfare" is now listed explicitly: whole-word "war" no longer reaches it.
CALIBRATION_HINTS = {
    "t:sea": ["sea", "ocean", "voyage", "sail*", "maritime", "shipwreck"],
    "t:magic": ["magic*", "sorcer*", "witch*", "supernatural", "enchant*"],
    "t:war": ["war", "warfare", "battle", "military"],
    "t:romance": ["love", "romance", "romantic", "courtship"],
    "t:survival": ["survival", "surviv*", "endurance"],
    "t:family": ["family", "parent*", "sibling*", "marriage"],
    "t:child": ["childhood", "coming of age", "growing up"],
    "t:funny": ["satire", "satirical", "humor", "humour", "comic", "comedy"],
    "t:detective": ["mystery", "detective", "investigation"],
    "t:realevents": ["historical", "biography", "memoir", "true story"],
}

# Compiled once, same idiom as features._RULE_INDEX.
_HINT_INDEX = {key: [_compile(n) for n in needles]
               for key, needles in CALIBRATION_HINTS.items()}


def expected_traits(themes: list[str]) -> set[str]:
    """Which traits our human-reviewed themes assert."""
    normed = [normalize(t) for t in themes]
    return {key for key, pats in _HINT_INDEX.items()
            if any(p.search(t) for p in pats for t in normed)}


def _generate(prompt: str, provider: str = "auto") -> str:
    """One completion. `provider` picks who answers.

    "auto" is the shared client's normal chain: every Gemini key, then Groq.
    Correct in production, wasteful here — once Gemini's daily quota is
    spent, every single call burns a doomed Gemini attempt (and its
    backoff) before reaching the provider that can actually answer. Over
    5,000 books that is hours of failing on purpose.

    "groq" goes straight there. Gemini's free tier allows 15 requests a
    minute per key, which is 5.6 hours for the corpus on one key and is
    what pushed this to a switch rather than a preference.
    """
    if provider == "groq":
        from gemini_client import DEFAULT_CONFIG, _groq_generate
        text = _groq_generate(prompt, DEFAULT_CONFIG)
        if not text:
            raise RuntimeError("Groq returned nothing (key set? quota left?)")
        return text
    from gemini_client import generate
    return generate(prompt)


def extract_one(title: str, author: str, text: str,
                provider: str = "auto") -> list[str] | None:
    """Labels for one book, or None when the call failed.

    None and [] are different and both are kept: a failed call should be
    retried on the next run, while a genuine empty answer should not.
    """
    try:
        return parse_response(_generate(build_prompt(title, author, text), provider))
    except Exception as exc:  # noqa: BLE001
        print(f"    ! {title[:40]}: {str(exc)[:70]}", file=sys.stderr)
        return None


def extract_batch(rows: list[tuple[str, str, str]],
                  provider: str) -> list[list[str] | None]:
    """Label several books in one call, falling back to one at a time.

    A batch reply that does not align exactly — wrong count, duplicate or
    out-of-range index, malformed labels — is discarded WHOLE rather than
    salvaged. Partially-aligned labels would be attached to the wrong
    books, silently, which is worse than any number of failed calls.
    """
    try:
        raw = _generate(build_batch_prompt(rows), provider)
        parsed = parse_batch_response(raw, len(rows))
        if parsed is not None:
            return parsed
        print(f"    . batch of {len(rows)} did not align; retrying singly",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! batch failed ({str(exc)[:60]}); retrying singly",
              file=sys.stderr)
    return [extract_one(t, a, x, provider) for t, a, x in rows]


def _score(rows: list[dict]) -> None:
    """Print the comparison. `rows` is [{title, themes, labels}, ...]."""
    hit = miss = extra = 0
    misses: list[tuple[str, str]] = []
    for r in rows:
        got = set(r["labels"])
        expected = expected_traits(r["themes"])
        hit += len(expected & got)
        miss += len(expected - got)
        extra += len(got - expected)
        for key in sorted(expected - got):
            misses.append((key, r["title"]))

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
    if misses:
        by_trait: dict[str, list[str]] = {}
        for key, title in misses:
            by_trait.setdefault(key, []).append(title)
        print("\n  What was missed, so the number can be argued with:")
        for key, titles in sorted(by_trait.items(), key=lambda x: -len(x[1])):
            shown = ", ".join(t[:34] for t in titles[:4])
            more = f" +{len(titles) - 4}" if len(titles) > 4 else ""
            print(f"    {key:<14} {len(titles):>3}  {shown}{more}")


def calibrate(delay: float, provider: str = "auto",
              save_path: str = CALIBRATION_PATH) -> None:
    """Measure the model against themes a human already reviewed.

    The model's answers are SAVED. Scoring them is pure judgment — which
    theme phrase asserts which trait — and that judgment was wrong the first
    time round (see CALIBRATION_HINTS). Re-scoring a saved run costs
    nothing; re-running the model to change one's mind about a keyword costs
    129 calls of a 15-a-minute quota, which is how a measurement ends up
    unchallenged.
    """
    pages = [p for p in load_site_books() if p.get("themes") and p.get("prose")]
    print(f"Calibrating on {len(pages)} published pages with themes.\n")

    rows: list[dict] = []
    failed = 0
    for i, p in enumerate(pages, 1):
        labels = extract_one(p["title"], p["author"], p["prose"][:4000], provider)
        if labels is None:
            failed += 1          # a failed call is not an empty answer
            continue
        rows.append({"title": p["title"], "themes": p["themes"],
                     "labels": labels})
        if i % 10 == 0 or i == len(pages):
            print(f"  {i:>4}/{len(pages)}   {len(rows)} scored, {failed} failed")
        time.sleep(delay)

    with open(save_path, "w", encoding="utf-8") as fh:
        json.dump({"pages": rows}, fh, ensure_ascii=False, indent=1)
    print(f"\nsaved {len(rows)} model answers -> {save_path}")
    if failed:
        print(f"  {failed} call(s) failed and are excluded, not counted as "
              f"empty answers")
    _score(rows)


def rescore(path: str = CALIBRATION_PATH) -> None:
    """Re-score a saved calibration run against the current hints. No calls."""
    if not os.path.exists(path):
        print(f"No saved calibration at {path} — run --calibrate first.")
        return
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)["pages"]
    print(f"Re-scoring {len(rows)} saved model answers. No API calls.")
    _score(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score the last --calibrate run against the "
                         "current CALIBRATION_HINTS, without any API calls.")
    ap.add_argument("--limit", type=int, default=5000)
    # 4.2s, not 0.5. The free tier allows FIFTEEN requests per minute per
    # key for gemini-3.1-flash-lite, and the first calibration run at 0.25s
    # (240/min) had most of its calls rejected with 429 — it produced a
    # number that looked like a measurement and was mostly failures. A
    # default that silently exceeds the quota is worse than a slow one.
    #
    # Cost at this rate, single key: 9 minutes for the 129-page calibration,
    # about 5.6 hours for the full 5,000. Resumable, so it can be run in
    # pieces, and it is popularity-ordered so a partial run covers the books
    # players actually think of.
    ap.add_argument("--delay", type=float, default=4.2,
                    help="seconds between calls; 4.2 keeps one key inside "
                         "the 15/min free-tier quota. Lower it only if "
                         "GEMINI_API_KEYS holds several keys.")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE,
                    help="books per call. The trait vocabulary is 575 tokens "
                         "against 201 for a book, so sending one at a time "
                         "pays the vocabulary 3,590 times over — 2.79M "
                         "tokens for the corpus against 1.13M at five. Set 1 "
                         "to disable.")
    ap.add_argument("--provider", choices=["auto", "groq"], default="auto",
                    help="'groq' skips the Gemini attempts entirely. Use it "
                         "once Gemini's daily quota is spent — otherwise "
                         "every call pays for a doomed Gemini try first.")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    if args.rescore:
        rescore()
        return

    if args.calibrate:
        calibrate(args.delay, args.provider)
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

    for start in range(0, len(todo), args.batch):
        chunk = todo[start:start + args.batch]
        rows = [(d.get("title") or "", (d.get("author_name") or [""])[0],
                 descriptions[d["key"]]) for d in chunk]
        results = extract_batch(rows, args.provider) if args.batch > 1 else             [extract_one(*r, args.provider) for r in rows]

        for doc, labels in zip(chunk, results):
            if labels is None:
                continue      # failed: leave it for the next run
            out[doc["key"]] = labels

        i = start + len(chunk)
        if True:
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
