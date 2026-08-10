"""
scripts/akinator/corrections.py — hand-verified fixes to source metadata.

WHY THIS EXISTS. Open Library is the spine of this corpus and it is
sometimes wrong. Not missing — wrong, which is worse, because a missing
field becomes `unknown` and costs nothing while a wrong one becomes a
confident claim that pushes the right book DOWN when the player answers
correctly.

The case that opened this file: **Harry Potter and the Cursed Child**
(`/works/OL17360811W`), rank 214 of 5,000. Open Library records
`first_publish_year: 2001`. The play premiered in the West End in 2016 and
the playscript was published in 2016 — OL's own record for another edition
of the same work (`OL24779727W`) says 2016. With 2001 the game answers "no"
to *"Was it published in the last 10 years?"*, so a player who knows the
book and answers "yes" — correctly — is punished for it.

THE BAR FOR ADDING AN ENTRY, because a hand-maintained list is a liability
if it grows carelessly:

  1. The source value must be demonstrably wrong, not merely suspicious.
  2. The correct value must be verifiable from an independent source, and
     the source must be named in the comment.
  3. The field must be one the game actually asks about. Fixing a field no
     question reads is maintenance cost for nothing.

This is deliberately NOT a general editing surface. It is a short list of
errors somebody checked. The scalable answer to source errors is the phase
5 learning loop, where a player who knows better moves a cell through
accumulated evidence rather than through this file.

Applied inside `corpus_filter.filter_corpus`, which is the one function
both `build_matrix.py` and `simulate.py` pass their documents through — so
the artifact and the measurement of it cannot disagree about a correction.
"""
from __future__ import annotations

# work key -> {field: corrected value}
CORRECTIONS: dict[str, dict] = {
    # "Harry Potter and the Cursed Child", Jack Thorne / John Tiffany /
    # J.K. Rowling. A stage play; the West End production opened 30 July
    # 2016 and the official playscript was published the same month. OL's
    # 2001 on this work record contradicts its own OL24779727W (2016).
    # Verified 2026-08-10 against openlibrary.org/search.json.
    "/works/OL17360811W": {"first_publish_year": 2016},
}


def apply_corrections(docs: list[dict], verbose: bool = False) -> int:
    """Overwrite verified-wrong fields in place. Returns how many applied."""
    applied = 0
    for doc in docs:
        fix = CORRECTIONS.get(doc.get("key") or "")
        if not fix:
            continue
        for field, value in fix.items():
            if doc.get(field) != value:
                if verbose:
                    print(f"    corrected {doc.get('title', '')[:44]}: "
                          f"{field} {doc.get(field)!r} -> {value!r}")
                doc[field] = value
                applied += 1
    return applied
