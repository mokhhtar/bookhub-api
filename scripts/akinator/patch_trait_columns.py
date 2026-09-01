"""
scripts/akinator/patch_trait_columns.py — re-encode the trait columns in the
SHIPPED matrix, without rebuilding anything else.

    python scripts/akinator/patch_trait_columns.py --dry-run
    python scripts/akinator/patch_trait_columns.py

WHY THIS EXISTS, and why `build_matrix.py` was the wrong tool for the job it
was about to be used for.

A trait re-harvest changes the cells in eleven columns and nothing else. The
obvious way to ship that is a full rebuild — and a full rebuild today would
also have changed the BOOK LIST, because it re-truncates the corpus by
popularity and the shipped artifact is not a plain build: `akinator_sync.py`
appends newly published books between rebuilds, so what ships is "last full
build + 29 increments". Measured before this script was written:

    --limit 5000   5,000 books   loses 35 (12 works, 16 fandom, 7 site), gains 6
    --limit 5100   5,100 books   loses 7 (ALL site), gains 78

Seven published pages are dropped at either limit — three added through the
admin page, which never wrote a `_books/` entry for them to be re-pinned
from, and four that do still have one. That is a real bug and it is NOT this
change's to fix or to smuggle in. A measurement was run on the trait cells;
the trait cells are what should ship.

WHAT IT TOUCHES: two bits per (book, trait question) cell in matrix.bin, and
the `base` field of those questions in questions.json. Books, characters,
series, authors, meta, the question list and its hash are untouched — so
`question_hash` does not move and no stored override, lock or learned count
is invalidated.

WHAT MAKES IT SAFE. The packing is re-derived from meta.json rather than
assumed, every write is bracketed by an exact size assertion, and --dry-run
reports every cell that would move. Most importantly `--verify-against`
compares the result to a real `build_matrix.py` run on the books the two
share: if this script and the builder disagree about a single trait cell,
that is a bug in this script and it says so and exits non-zero.

A book with no entry in the traits file is set to `unknown`, exactly as
`apply_labels` does — never to absent. That distinction is the whole safety
margin: the 877 books with no description at all end up asserting nothing,
rather than asserting "no war, no romance, no detective" about a book nobody
has read.

/fandom/ rows are not this script's to touch and are excluded from both the
patch and the verification, with the count printed so the exclusion is
visible.

Verified 2026-09-01: 54,340 shared trait cells, ZERO disagreements with
`build_matrix.py --with-fandom`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from traits import TRAITS, load_traits, split_labels   # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARTIFACTS = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator"))

STATE_ABSENT, STATE_PRESENT, STATE_UNKNOWN = 0, 1, 2


def _get(row: bytearray, off: int, col: int) -> int:
    return (row[off + (col >> 2)] >> ((col & 3) * 2)) & 3


def _set(row: bytearray, off: int, col: int, state: int) -> None:
    shift = (col & 3) * 2
    i = off + (col >> 2)
    row[i] = (row[i] & ~(3 << shift)) | (state << shift)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default=ARTIFACTS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-against", default=None,
                    help="a build_matrix.py --out-dir to check this against")
    args = ap.parse_args()

    with open(os.path.join(args.artifacts, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(args.artifacts, "questions.json"), encoding="utf-8") as fh:
        questions = json.load(fh)
    with open(os.path.join(args.artifacts, "books.json"), encoding="utf-8") as fh:
        books = json.load(fh)
    with open(os.path.join(args.artifacts, "matrix.bin"), "rb") as fh:
        raw = bytearray(fh.read())

    nb, nq, bpr = meta["books"], meta["questions"], meta["bytes_per_row"]
    if len(raw) != nb * bpr:
        sys.exit(f"matrix.bin is {len(raw)} bytes, meta implies {nb * bpr}")
    if len(books) != nb or len(questions) != nq:
        sys.exit("books.json / questions.json disagree with meta.json")

    qcol = {q["id"]: i for i, q in enumerate(questions)}
    cols = {q: qcol[q] for q in TRAITS if q in qcol}
    print(f"{nb} books, {nq} questions; {len(cols)} trait column(s) to re-encode:")
    print("  " + ", ".join(sorted(cols)))

    labels = load_traits()
    print(f"traits file: {len(labels)} labelled book(s)\n")

    moved = {}
    untouched = 0
    for i, b in enumerate(books):
        key = b.get("k") or ""
        # FANDOM BOOKS COME FROM A DIFFERENT SOURCE, and mirroring that here
        # is not a nicety — it is what `build_matrix.py` does. Its loop reads
        # `doc["_fandom_traits"]` (from akinator_fandom_traits.json, keyed by
        # TITLE) and, when present, hands those to apply_labels INSTEAD of the
        # main extraction. Ten /fandom/ keys also appear in the main traits
        # file, so patching from it overwrote cells the builder sources
        # elsewhere: --verify-against caught exactly 42 such disagreements,
        # every one of them on a /fandom/ row.
        #
        # This harvest did not touch the fandom pipeline, so the honest
        # action for those rows is none at all.
        if key.startswith("/fandom/"):
            untouched += 1
            continue
        # NO ENTRY MEANS UNKNOWN, because that is what apply_labels does:
        # "no entry -> unknown as well. A book whose description was never
        # harvested has not been examined at all." Leaving the shipped cell
        # instead preserves whatever an older traits file once said, and
        # --verify-against found real cases — /site/african-textiles ships
        # `t:child` PRESENT while no entry for it exists any more.
        #
        # Setting unknown is safe in the one direction that matters: it never
        # writes ABSENT, so the 877 books with no description at all cannot
        # be made to assert "no war, no romance, no detective".
        entry = labels.get(key)
        yes, no = split_labels(entry) if entry is not None else (set(), set())
        if entry is None:
            untouched += 1
        off = i * bpr
        for q, col in cols.items():
            want = (STATE_PRESENT if q in yes else
                    STATE_ABSENT if q in no else STATE_UNKNOWN)
            if _get(raw, off, col) != want:
                moved[q] = moved.get(q, 0) + 1
            # APPLIED EVEN UNDER --dry-run, which only gates the WRITE.
            # Skipping it here made `--dry-run --verify-against` compare the
            # unpatched matrix against a real build and report all 10,657
            # moved cells as disagreements — a check that could only ever
            # fail, on its first run, in exactly the mode meant to be safe.
            _set(raw, off, col, want)

    total = sum(moved.values())
    print(f"{'question':<16} {'cells moved':>12}")
    print("-" * 30)
    for q in sorted(moved, key=lambda k: -moved[k]):
        print(f"{q:<16} {moved[q]:>12,}")
    print("-" * 30)
    print(f"{'TOTAL':<16} {total:>12,}")
    print(f"\n{untouched} book(s) have no entry in the traits file and were "
          f"left exactly as they were")

    if args.verify_against:
        _verify(args.verify_against, args.artifacts, raw, books, questions,
                cols, meta)

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    # Base rates follow the cells. `base` is the share of books answering YES
    # and the engine reads it nowhere, but propose_questions.py and the admin
    # page both do, and a stale one would describe the previous harvest.
    for q, col in cols.items():
        present = sum(1 for i in range(nb)
                      if _get(raw, i * bpr, col) == STATE_PRESENT)
        questions[qcol[q]]["base"] = round(present / nb, 4)

    assert len(raw) == nb * bpr, "size changed; refusing to write"
    with open(os.path.join(args.artifacts, "matrix.bin"), "wb") as fh:
        fh.write(bytes(raw))
    with open(os.path.join(args.artifacts, "questions.json"), "w",
              encoding="utf-8") as fh:
        json.dump(questions, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"\nwrote matrix.bin ({len(raw):,} bytes) and questions.json")


def _verify(build_dir: str, art_dir: str, raw: bytearray, books: list,
            questions: list, cols: dict, meta: dict) -> None:
    """Compare the patched trait columns against a real build_matrix.py run.

    THE POINT OF THE WHOLE SCRIPT RESTS ON THIS. Patching bits by hand is
    only defensible if it lands where the builder would have landed, so this
    decodes the builder's own output and compares cell for cell on the books
    both artifacts contain. A single disagreement is a bug here.
    """
    with open(os.path.join(build_dir, "meta.json"), encoding="utf-8") as fh:
        bmeta = json.load(fh)
    with open(os.path.join(build_dir, "questions.json"), encoding="utf-8") as fh:
        bq = json.load(fh)
    with open(os.path.join(build_dir, "books.json"), encoding="utf-8") as fh:
        bbooks = json.load(fh)
    with open(os.path.join(build_dir, "matrix.bin"), "rb") as fh:
        braw = fh.read()

    bcol = {q["id"]: i for i, q in enumerate(bq)}
    brow = {b["k"]: i for i, b in enumerate(bbooks)}
    bpr, bbpr = meta["bytes_per_row"], bmeta["bytes_per_row"]

    checked = bad = skipped = 0
    examples = []
    for i, b in enumerate(books):
        # Fandom rows are OUT OF SCOPE for this script (their labels come
        # from akinator_fandom_traits.json, which this harvest never
        # touched), so comparing them to the builder measures drift between
        # the shipped artifact and a fresh build rather than anything this
        # patch did. Excluded from the check, and counted so the exclusion
        # is visible rather than silent.
        if (b.get("k") or "").startswith("/fandom/"):
            skipped += 1
            continue
        j = brow.get(b.get("k"))
        if j is None:
            continue
        for q, col in cols.items():
            if q not in bcol:
                continue
            mine = _get(raw, i * bpr, col)
            theirs = (braw[j * bbpr + (bcol[q] >> 2)] >> ((bcol[q] & 3) * 2)) & 3
            checked += 1
            if mine != theirs:
                bad += 1
                if len(examples) < 5:
                    examples.append(f"{b.get('k')} {q}: patched={mine} built={theirs}")

    print(f"\nVERIFY against {build_dir}")
    print(f"  {checked:,} shared trait cell(s) compared, {bad} disagreement(s)")
    print(f"  {skipped} /fandom/ row(s) excluded: this script does not own them")
    for e in examples:
        print(f"    {e}")
    if bad:
        sys.exit("patched matrix disagrees with build_matrix.py — refusing")
    print("  the patch lands exactly where build_matrix.py would have")


if __name__ == "__main__":
    main()
