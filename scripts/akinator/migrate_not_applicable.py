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
from pathlib import Path

from question_policy import (STATE_ABSENT, STATE_NOT_APPLICABLE,
                             STATE_PRESENT, STATE_UNKNOWN,
                             encode_not_applicable_matrix, policy_digest)


def migrate(data_dir: Path, max_books: int, dry_run: bool = False) -> dict:
    meta_path = data_dir / "meta.json"
    questions_path = data_dir / "questions.json"
    matrix_path = data_dir / "matrix.bin"
    policy_path = data_dir / "question_policy.json"

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    matrix, stats = encode_not_applicable_matrix(
        matrix_path.read_bytes(), meta, questions, policy, max_books)

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
    args = parser.parse_args()
    if args.max_books <= 0:
        raise SystemExit("--max-books must be positive")
    print(json.dumps(migrate(args.data_dir, args.max_books, args.dry_run),
                     sort_keys=True))


if __name__ == "__main__":
    main()
