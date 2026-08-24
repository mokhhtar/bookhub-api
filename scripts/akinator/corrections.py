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

import json
import os

# work key -> {field: corrected value}
CORRECTIONS: dict[str, dict] = {
    # "Harry Potter and the Cursed Child", Jack Thorne / John Tiffany /
    # J.K. Rowling. A stage play; the West End production opened 30 July
    # 2016 and the official playscript was published the same month. OL's
    # 2001 on this work record contradicts its own OL24779727W (2016).
    # Verified 2026-08-10 against openlibrary.org/search.json.
    "/works/OL17360811W": {"first_publish_year": 2016},

    # "Fifty Shades of Grey", E. L. James. Open Library's own row for this
    # title and author carries first_publish_year 2000; the novel was
    # published in 2011. Wikidata's work entity (not an edition —
    # Q3331189 filtered) gives 2011 with the same author. Reached the game
    # through harvest_fandom_years.py's Open Library fallback, which is
    # exact-title matched and still cannot see past a wrong source row.
    # Verified 2026-08-17 against query.wikidata.org (P577 on the work).
    "/fandom/fiftyshadesofgrey": {"first_publish_year": 2011},
}

# NOT CORRECTED, and recorded here so the next audit does not re-open them:
#
#   /fandom/thesummeriturnedpretty — Open Library says 2000 and the book
#     is 2009, but Wikidata returned no work entity under that label with
#     that author, so there is no independent source to name. Rule 2 of
#     the bar above is not met. Left for manual review rather than fixed
#     from memory.
#
#   /fandom/renegadecats — flagged during an audit as "Erin Hunter, 2026,
#     should be Marissa Meyer 2017" and that flag was WRONG. The wiki's
#     own front page reads "Erin Hunter's new Renegades series"; the
#     subdomain is `renegadecats`. The harvest was right and the audit was
#     the error — a reminder that a plausible-looking correction is still
#     a claim, and rule 2 exists to catch exactly this.


# WHERE ADMIN-PAGE CORRECTIONS LAND, and why not straight into CORRECTIONS
# above. That dict is committed Python source with a 3-point bar and a
# named source per entry — a web form should never commit text into a
# .py file via the GitHub API; an admin typo there is a syntax error that
# breaks the next `import corrections` on Render, with no review step.
# `admin_corrections.json` is the same {work_key: {field: value}} shape,
# written by `tools/akinator_admin.py` into the `bookhub` repo at
# `games/data/akinator/admin_corrections.json` — the same location and
# same "one file, both readers" reasoning as `exclusions.py`'s
# `excluded.json`. It is layered ON TOP of the hand-curated dict here,
# never replacing it.
#
# TWO WRITERS, AND THE SECOND ONE SETS DIFFERENT FIELDS.
# `/akinator/admin/correction` is restricted to `first_publish_year` and
# deliberately stays that narrow. `/akinator/admin/authors/link` writes
# `author_name` and `author_key` here, because saying who a book is BY is
# exactly a correction to the source row: `AuthorIndex` groups on those two
# fields, `book_traits()` looks Wikidata up by `author_key`, and
# `author:prolific` counts by it. A synced `/site/` row carries no author
# key at all by construction, so this file is the only place one can be
# attached — and going through the correction mechanism means the next
# build reaches the conclusion ITSELF, rather than depending forever on the
# per-cell clamps `link` also writes for immediate effect.
#
# Order still matters and still holds: `apply_corrections` runs after the
# /site/ and /fandom/ supplements in `build_matrix.load_corpus`, which is
# before `build_books` — so the corrected author reaches AuthorIndex and
# the trait join, not just the printed name.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADMIN_CORRECTIONS_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator", "admin_corrections.json"))


def _load_admin_corrections() -> dict[str, dict]:
    if not os.path.exists(ADMIN_CORRECTIONS_PATH):
        return {}
    try:
        with open(ADMIN_CORRECTIONS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_corrections(docs: list[dict], verbose: bool = False) -> int:
    """Overwrite verified-wrong fields in place. Returns how many applied.

    Effective only at the next full `build_matrix.py` run, same as every
    entry in `CORRECTIONS` today — neither source is consulted by anything
    that runs live on Render. A correction that changes a matrix bit (an
    era question's ladder, say) needs that bit recomputed, which only a
    full rebuild does.
    """
    admin = _load_admin_corrections()
    applied = 0
    for doc in docs:
        fix = {**CORRECTIONS.get(doc.get("key") or "", {}),
              **admin.get(doc.get("key") or "", {})}
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
