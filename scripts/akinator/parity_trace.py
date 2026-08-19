"""
scripts/akinator/parity_trace.py — record a game, so both engines can be
checked against it.

    python scripts/akinator/parity_trace.py --out ../bookhub/games/data/akinator/parity_trace.json

WHY THIS EXISTS. "The Python engine and the JavaScript engine must stay
identical" is a hard constraint and, until now, a purely manual one — two
files in two repos kept in step by remembering to. It has already failed
once in a way nothing could catch offline: `build_matrix.py`'s question
wording lookup omitted TRAIT_QUESTIONS while `simulate.py`'s had it, so the
simulation printed the real question and the artifact would have shipped
the raw key.

So: run one scripted game through THIS engine, write down every question it
asks and the belief vector after every answer, and commit that. A harness
then drives the browser engine through the same script and compares. A
divergence in the chooser, the weights, the floor, the exclusion rules or
the guess thresholds shows up as a mismatched line instead of as a worse
game nobody can explain.

FED FROM THE SHIPPED ARTIFACTS, NOT THE CORPUS. The browser has no corpus —
it has matrix.bin, questions.json, books.json and meta.json. Building the
Python side from the corpus instead would compare two different inputs and
call any difference an engine bug. So this reconstructs Matrix from exactly
the four files the page downloads, and then uses the REAL Engine class: the
thing under test is engine.py itself, not a reimplementation of it.

Determinism: the answer script is fixed, not sampled, so there is no seed
and no noise. This measures agreement, not quality — simulate.py measures
quality.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Engine, Matrix  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ARTIFACTS = os.path.join(REPO_ROOT, "..", "bookhub", "games", "data", "akinator")

# A fixed answer script. Deliberately mixed: firm and hedged answers in both
# directions, and "unknown" — which must skip the update entirely, so it is
# also a check that both engines agree on doing nothing.
ANSWER_SCRIPT = [
    "yes", "no", "probably_yes", "unknown", "no",
    "yes", "probably_no", "yes", "unknown", "no",
    "probably_yes", "yes", "no", "no", "probably_no",
    "yes", "unknown", "no", "yes", "probably_yes",
]


def load_artifacts(path: str) -> tuple[dict, list[dict], list[dict], bytes]:
    with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(path, "questions.json"), encoding="utf-8") as fh:
        questions = json.load(fh)
    with open(os.path.join(path, "books.json"), encoding="utf-8") as fh:
        books = json.load(fh)
    with open(os.path.join(path, "matrix.bin"), "rb") as fh:
        raw = fh.read()
    if len(raw) != meta["books"] * meta["bytes_per_row"]:
        raise SystemExit(
            f"matrix.bin is {len(raw)} bytes, meta.json implies "
            f"{meta['books'] * meta['bytes_per_row']} — artifacts are inconsistent")
    return meta, questions, books, raw


def books_from_artifacts(meta: dict, questions: list[dict],
                         books: list[dict], raw: bytes) -> list[dict]:
    """Rebuild the engine's book dicts from the packed matrix.

    The reverse of build_matrix.pack_matrix, and the same decoding the page
    does — two bits a cell, four cells a byte, question-major within a book.
    """
    nq, bpr = meta["questions"], meta["bytes_per_row"]
    qids = [q["id"] for q in questions]
    out = []
    for i, b in enumerate(books):
        off = i * bpr
        present, unknown = [], []
        for q in range(nq):
            state = (raw[off + (q >> 2)] >> ((q & 3) * 2)) & 3
            if state == 1:
                present.append(qids[q])
            elif state == 2:
                unknown.append(qids[q])
        out.append({
            "present": present,
            "unknown": unknown,
            "richness": b.get("r") or 0,
            "popularity": b.get("p") or 0,
            # Character questions are excluded from the trace on purpose:
            # they need characters.json and unlock only in the endgame, and
            # a 20-turn scripted game does not reach it. Keeping them out
            # means a trace mismatch always points at the main pool.
            "char_tokens": [],
            "key": b.get("k"),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    meta, questions, books_json, raw = load_artifacts(args.artifacts)
    books = books_from_artifacts(meta, questions, books_json, raw)
    qids = [q["id"] for q in questions]

    # The browser reads excluded.json out of this same directory before it
    # computes its prior, so the trace has to as well or the two engines are
    # not being asked the same question. Read from the artifacts dir rather
    # than through exclusions.py, which resolves the sibling checkout: the
    # point of this script is to model whatever is SHIPPED at `--artifacts`.
    excluded_path = os.path.join(args.artifacts, "excluded.json")
    excluded: set[str] = set()
    if os.path.exists(excluded_path):
        try:
            with open(excluded_path, encoding="utf-8") as fh:
                excluded = {k for k in json.load(fh) if isinstance(k, str)}
        except (OSError, json.JSONDecodeError):
            excluded = set()
    if excluded:
        print(f"excluded.json: {len(excluded)} book(s) zeroed in the prior")

    matrix = Matrix(books, qids, excluded=excluded)
    engine = Engine(matrix)

    turns = []
    for answer in ANSWER_SCRIPT:
        q = engine.next_question()
        if q is None:
            break
        engine.update(q, answer)
        top = engine.ranking(3)
        turns.append({
            "question": q,
            "answer": answer,
            # Full belief vectors would be a 5,000-number diff per turn. The
            # top three plus a checksum catches any divergence that matters
            # while keeping the fixture readable by a person.
            "top": [{"key": books[i]["key"], "p": round(p, 12)} for i, p in top],
            "belief_sum": round(sum(engine.belief), 12),
            "belief_checksum": round(
                sum(b * math.log1p(i + 1) for i, b in enumerate(engine.belief)), 12),
        })

    trace = {
        "generated_from": {
            "question_hash": meta.get("question_hash"),
            "books": meta["books"],
            "questions": meta["questions"],
        },
        "answer_script": ANSWER_SCRIPT,
        "turns": turns,
    }

    text = json.dumps(trace, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"{len(turns)} turns -> {args.out}")
        print(f"question_hash {meta.get('question_hash')}")
        for t in turns[:5]:
            print(f"  {t['question']:<24} {t['answer']:<14} top={t['top'][0]['key']}")
    else:
        print(text)


if __name__ == "__main__":
    main()
