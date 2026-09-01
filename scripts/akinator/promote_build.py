"""
scripts/akinator/promote_build.py — the gate between a rebuild and the game.

    python scripts/akinator/promote_build.py --dry-run
    python scripts/akinator/promote_build.py

WHY A GATE AND NOT A HABIT. `build_matrix.py` writes to `data/akinator_build`
and never touches what ships, so promoting a build is a manual copy — and a
manual copy is where a book list silently changes. The shipped artifact is
not a plain build: `akinator_sync.py` appends newly published books between
rebuilds, so a fresh build re-truncates the corpus and its row set will not
match. Someone has to look at the difference. This makes looking automatic
and makes the safe path the easy one.

THE ONE CLASSIFICATION THAT MATTERS. A key present in the shipped artifact
and absent from the build is not automatically a loss. On 2026-09-01 eight
such keys looked like "the rebuild deletes eight published pages", and after
checking each one:

    3   EXCLUDED by hand in excluded.json  -> the rebuild is CLEANING UP
                                              rows the owner already killed
    4   RE-KEYED: a book with the same title IS in the build under another
        key (/site/admin-scaramouche -> /site/scaramouche, /site/the-idiot
        -> /works/OL166925W)          -> the book stays, its key moves
    1   genuinely gone: /site/admin-lord-of-mysteries-2-…, added through the
        admin page with no _books/ entry to be re-pinned from

So one real loss, not eight. Reporting the raw count would cry wolf and get
the check ignored, which is worse than not having it.

RE-KEYING IS NOT FREE, and the report says so rather than waving it through.
`overrides.json`, `overrides_locked.json` and the play counts in Redis are
all keyed by work_key. A row that changes key keeps its cells but loses
every hand-set clamp, lock and learned count attached to the old one. That
is a decision, not a detail — so re-keyed rows are listed, always, even
though they do not block.

EXIT CODES: 0 promoted (or nothing to do), 1 refused. --force promotes
anyway and is meant for the case where the loss is understood and wanted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BUILD = os.path.join(REPO_ROOT, "data", "akinator_build")
DEFAULT_SHIPPED = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator"))

# Written by build_matrix.py. Everything else in the shipped directory is
# owned by something else — overrides.json by the drain, excluded.json and
# display_overrides.json by the admin page, cold_questions.json and
# exclusive_overrides.json by hand — and promoting a build must never
# overwrite a file the build did not produce.
BUILD_FILES = ("meta.json", "questions.json", "books.json", "matrix.bin",
               "characters.json", "authors.json", "series.json")


def _load(path, name, default=None):
    p = os.path.join(path, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", default=DEFAULT_BUILD)
    ap.add_argument("--shipped", default=DEFAULT_SHIPPED)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="promote even when a book would be genuinely lost")
    args = ap.parse_args()

    for f in BUILD_FILES:
        if not os.path.exists(os.path.join(args.build, f)):
            sys.exit(f"{args.build} has no {f} — is that a build_matrix.py --out-dir?")

    sb = _load(args.shipped, "books.json", [])
    bb = _load(args.build, "books.json", [])
    excluded = set(_load(args.shipped, "excluded.json", []) or [])

    sk = {b["k"]: b for b in sb}
    bk = {b["k"]: b for b in bb}
    # Title -> keys in the build, for the re-keying test.
    btitles: dict[str, list[str]] = {}
    for b in bb:
        btitles.setdefault((b.get("t") or "").strip().lower(), []).append(b["k"])

    cleaned, rekeyed, lost = [], [], []
    for k in sorted(set(sk) - set(bk)):
        if k in excluded:
            cleaned.append(k)
            continue
        hits = btitles.get((sk[k].get("t") or "").strip().lower(), [])
        (rekeyed if hits else lost).append((k, hits))

    gained = sorted(set(bk) - set(sk))
    print(f"shipped {len(sb)} books, build {len(bb)} books\n")
    print(f"  cleaned up (excluded by hand) : {len(cleaned)}")
    print(f"  re-keyed (book stays, key moves): {len(rekeyed)}")
    print(f"  GENUINELY LOST                 : {len(lost)}")
    print(f"  gained                         : {len(gained)}")

    if rekeyed:
        print("\nRE-KEYED — these keep their cells but LOSE every override, lock")
        print("and learned count filed under the old key:")
        for k, hits in rekeyed:
            print(f"    {k}\n        -> {', '.join(hits)}")

    if lost:
        print("\nGENUINELY LOST — in the game today, in no build row, and not")
        print("excluded by hand. Publish a _books/ page so it is re-pinned, or")
        print("exclude it deliberately, then re-run:")
        for k, _ in lost:
            print(f"    {k}   {str(sk[k].get('t'))[:52]!r}")

    if lost and not args.force:
        sys.exit("\nREFUSING to promote. Re-run with --force if this is wanted.")

    if args.dry_run:
        print("\n--dry-run: nothing copied")
        return

    for f in BUILD_FILES:
        shutil.copy(os.path.join(args.build, f), os.path.join(args.shipped, f))
    print(f"\npromoted {len(BUILD_FILES)} file(s) -> {args.shipped}")
    print("Now re-run parity_trace.py and parity-check.js before committing: a "
          "new question list moves question_hash, and every client with an "
          "open tab goes stale.")


if __name__ == "__main__":
    main()
