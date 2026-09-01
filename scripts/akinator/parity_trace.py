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
import hashlib
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
#
# EXTENDED FROM 20 TO 29 when the re-check went in, and the length is now
# load-bearing rather than arbitrary. At 20 turns the leader is still so
# flat that `contradicted_question` finds nothing above RECHECK_MIN_CLASH,
# so the fixture recorded "no contradiction" and tested the feature's null
# path only. Nine more answers concentrate belief enough for a real clash to
# appear — and they also carry the trace past turn 23, which is the SECOND
# cold question. The first 20 are unchanged so the diff stays readable.
ANSWER_SCRIPT = [
    "yes", "no", "probably_yes", "unknown", "no",
    "yes", "probably_no", "yes", "unknown", "no",
    "probably_yes", "yes", "no", "no", "probably_no",
    "yes", "unknown", "no", "yes", "probably_yes",
    "no", "yes", "probably_no", "unknown", "yes",
    "no", "probably_yes", "no", "yes",
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


def overrides_fingerprint(overrides: dict) -> str:
    """A language-neutral rendering of the taught cells, for the digest.

    Mirrored byte for byte by `overridesFingerprint` in
    `games/parity-check.js`. Six fixed decimals because that is the one number
    format Python and JavaScript are guaranteed to agree on; booleans and
    anything non-numeric render as `?` rather than being coerced, since a
    malformed cell is refused by both engines and should still change the
    fingerprint if it appears.
    """
    parts = []
    for key in sorted(overrides):
        cells = overrides[key] or {}
        for q in sorted(cells):
            p = cells[q]
            if isinstance(p, bool) or not isinstance(p, (int, float)):
                value = "?"
            else:
                value = f"{float(p):.6f}"
            # "|" and not a NUL byte: a NUL in the JS mirror makes git treat
            # parity-check.js as a binary file, which it briefly did. Neither
            # a work key nor a question id can contain a pipe.
            parts.append(f"{key}|{q}|{value}")
    return "\n".join(parts)


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

    # Read from the artifacts directory for the same reason `excluded.json`
    # is: the browser fetches this file out of that directory and patches
    # `pYesCache` with it, so a trace generated without it compares two
    # engines that were given different inputs and blames the difference on
    # the engines. That is not hypothetical — it happened on 2026-08-23, and
    # reported a QUESTION MISMATCH on a build whose engines agreed 20/20 once
    # this file was taken into account.
    overrides_path = os.path.join(args.artifacts, "overrides.json")
    overrides: dict[str, dict[str, float]] = {}
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                overrides = {k: v for k, v in data.items()
                             if isinstance(k, str) and isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            overrides = {}
    if overrides:
        cells = sum(len(v) for v in overrides.values())
        print(f"overrides.json: {cells} learned cell(s) across "
              f"{len(overrides)} book(s)")

    # The third file the page reads out of this directory, for the third
    # time the same lesson. `excluded.json` and `overrides.json` were each
    # read by the browser and not by Python, and each reported a drift on
    # engines that agreed. Cold questions change WHICH QUESTION a turn asks,
    # so omitting them here would not report a subtle belief difference — it
    # would report a flat question mismatch from turn 15 on.
    cold_path = os.path.join(args.artifacts, "cold_questions.json")
    cold: list[str] = []
    if os.path.exists(cold_path):
        try:
            with open(cold_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                cold = [q["id"] for q in data
                        if isinstance(q, dict) and isinstance(q.get("id"), str)]
        except (OSError, json.JSONDecodeError, KeyError):
            cold = []
    if cold:
        print(f"cold_questions.json: {len(cold)} question(s) with no packed column")

    # The FOURTH file the page reads out of this directory, and the fourth
    # time the same lesson: excluded.json, overrides.json, cold_questions
    # .json, and now this. It decides which questions a firm yes suppresses,
    # so leaving it out here would diverge the two engines' question order,
    # not a belief in the twelfth decimal.
    excl_path = os.path.join(args.artifacts, "exclusive_overrides.json")
    exclusive_extra: list[list[str]] = []
    if os.path.exists(excl_path):
        try:
            with open(excl_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                exclusive_extra = [[q for q in g if isinstance(q, str)]
                                   for g in data if isinstance(g, list)]
        except (OSError, json.JSONDecodeError):
            exclusive_extra = []
    if exclusive_extra:
        print(f"exclusive_overrides.json: {len(exclusive_extra)} declared group(s)")

    matrix = Matrix(books, qids, excluded=excluded, overrides=overrides,
                    cold_questions=cold, exclusive_extra=exclusive_extra)
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

    # A FINGERPRINT OF THE OVERRIDES, and it closes a trap this file's own
    # fix would otherwise have opened. Now that the trace is recorded WITH
    # `overrides.json` applied, every cell the owner teaches from the admin
    # page invalidates it — and without something to compare, parity-check.js
    # would report that as "the engines have drifted" rather than as a stale
    # fixture. It already distinguishes the two for a nightly sync (books and
    # question_hash); this is the third input that moves underneath it, and it
    # moves far more often than the other two.
    # NOT a hash of the JSON text. `json.dumps` and `JSON.stringify` disagree
    # about numbers — Python writes a float 1.0 as "1.0" and JavaScript writes
    # it as "1" — and a fingerprint that drifts between the two languages
    # would report a stale fixture on every run, which is worse than not
    # having one. Fixed to six decimals on both sides instead, over a sorted
    # key\0question\0value list. The JS mirror is in games/parity-check.js and
    # the two must be edited together.
    overrides_digest = hashlib.sha256(
        overrides_fingerprint(overrides).encode("utf-8")).hexdigest()[:16]

    # THE RE-CHECK, scripted onto the end of the same game. Both halves are
    # recorded even when there is nothing to re-check: "both engines agree
    # there is no contradiction worth a turn" is a real check, and a fixture
    # that only exercised the feature when it happened to fire would go
    # quietly untested the first time the corpus shifted.
    #
    # The answer is fixed rather than derived, for the same reason
    # ANSWER_SCRIPT is: this measures agreement, not quality.
    stale = engine.contradicted_question()
    recheck = {"question": stale, "answer": "yes" if stale else None,
               "wants": engine.wants_recheck()}
    if stale is not None:
        engine.revise(stale, "yes")
        top = engine.ranking(3)
        recheck["top"] = [{"key": books[i]["key"], "p": round(p, 12)}
                          for i, p in top]
        recheck["belief_sum"] = round(sum(engine.belief), 12)
        recheck["belief_checksum"] = round(
            sum(b * math.log1p(i + 1) for i, b in enumerate(engine.belief)), 12)

    # THE SEEDED OPENING, which the scripted game above cannot reach. That
    # game runs on seed 0 — strict argmax — because a fixture has to be
    # deterministic, and seed 0 skips the new branch entirely. Recording only
    # that would ship a randomised chooser whose randomised path no check
    # ever executes, which is precisely how the cold-question fixture ended
    # up testing only its null case.
    #
    # So: several seeds, each replayed on the browser side through
    # start(seed). This is the test that the two mulberry32 implementations
    # agree, and it fails loudly if either drifts by a single bit.
    openings = {}
    for s in (1, 2, 3, 7, 42, 12345):
        openings[str(s)] = Engine(matrix, seed=s).next_question()
    print("seeded openings: " + ", ".join(
        f"{s}->{q}" for s, q in openings.items()))

    trace = {
        "openings": openings,
        "recheck": recheck,
        "generated_from": {
            "question_hash": meta.get("question_hash"),
            "books": meta["books"],
            "questions": meta["questions"],
            "overrides_digest": overrides_digest,
            "overrides_cells": sum(len(v) for v in overrides.values()),
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
        print(f"recheck: {recheck['question'] or 'nothing contradicted'}"
              f" (leader weak: {recheck['wants']})")
        for t in turns[:5]:
            print(f"  {t['question']:<24} {t['answer']:<14} top={t['top'][0]['key']}")
    else:
        print(text)


if __name__ == "__main__":
    main()
