"""
scripts/akinator/simulate.py — the Phase 0 gate.

    python scripts/akinator/simulate.py
    python scripts/akinator/simulate.py --miss-rate 0.40 --no-characters

WHAT IT MEASURES. How many questions the engine needs to name the book a
simulated player is thinking of. The plan's gate: **median <= 25 questions
on famous books**, or the feature set needs rethinking before any 4 GB dump
pipeline gets built.

HOW THE SIMULATED PLAYER ANSWERS, and why it is not circular. The obvious
mistake would be to answer from the same probability matrix the engine uses
for inference; that measures nothing but arithmetic. Instead the player
answers from the book's **ground-truth feature set**, and then two kinds of
disagreement are injected on top:

  * `--noise`  — ordinary human hedging: "probably", or "don't know" about
    a book they have read but don't remember in that much detail.
  * `--miss-rate` — the one that actually matters. Open Library's metadata
    is incomplete, so a player will often answer *yes* to a feature the
    record does not carry ("does it take place at sea?" for a sea story
    nobody tagged as one). That answer pushes belief AWAY from the correct
    book. This is the realistic failure mode, and a simulation without it
    would report a number the live game could never reproduce.

    Measured effect, 500 books: 83.8% at miss_rate=0 against 64.0% at 0.25.
    **The engine is not the bottleneck; metadata coverage is.** That is the
    finding that should drive Phase 1's effort budget.

Character questions are asked only in the endgame and are answered from the
player's own knowledge of the cast, so `--miss-rate` applies to them too:
OL not listing Hermione does not stop a player saying yes to her.

Treat every number here as an **upper bound**. Real players disagree with
library metadata in ways no noise parameter models, and they think of books
outside the corpus entirely — which is what the "who was it?" flow in
Phase 5 exists to catch.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from author_traits import AUTHOR_QUESTIONS, book_traits, load_wikidata  # noqa: E402
from characters import extract_characters, usable_token          # noqa: E402
from corpus_filter import filter_corpus                          # noqa: E402
from site_books import supplement as site_supplement              # noqa: E402
from traits import TRAIT_QUESTIONS, apply_labels, load_traits     # noqa: E402
from engine import Engine, Matrix                                # noqa: E402
from work_traits import (WORK_QUESTIONS, load_protagonists,        # noqa: E402
                         load_works, merge_into)
from features import (FORCE_KEEP, QUESTION_TEXT,                    # noqa: E402
                      STRUCTURAL_QUESTIONS, extract)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_PATH = os.path.join(REPO_ROOT, "data", "akinator_sample.json")
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")

# A feature carried by 2% of books eliminates almost nothing; one carried by
# 80% tells almost nothing. The requirements note settled on a 5%-60% band.
MIN_FREQ = 0.05
MAX_FREQ = 0.60

# Character tokens are the opposite case: a name shared by many books is
# useless, and one unique to a single book is exactly what we want in the
# endgame. Only the floor matters — it must appear at all.
MIN_CHAR_BOOKS = 1
MAX_CHAR_SHARE = 0.02


def load_docs(corpus_size: int = 0) -> list[dict]:
    """Prefer the real corpus; fall back to the Phase 0 sample.

    `corpus_size` truncates by popularity, which is what makes the
    corpus-size sweep meaningful: the top N of a popularity-sorted list is
    the same set the shipped game would carry at that size.
    """
    if os.path.exists(CORPUS_PATH):
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
    else:
        with open(SAMPLE_PATH, encoding="utf-8") as fh:
            docs = json.load(fh)
    # Filter BEFORE truncating, so removing a workbook promotes a real book
    # into the corpus rather than leaving a hole.
    docs = filter_corpus(docs, verbose=False)
    # Books we publish are pinned in regardless of Open Library rank.
    docs = site_supplement(docs, verbose=False)
    return docs[:corpus_size] if corpus_size else docs


def load_books(corpus_size: int = 0, with_author_traits: bool = True,
               with_traits: bool = True) -> list[dict]:
    docs = load_docs(corpus_size)
    n = len(docs)

    # Phase 2: Wikidata author facts, if they have been harvested. Absent
    # file = the game simply has fewer questions, never wrong ones.
    wd = load_wikidata() if with_author_traits else {}
    works = load_works() if with_author_traits else {}
    protagonists = load_protagonists() if with_author_traits else {}
    # Model-extracted traits, if the extraction has been run. Absent file =
    # the game simply has fewer questions, never wrong ones.
    #
    # `with_traits` exists to answer one question honestly: does the trait
    # extraction actually shorten a game? That needs the same seed, the same
    # corpus and the same books, with only the traits removed. Deleting the
    # file to find out would also destroy hours of quota, and comparing two
    # runs from different days compares the weather.
    extracted = load_traits() if (with_author_traits and with_traits) else {}
    book_counts: dict[str, int] = {}
    for doc in docs:
        for key in doc.get("author_key") or []:
            book_counts[key] = book_counts.get(key, 0) + 1

    books = []
    # Docs are sorted by readinglog_count, so index == popularity rank.
    for rank, doc in enumerate(docs):
        book = extract(doc, rank, n)
        names, tokens = extract_characters(book.pop("persons"))
        book["char_names"] = sorted(names)
        book["char_tokens"] = sorted(t for t in tokens if usable_token(t))

        if with_author_traits:
            present = set(book["present"])
            unknown = set(book["unknown"])
            known_false = set(book.get("known_false") or ())
            for key, value in book_traits(doc.get("author_key") or [],
                                          wd, book_counts).items():
                if value is None:
                    unknown.add(key)
                elif value:
                    present.add(key)
                else:
                    # Wikidata says the author IS American, so they are
                    # certainly not African. Scoring that like an unrecorded
                    # subject is what let the game ask both.
                    known_false.add(key)
            book["present"] = sorted(present)
            book["unknown"] = sorted(unknown)
            book["known_false"] = sorted(known_false)
            merge_into(book, doc, works, protagonists)

            # A trait the model asserted is `present`; everything else is
            # `unknown`, NOT absent — including books it never saw. Shared
            # with build_matrix.py on purpose: this file measures the game
            # that file ships, so a private copy of this rule is a way for
            # the measurement to stop describing the artifact.
            apply_labels(book, doc.get("key") or "", extracted)

        books.append(book)
    return books


def select_questions(books: list[dict], verbose: bool = True) -> list[str]:
    n = len(books)
    counts: dict[str, int] = {}
    for b in books:
        for f in b["present"]:
            counts[f] = counts.get(f, 0) + 1

    kept, dropped = [], []
    all_keys = set(counts) | {k for b in books for k in b["unknown"]}
    for key in sorted(all_keys):
        freq = counts.get(key, 0) / n
        ok = (MIN_FREQ <= freq <= MAX_FREQ) or (key in FORCE_KEEP and freq > 0)
        (kept if ok else dropped).append((key, freq))

    if verbose:
        print(f"Feature selection ({MIN_FREQ:.0%}-{MAX_FREQ:.0%} band): "
              f"kept {len(kept)}, dropped {len(dropped)}")
        for key, freq in sorted(kept, key=lambda x: -x[1])[:12]:
            label = (QUESTION_TEXT.get(key) or STRUCTURAL_QUESTIONS.get(key)
                     or AUTHOR_QUESTIONS.get(key) or WORK_QUESTIONS.get(key)
                     or TRAIT_QUESTIONS.get(key, key))
            print(f"     {freq:5.1%}  {label}")
        print(f"     ... and {max(0, len(kept) - 12)} more")
    return [k for k, _ in kept]


def select_character_questions(books: list[dict], verbose: bool = True) -> list[str]:
    n = len(books)
    counts: dict[str, int] = {}
    for b in books:
        for t in b["char_tokens"]:
            counts[t] = counts.get(t, 0) + 1

    kept = [t for t, c in counts.items()
            if MIN_CHAR_BOOKS <= c <= max(1, int(n * MAX_CHAR_SHARE))]
    with_chars = sum(1 for b in books if b["char_tokens"])
    if verbose:
        print(f"Character questions: {len(kept)} name tokens "
              f"from {with_chars}/{n} books ({with_chars / n:.0%} coverage)")
        sample = sorted(kept)[:10]
        print("     e.g. " + ", ".join(sample))
    return [f"char:{t}" for t in kept]


def answer_as_player(book: dict, question: str, rng: random.Random,
                     noise: float, miss_rate: float, char_tokens: set[str]) -> str:
    """What a human who knows this book would say — not what the matrix says."""
    if question.startswith("char:"):
        token = question.split(":", 1)[1]
        truth = token in char_tokens
        if not truth and not char_tokens:
            # The player knows the cast even when OL doesn't record it, but
            # we have no ground truth to simulate from, so they hedge.
            return "unknown" if rng.random() < 0.5 else "no"
        # A player forgets minor names more often than they invent them.
        if truth and rng.random() < noise:
            return "unknown"
        return "yes" if truth else "no"

    if question in book["unknown"]:
        return "unknown"

    truth = question in book["present"]
    if not truth and rng.random() < miss_rate:
        # The metadata is missing something the reader knows is true.
        truth = True

    if rng.random() < noise:
        return rng.choice(["probably_yes", "unknown", "probably_no"])
    if truth:
        return "yes" if rng.random() > 0.15 else "probably_yes"
    return "no" if rng.random() > 0.20 else "probably_no"


def play(matrix: Matrix, target_idx: int, rng: random.Random,
         max_questions: int, max_guesses: int, noise: float,
         miss_rate: float) -> tuple[bool, int]:
    """Returns (guessed correctly, questions used)."""
    engine = Engine(matrix)
    target = matrix.books[target_idx]
    target_tokens = set(target["char_tokens"])
    rejected: set[int] = set()
    asked = 0

    while asked < max_questions:
        if engine.should_guess():
            for idx, _p in engine.ranking(10):
                if idx not in rejected:
                    if idx == target_idx:
                        return True, asked
                    rejected.add(idx)
                    engine.reject(idx)
                    break
            if len(rejected) >= max_guesses:
                return False, asked

        question = engine.next_question()
        if question is None:
            break
        engine.update(question, answer_as_player(
            target, question, rng, noise, miss_rate, target_tokens))
        asked += 1

    for idx, _p in engine.ranking(3):
        if idx not in rejected:
            return idx == target_idx, asked
    return False, asked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=2, help="plays per book")
    ap.add_argument("--max-questions", type=int, default=25)
    ap.add_argument("--max-guesses", type=int, default=3)
    ap.add_argument("--noise", type=float, default=0.10,
                    help="chance the player hedges on a question")
    ap.add_argument("--miss-rate", type=float, default=0.25,
                    help="chance the player asserts a feature OL never recorded")
    ap.add_argument("--no-traits", action="store_true",
                    help="ignore the model-extracted traits. Run it with and "
                         "without, same --seed, to measure what they buy.")
    ap.add_argument("--no-characters", action="store_true",
                    help="disable endgame character questions (for A/B)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sample", type=int, default=0,
                    help="test only the N most popular books (0 = all)")
    ap.add_argument("--corpus-size", type=int, default=0,
                    help="truncate the corpus by popularity (0 = all)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    books = load_books(args.corpus_size, with_traits=not args.no_traits)
    verbose = not args.quiet
    if verbose:
        print(f"Corpus: {len(books)} books\n")

    questions = select_questions(books, verbose)
    char_questions = [] if args.no_characters else select_character_questions(books, verbose)
    if verbose:
        print()
    if len(questions) < 8:
        print("!! Too few usable features to play at all.")
        sys.exit(1)

    matrix = Matrix(books, questions, char_questions)
    rng = random.Random(args.seed)
    targets = range(args.sample or len(books))

    results: list[tuple[bool, int]] = []
    per_tier: dict[str, list[bool]] = {"top 50": [], "51-200": [], "201+": []}

    for idx in targets:
        tier = "top 50" if idx < 50 else ("51-200" if idx < 200 else "201+")
        for _ in range(args.trials):
            ok, used = play(matrix, idx, rng, args.max_questions,
                            args.max_guesses, args.noise, args.miss_rate)
            results.append((ok, used))
            per_tier[tier].append(ok)

    wins = [used for ok, used in results if ok]
    rate = len(wins) / len(results)

    print(f"Played {len(results)} games "
          f"(noise={args.noise}, miss_rate={args.miss_rate}, "
          f"characters={'off' if args.no_characters else 'on'}, "
          f"cap={args.max_questions}q / {args.max_guesses} guesses)")
    print()
    print(f"  success rate      : {rate:.1%}")
    if wins:
        print(f"  median questions  : {statistics.median(wins):.0f}")
        print(f"  mean questions    : {statistics.mean(wins):.1f}")
    print()
    for tier, oks in per_tier.items():
        if oks:
            print(f"  {tier:<8} success: {sum(oks) / len(oks):.1%}  ({len(oks)} games)")

    print()
    gate = rate >= 0.60 and wins and statistics.median(wins) <= args.max_questions
    print("GATE:", "PASS — the feature set can separate books." if gate else
          "FAIL — features are not discriminative enough yet.")


if __name__ == "__main__":
    main()
