"""Encode semantic NOT_APPLICABLE cells into an existing packed matrix.

This is intentionally a tiny artifact migration, not a corpus rebuild.  It
reads one packed row at a time, derives applicability only from that row's
grounded parent states, and updates matrix.bin plus its decoding metadata.

Example (the explicit bound prevents accidentally processing an unexpected
artifact set)::

    python scripts/akinator/migrate_not_applicable.py \
        --data-dir ../bookhub/games/data/akinator --max-books 6000
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from question_policy import (STATE_ABSENT, STATE_NOT_APPLICABLE,
                             STATE_PRESENT, STATE_UNKNOWN,
                             encode_not_applicable_matrix, policy_digest)


def _restore_columns(matrix: bytes, baseline: bytes, meta: dict,
                     questions: list[dict], question_ids: set[str]) -> bytes:
    """Restore selected 2-bit columns from a known pre-policy artifact."""
    if len(matrix) != len(baseline):
        raise ValueError("baseline matrix length differs from current matrix")
    indexes = [i for i, row in enumerate(questions)
               if row.get("id") in question_ids]
    missing = question_ids - {questions[i].get("id") for i in indexes}
    if missing:
        raise ValueError("unknown restore question(s): " + ", ".join(sorted(missing)))
    out = bytearray(matrix)
    row_bytes = int(meta["bytes_per_row"])
    for book in range(int(meta["books"])):
        base = book * row_bytes
        for q in indexes:
            bit = q * 2
            byte = base + (bit >> 3)
            shift = bit & 7
            state = (baseline[byte] >> shift) & 3
            out[byte] = (out[byte] & ~(3 << shift)) | (state << shift)
    return bytes(out)


def _git_matrix(data_dir: Path, revision: str) -> bytes:
    repo = subprocess.check_output(
        ["git", "-C", str(data_dir), "rev-parse", "--show-toplevel"],
        stderr=subprocess.DEVNULL, text=True).strip()
    relative = (data_dir / "matrix.bin").resolve().relative_to(
        Path(repo).resolve()).as_posix()
    return subprocess.check_output(
        ["git", "-C", repo, "show", f"{revision}:{relative}"],
        stderr=subprocess.DEVNULL)


def migrate(data_dir: Path, max_books: int, dry_run: bool = False,
            restore_from_git: str | None = None,
            restore_questions: set[str] | None = None) -> dict:
    meta_path = data_dir / "meta.json"
    questions_path = data_dir / "questions.json"
    matrix_path = data_dir / "matrix.bin"
    policy_path = data_dir / "question_policy.json"

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    matrix = matrix_path.read_bytes()
    if restore_questions:
        if not restore_from_git:
            raise ValueError("restore questions require --restore-from-git")
        matrix = _restore_columns(
            matrix, _git_matrix(data_dir, restore_from_git), meta, questions,
            restore_questions)
    matrix, stats = encode_not_applicable_matrix(
        matrix, meta, questions, policy, max_books)
    stats["restored_questions"] = len(restore_questions or ())

    meta["states"] = {
        "absent": STATE_ABSENT,
        "present": STATE_PRESENT,
        "unknown": STATE_UNKNOWN,
        "not_applicable": STATE_NOT_APPLICABLE,
    }
    meta["p_not_applicable"] = 0.5
    meta["question_policy_digest"] = policy_digest(policy)

    if not dry_run:
        matrix_tmp = matrix_path.with_suffix(".bin.tmp")
        meta_tmp = meta_path.with_suffix(".json.tmp")
        matrix_tmp.write_bytes(matrix)
        meta_tmp.write_text(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        os.replace(matrix_tmp, matrix_path)
        os.replace(meta_tmp, meta_path)

    return {**stats, "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--max-books", required=True, type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore-from-git")
    parser.add_argument("--restore-question", action="append", default=[])
    args = parser.parse_args()
    if args.max_books <= 0:
        raise SystemExit("--max-books must be positive")
    print(json.dumps(migrate(
        args.data_dir, args.max_books, args.dry_run,
        args.restore_from_git, set(args.restore_question)),
                     sort_keys=True))


if __name__ == "__main__":
    main()
