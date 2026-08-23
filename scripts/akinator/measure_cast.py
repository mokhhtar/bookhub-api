"""
scripts/akinator/measure_cast.py — what a recorded cast is actually worth.

    python scripts/akinator/measure_cast.py --sample 150 --seed 7

THE QUESTION THIS EXISTS TO ANSWER, before section E spends a day harvesting
names. Cast coverage is 1,740 of 5,004 books (34.8%). The obvious move is to
raise it. The obvious move is only worth making if the books that HAVE a cast
are being helped by it — and that is measurable today, for free, without
harvesting anything:

    take the books that have a cast, hide it from the engine, and see what
    the game loses.

Whatever that loss is, it is the CEILING on what covering a book that has no
cast could buy. If it is zero, section E is spending on something that is not
the bottleneck, and no amount of new data changes that.

THREE ARMS, because "having a cast" is two different things at once:

  keep       nothing hidden — the game as shipped.
  hide       the target's character tokens are hidden from the engine. The
             simulated player still knows the cast (a reader does not forget
             Hermione because Open Library did), so this isolates exactly
             what the NAMES buy.
  unrecord   the honest counterfactual: hide the names AND score
             `fact:namedchars` as ordinary absence. A book with no cast today
             has no `person` field, and `has_named_characters([])` is False,
             which lands in `known_false` and is scored as absence. So an
             uncovered book does not merely lack names — it actively answers
             "no, it has no well-known named characters". `hide` vs
             `unrecord` separates the two effects.

PAIRED, ONE RNG PER GAME. Every arm plays the same book against the same
simulated player, seeded from the book's own key exactly as simulate.py does.
Comparisons are per-book (McNemar), never rate-against-rate — see
[[akinator-paired-measurement]] for what a shared RNG did to an earlier run.

THE INSTRUMENTATION IS THE OTHER HALF. Character questions are gated behind
`effective_candidates() <= ENDGAME_CANDIDATES`, and a web-novel game ends at a
median of 1,033 candidates. So before believing any delta, this records how
many games reach the gate at all, how many then pass `_expects_named_characters`,
and how many character questions actually get asked. A zero delta means
something very different when the gate never opened than when it opened and
the questions bought nothing.

`play_instrumented` is a copy of `simulate.play` with counters added, which is
exactly the shape of failure #3 in the vault (a simulator quietly describing a
different game). `--self-check` guards it: it runs both functions on the same
seeds and asserts the outcomes are identical, and it is not optional in any
run whose number gets quoted.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import simulate                                                   # noqa: E402
import parity_trace                                               # noqa: E402
from engine import (CHAR_UNKNOWN_CONFIDENCE, ENDGAME_CANDIDATES,   # noqa: E402
                    Engine, Matrix, _binary_entropy)
from features import absence_confidence                            # noqa: E402

NAMEDCHARS = "fact:namedchars"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARTIFACTS = os.path.join(REPO_ROOT, "..", "bookhub", "games", "data", "akinator")


# ---------------------------------------------------------------------------
# The shipped game, rebuilt from the four files the browser downloads
# ---------------------------------------------------------------------------

def load_shipped(path: str) -> tuple[list[dict], list[str], list[str],
                                     list[str | None], dict, set[str]]:
    """The live game, not a re-derivation of it.

    THIS IS NOT THE OBVIOUS CHOICE AND IT IS THE RIGHT ONE. `simulate.py`
    rebuilds the corpus from `akinator_corpus.jsonl` plus whatever harvests
    happen to be sitting in `data/`, which is how a measurement drifts away
    from the artifact it claims to describe. Run today it selects **50**
    questions — `t:animals`, `t:magic` and `t:school` cross the 5% floor on
    the section B trait labels that are on disk and were deliberately never
    shipped — against the **48** the live game asks, and it loses
    `t:powersystem`, which the live game has. Measuring section E against
    that corpus would be measuring a game nobody plays, which is failure
    shape #3 in the vault, five appearances and counting.

    So this reconstructs the engine's inputs from `meta.json`,
    `questions.json`, `books.json` and `matrix.bin` — exactly what
    `parity_trace.py` does, and for the same reason. `characters.json`
    supplies the cast (which the trace deliberately omits, since a 20-turn
    scripted game never reaches the endgame), `series.json` the pooling, and
    `excluded.json` the zeroed priors.
    """
    meta, questions, books_json, raw = parity_trace.load_artifacts(path)
    books = parity_trace.books_from_artifacts(meta, questions, books_json, raw)
    qids = [q["id"] for q in questions]

    with open(os.path.join(path, "characters.json"), encoding="utf-8") as fh:
        chars = json.load(fh)
    tokens, per_book = chars["tokens"], chars["books"]
    # `characters.json` is written by the monthly build; the nightly sync
    # appends books to books.json without it, so it is legitimately SHORTER
    # than the corpus. The page reads `chars.books[i] || []` for the same
    # reason. A missing row means no cast, never a shifted one, because the
    # sync appends.
    for i, book in enumerate(books):
        ids = per_book[i] if i < len(per_book) else []
        book["char_tokens"] = [tokens[t] for t in ids]
    char_questions = [f"char:{t}" for t in tokens]

    series_of: list[str | None] = [None] * len(books)
    series_names: dict[str, str] = {}
    spath = os.path.join(path, "series.json")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as fh:
            s = json.load(fh)
        series_names = s.get("names") or {}
        for i, sid in enumerate(s.get("books") or []):
            if i < len(series_of):
                series_of[i] = sid

    excluded: set[str] = set()
    xpath = os.path.join(path, "excluded.json")
    if os.path.exists(xpath):
        with open(xpath, encoding="utf-8") as fh:
            excluded = {k for k in json.load(fh) if isinstance(k, str)}

    return books, qids, char_questions, series_of, series_names, excluded


# ---------------------------------------------------------------------------
# The ablation
# ---------------------------------------------------------------------------

class NamedCharsAsUnknown:
    """Score `fact:namedchars` as UNKNOWN for every book with no `person` data.

    WHAT THIS MODELS. `has_named_characters([])` returns False, which lands in
    `known_false` and is scored as ordinary absence — so 3,190 of the shipped
    5,004 books answer "no, it has no well-known named characters" on the
    strength of an EMPTY Open Library field. The rule was written to protect
    *The Road*, whose cast really is "the man" and "the boy"; measured against
    the corpus it fires correctly **7** times and wrongly **3,190**.
    `structural_features`' own docstring says a fact we cannot determine must
    be `None`, and this is the one field in it that ignores that.

    THE SIMULATION TRAP THIS HAS TO STEP AROUND, which is why the fix cannot
    just be dropped into `simulate.py` and re-run. The simulated player
    answers from `book["present"]`, which is derived from the SAME broken
    field — so out of the box the player says "no" in agreement with the lie,
    the fix looks like it only removes information, and the run reports a
    loss. That is failure shape #3 dressed up as a result.

    So the player is given a ground truth the matrix does not have:
    `fact:namedchars` is TRUE for a book that is fiction or has a recorded
    cast. Editing `book["present"]` is safe after `Matrix.__init__` has run —
    the rows are already copied, so this moves the PLAYER and never the table.

    THE ASSUMPTION IS STATED BECAUSE IT IS ONE: "a novel has characters a
    reader can name" is true of nearly every novel and false of *The Road*.
    Books whose `person` field is non-empty are excluded from it entirely —
    those were actually examined, and their `False` is a finding rather than a
    silence.
    """

    def __init__(self, matrix: Matrix, no_person: set[str],
                 patch_table: bool, patch_player: bool):
        # THE TWO PATCHES ARE SEPARATE FLAGS ON PURPOSE. The player's ground
        # truth must be IDENTICAL in both arms or the comparison is between
        # two different players rather than two different tables. So the
        # control arm runs with patch_player=True and patch_table=False: the
        # reader knows their novel has named characters in both arms, and the
        # only thing that changes is whether the matrix contradicts them.
        self.m = matrix
        self.no_person = no_person
        self.patch_table = patch_table
        self.patch_player = patch_player
        self.saved: dict[int, tuple[float, float]] = {}
        self.saved_present: dict[int, list[str]] = {}

    def __enter__(self):
        m = self.m
        if NAMEDCHARS not in m.question_set:
            return self
        h = _binary_entropy(0.5)
        for i, book in enumerate(m.books):
            if book["key"] not in self.no_person:
                continue
            if NAMEDCHARS in book["present"] or NAMEDCHARS in book["unknown"]:
                continue                    # already not an asserted "no"
            if self.patch_table:
                self.saved[i] = (m.rows[i][NAMEDCHARS], m.hrows[i][NAMEDCHARS])
                m.rows[i][NAMEDCHARS] = 0.5     # UNKNOWN_CONFIDENCE
                m.hrows[i][NAMEDCHARS] = h
            if self.patch_player and "form:fiction" in book["present"]:
                self.saved_present[i] = list(book["present"])
                book["present"] = book["present"] + [NAMEDCHARS]
        return self

    def __exit__(self, *exc):
        m = self.m
        for i, (p, h) in self.saved.items():
            m.rows[i][NAMEDCHARS] = p
            m.hrows[i][NAMEDCHARS] = h
        for i, present in self.saved_present.items():
            m.books[i]["present"] = present
        return False


class HiddenCast:
    """Make the engine forget one book's cast for the duration of one game.

    Mutates the shared Matrix in place and restores it, because rebuilding a
    5,004-book Matrix per game costs more than every game in the run put
    together. Restoration is in `__exit__` so an exception cannot leave the
    matrix poisoned for the games that follow — which would silently ablate
    the control arm.

    NOT ablated: the character question LIST. A token only this book carries
    would not exist as a question in a world where this book was never
    harvested, but `Engine._pool` only ever offers tokens held by a live
    candidate, and this book now holds none — so such a question can never be
    asked either way. Leaving the list identical across arms keeps the two
    games comparable in every respect except the one under test.
    """

    def __init__(self, matrix: Matrix, idx: int, unrecord: bool):
        self.m, self.idx, self.unrecord = matrix, idx, unrecord
        self.saved_cells: dict[str, tuple[float, float]] = {}
        self.saved_tokens: list[str] = []

    def __enter__(self):
        m, i = self.m, self.idx
        row, hrow = m.rows[i], m.hrows[i]
        for q in m.char_questions:
            self.saved_cells[q] = (row[q], hrow[q])
            row[q] = CHAR_UNKNOWN_CONFIDENCE
            hrow[q] = _binary_entropy(CHAR_UNKNOWN_CONFIDENCE)
        self.saved_tokens = list(m.books[i].get("char_tokens") or ())
        m.books[i]["char_tokens"] = []

        if self.unrecord and NAMEDCHARS in m.question_set:
            self.saved_cells[NAMEDCHARS] = (row[NAMEDCHARS], hrow[NAMEDCHARS])
            p = absence_confidence(m.books[i]["richness"])
            row[NAMEDCHARS] = p
            hrow[NAMEDCHARS] = _binary_entropy(p)
        return self

    def __exit__(self, *exc):
        m, i = self.m, self.idx
        row, hrow = m.rows[i], m.hrows[i]
        for q, (p, h) in self.saved_cells.items():
            row[q], hrow[q] = p, h
        m.books[i]["char_tokens"] = self.saved_tokens
        return False


# ---------------------------------------------------------------------------
# The game, with counters
# ---------------------------------------------------------------------------

def play_instrumented(matrix: Matrix, target_idx: int, rng: random.Random,
                      max_questions: int, max_guesses: int, noise: float,
                      miss_rate: float, player_tokens: set[str]) -> dict:
    """simulate.play, plus a record of whether the endgame ever opened.

    `player_tokens` is passed in rather than read off the book, because under
    ablation the book's tokens are gone from the matrix while the person
    playing still knows them. Reading them off the book would model a reader
    who forgot the cast, which is not the counterfactual anyone cares about.
    """
    engine = Engine(matrix)
    target = matrix.books[target_idx]
    rejected: set[int] = set()
    rejected_series: set[str] = set()
    guesses = 0
    asked = 0
    reached_gate = False       # effective_candidates() <= ENDGAME_CANDIDATES
    gate_opened = False        # ...and _expects_named_characters() agreed
    char_asked = 0
    namedchars_turn = 0        # which turn asked `fact:namedchars`, 0 = never
    min_candidates = 10 ** 9

    def offer() -> bool | None:
        nonlocal guesses
        target_g = engine.guess_target(rejected | rejected_series)
        if target_g is None:
            return False
        guesses += 1
        if target_g["kind"] == "series":
            sid = target_g["sid"]
            members = matrix.series_members.get(sid, [])
            if target_idx in members:
                return True
            rejected_series.add(sid)
            hit = members
        else:
            if target_g["index"] == target_idx:
                return True
            hit = [target_g["index"]]
        for i in hit:
            rejected.add(i)
            engine.reject(i)
        return False if guesses >= max_guesses else None

    # THE CONTROL FLOW IS COPIED LITERALLY, and the first draft of this
    # function already got it wrong: `question is None` breaks out of the
    # question loop and then STILL guesses with whatever guesses are left,
    # while a finished game returns immediately. Collapsing those two exits
    # into one `break` silently turned every out-of-questions game into a
    # loss. Caught by reading, before `--self-check` ever ran.
    won = False
    over = False
    while asked < max_questions:
        if engine.should_guess() and guesses < max_guesses:
            done = offer()
            if done is not None:
                won, over = done, True
                break

        # Read the gate before the question is chosen, so this describes the
        # state `_pool` is about to see rather than the state after it acted.
        # Both calls are pure and draw nothing from `rng`, so instrumenting
        # here cannot move the game.
        eff = engine.effective_candidates()
        min_candidates = min(min_candidates, eff)
        if matrix.char_questions and eff <= ENDGAME_CANDIDATES:
            reached_gate = True
            if engine._expects_named_characters():
                gate_opened = True

        question = engine.next_question()
        if question is None:
            break
        if question.startswith("char:"):
            char_asked += 1
        elif question == NAMEDCHARS:
            namedchars_turn = asked + 1
        engine.update(question, simulate.answer_as_player(
            target, question, rng, noise, miss_rate, player_tokens))
        asked += 1

    if not over:
        while guesses < max_guesses:
            done = offer()
            if done is not None:
                won = done
                break

    rank = next((r for r, (i, _p) in enumerate(engine.ranking(50))
                 if i == target_idx), None)
    return {
        "won": won, "asked": asked, "reached_gate": reached_gate,
        "gate_opened": gate_opened, "char_asked": char_asked,
        # How close the game came to the endgame at its most concentrated,
        # and what the belief state looked like there. "Never reached the
        # gate" is a useless finding without this: 34 candidates is a near
        # miss worth engineering around and 1,000 is a different problem.
        "min_candidates": min_candidates,
        "final_top_p": engine.ranking(1)[0][1],
        "final_candidates": engine.effective_candidates(),
        # The quantity `_expects_named_characters` compares against 0.75.
        # Recorded as the number rather than the verdict, because "the gate
        # was shut" and "the gate was shut at 0.42" are different findings.
        "namedchars_belief": sum(b * row[NAMEDCHARS]
                                 for b, row in zip(engine.belief, matrix.rows))
        if NAMEDCHARS in matrix.question_set else None,
        "namedchars_turn": namedchars_turn,
        "final_rank": rank,
    }


def self_check(matrix: Matrix, targets: list[int], args) -> None:
    """Assert the instrumented loop is still the loop simulate.py measures.

    A copied game loop that drifts from the original is failure shape #3, and
    it has cost this project more than any other mistake. This is cheap, so it
    runs before every measurement rather than when someone remembers.
    """
    bad = 0
    for idx in targets:
        key = matrix.books[idx]["key"]
        tokens = set(matrix.books[idx]["char_tokens"])
        a = simulate.play(matrix, idx, random.Random(f"{args.seed}:{key}:0"),
                          args.max_questions, args.max_guesses,
                          args.noise, args.miss_rate)
        b = play_instrumented(matrix, idx, random.Random(f"{args.seed}:{key}:0"),
                              args.max_questions, args.max_guesses,
                              args.noise, args.miss_rate, tokens)
        if a != (b["won"], b["asked"]):
            bad += 1
            print(f"  !! DIVERGENCE on {key}: simulate={a} "
                  f"instrumented={(b['won'], b['asked'])}")
    if bad:
        raise SystemExit(f"self-check failed on {bad}/{len(targets)} games — "
                         f"the instrumented loop is not the shipped game")
    print(f"  self-check: {len(targets)}/{len(targets)} games identical to "
          f"simulate.play")


# ---------------------------------------------------------------------------
# McNemar, the same test the vault's paired runs use
# ---------------------------------------------------------------------------

def mcnemar(a: dict[str, bool], b: dict[str, bool]) -> str:
    """Exact binomial two-sided p over the discordant pairs."""
    keys = [k for k in a if k in b]
    a_only = sum(1 for k in keys if a[k] and not b[k])
    b_only = sum(1 for k in keys if b[k] and not a[k])
    n = a_only + b_only
    if n == 0:
        return f"  0 discordant pairs of {len(keys)} — no evidence either way"
    import math
    lo = min(a_only, b_only)
    p = sum(math.comb(n, i) for i in range(lo + 1)) / (2 ** n) * 2
    p = min(1.0, p)
    return (f"  discordant {n}/{len(keys)}: first-only {a_only}, "
            f"second-only {b_only}, exact p={p:.4f}")


# ---------------------------------------------------------------------------

def load_no_person(path: str | None) -> set[str]:
    if not path:
        raise SystemExit("--mode namedchars needs --no-person: the artifacts "
                         "record only that a cell is ABSENT, never whether "
                         "that came from an empty field or from a real check")
    with open(path, encoding="utf-8") as fh:
        return set(json.load(fh))


def gap_line(rs: list[dict]) -> str:
    """How close the tightest moment of each game came to the endgame gate.

    "Never reached the gate" on its own is not actionable — it could mean a
    handful of games missed by two candidates, or that the whole distribution
    lives two orders of magnitude away. The percentiles say which, and that
    decides whether the gate is a threshold worth tuning or the wrong lever
    entirely.
    """
    v = sorted(r["min_candidates"] for r in rs)
    if not v:
        return ""

    def pct(p: float) -> int:
        return v[min(len(v) - 1, int(p * len(v)))]
    return (f"candidates at the tightest turn: best {v[0]}, "
            f"p10 {pct(0.10)}, median {pct(0.50)}, p90 {pct(0.90)}")


def tier_of(idx: int) -> str:
    return ("1-500" if idx < 500 else "501-1500" if idx < 1500
            else "1501-3000" if idx < 3000 else "3001+")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=150,
                    help="how many books with a cast to play. Drawn UNIFORMLY "
                         "at random from all 1,740, not from the head of the "
                         "popularity list — a rate quoted off the top of a "
                         "sorted list is failure shape #5.")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sample-seed", type=int, default=1,
                    help="separate seed for WHICH books are drawn, so the "
                         "sample can be changed without changing the players")
    ap.add_argument("--max-questions", type=int, default=30)
    ap.add_argument("--max-guesses", type=int, default=3)
    ap.add_argument("--noise", type=float, default=0.10)
    ap.add_argument("--miss-rate", type=float, default=0.25)
    ap.add_argument("--artifacts", default=ARTIFACTS,
                    help="the shipped game to measure. See load_shipped for "
                         "why this is not rebuilt from the corpus.")
    ap.add_argument("--mode", default="cast", choices=("cast", "namedchars"),
                    help="`cast` ablates one book's recorded cast (the "
                         "section E question). `namedchars` scores the "
                         "asserted-from-silence 'no' as unknown for all 3,190 "
                         "books with no `person` field, which is a different "
                         "question that the cast run turned up.")
    ap.add_argument("--no-person", default=None,
                    help="JSON list of work keys whose `person` field is "
                         "empty in the corpus. Required by --mode namedchars; "
                         "the shipped artifacts cannot distinguish 'no data' "
                         "from 'examined and found role-only', and conflating "
                         "them would ablate The Road along with the rest.")
    ap.add_argument("--arms", default="keep,hide,unrecord")
    ap.add_argument("--also-uncovered", type=int, default=0,
                    help="additionally play N books that have NO cast today, "
                         "as the control the ablation cannot provide: how "
                         "often does a game even reach the endgame gate for "
                         "the books section E would be harvesting?")
    ap.add_argument("--out", default=None, help="write per-game JSON here")
    args = ap.parse_args()

    books, questions, char_questions, series_of, series_names, excluded = \
        load_shipped(args.artifacts)
    matrix = Matrix(books, questions, char_questions,
                    series_of=series_of, series_names=series_names,
                    excluded=excluded)
    with open(os.path.join(args.artifacts, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    print(f"Shipped artifacts: question_hash {meta['question_hash']}, "
          f"{meta['books']} books, {meta['questions']} questions, "
          f"{len(excluded)} excluded")

    covered = [i for i, b in enumerate(books) if b["char_tokens"]]
    uncovered = [i for i, b in enumerate(books) if not b["char_tokens"]]
    print(f"Corpus {len(books)} books, {len(questions)} questions, "
          f"{len(char_questions)} character questions")
    print(f"Cast coverage: {len(covered)}/{len(books)} "
          f"({len(covered) / len(books):.1%})\n")

    rng = random.Random(args.sample_seed)
    if args.mode == "namedchars":
        # Targets are drawn from the WHOLE corpus here, not from the covered
        # books: the change under test touches 3,190 rows, and sampling only
        # the ones that already answer `present` would measure the effect on
        # the books it does not change.
        pool = [i for i in range(len(books))
                if books[i]["key"] in load_no_person(args.no_person)]
        targets = sorted(rng.sample(pool, min(args.sample, len(pool))))
        ctrl = []
    else:
        targets = sorted(rng.sample(covered, min(args.sample, len(covered))))
        ctrl = sorted(rng.sample(uncovered, min(args.also_uncovered,
                                                len(uncovered))))

    print("Self-check before anything is measured:")
    self_check(matrix, targets[:8], args)
    print()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    results: dict[str, dict[str, dict]] = {a: {} for a in arms}

    no_person = load_no_person(args.no_person) if args.mode == "namedchars" else set()

    def run(arm: str, idx: int) -> dict:
        key = books[idx]["key"]
        rng_g = random.Random(f"{args.seed}:{key}:0")

        def go() -> dict:
            return play_instrumented(matrix, idx, rng_g, args.max_questions,
                                     args.max_guesses, args.noise,
                                     args.miss_rate,
                                     set(books[idx]["char_tokens"]))
        if args.mode == "namedchars":
            with NamedCharsAsUnknown(matrix, no_person,
                                     patch_table=(arm == "fixed"),
                                     patch_player=True):
                return go()
        if arm == "keep":
            return go()
        with HiddenCast(matrix, idx, unrecord=(arm == "unrecord")):
            return go()

    for arm in arms:
        print(f"--- arm {arm}: {len(targets)} games ---", flush=True)
        for n, idx in enumerate(targets, 1):
            results[arm][books[idx]["key"]] = run(arm, idx)
            if n % 25 == 0:
                won = sum(1 for r in results[arm].values() if r["won"])
                print(f"    {n}/{len(targets)}  running {won / n:.1%}",
                      flush=True)

    control: dict[str, dict] = {}
    if ctrl:
        print(f"--- control: {len(ctrl)} books with NO cast, unablated ---",
              flush=True)
        for n, idx in enumerate(ctrl, 1):
            key = books[idx]["key"]
            control[key] = play_instrumented(
                matrix, idx, random.Random(f"{args.seed}:{key}:0"),
                args.max_questions, args.max_guesses, args.noise,
                args.miss_rate, set())
            if n % 25 == 0:
                print(f"    {n}/{len(ctrl)}", flush=True)

    # -- report ------------------------------------------------------------
    print("\n" + "=" * 68)
    print(f"BOOKS WITH A CAST — {len(targets)} of {len(covered)}, "
          f"drawn uniformly across all ranks")
    print("=" * 68)
    for arm in arms:
        rs = list(results[arm].values())
        won = [r for r in rs if r["won"]]
        print(f"\n{arm}")
        print(f"  success           {len(won) / len(rs):.1%}  ({len(won)}/{len(rs)})")
        if won:
            print(f"  median questions  {statistics.median(r['asked'] for r in won):.0f}")
        print(f"  reached the gate  {sum(1 for r in rs if r['reached_gate'])}/{len(rs)}"
              f"   (effective_candidates() <= {ENDGAME_CANDIDATES})")
        print(f"  gate OPENED       {sum(1 for r in rs if r['gate_opened'])}/{len(rs)}")
        print(f"  asked >=1 name    {sum(1 for r in rs if r['char_asked'])}/{len(rs)}")
        print(f"  name questions    {sum(r['char_asked'] for r in rs)} total")
        print(f"  {gap_line(rs)}")
        nt = [r["namedchars_turn"] for r in rs]
        asked_nc = [t for t in nt if t]
        print(f"  fact:namedchars asked in {len(asked_nc)}/{len(nt)} games"
              + (f", median turn {statistics.median(asked_nc):.0f}" if asked_nc else ""))
        nb = [r["namedchars_belief"] for r in rs if r["namedchars_belief"] is not None]
        if nb:
            print(f"  namedchars belief at game end: median {statistics.median(nb):.2f} "
                  f"(gate needs >= 0.75); over gate in "
                  f"{sum(1 for v in nb if v >= 0.75)}/{len(nb)}")
        ranks = [r["final_rank"] for r in rs if r["final_rank"] is not None]
        if ranks:
            print(f"  median final rank {statistics.median(ranks):.0f} "
                  f"(target outside top 50 in {len(rs) - len(ranks)} games)")

    print("\nPAIRED (McNemar, exact):")
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            wa = {k: v["won"] for k, v in results[a].items()}
            wb = {k: v["won"] for k, v in results[b].items()}
            print(f"  {a} vs {b}")
            print(mcnemar(wa, wb))

    if "keep" in arms:
        print("\nBy rank tier (success, keep arm):")
        by: dict[str, list[bool]] = {}
        for idx in targets:
            by.setdefault(tier_of(idx), []).append(
                results["keep"][books[idx]["key"]]["won"])
        for t in ("1-500", "501-1500", "1501-3000", "3001+"):
            if by.get(t):
                v = by[t]
                print(f"  {t:<10} {sum(v) / len(v):.1%}  ({len(v)} games)")

    if control:
        rs = list(control.values())
        won = sum(1 for r in rs if r["won"])
        print("\n" + "=" * 68)
        print(f"CONTROL — {len(rs)} books with NO cast today "
              f"(what section E would harvest)")
        print("=" * 68)
        print(f"  success           {won / len(rs):.1%}")
        print(f"  reached the gate  {sum(1 for r in rs if r['reached_gate'])}/{len(rs)}")
        print(f"  gate OPENED       {sum(1 for r in rs if r['gate_opened'])}/{len(rs)}")
        print(f"  asked >=1 name    {sum(1 for r in rs if r['char_asked'])}/{len(rs)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"arms": results, "control": control,
                       "args": vars(args)}, fh)
        print(f"\nper-game outcomes -> {args.out}")


if __name__ == "__main__":
    main()
