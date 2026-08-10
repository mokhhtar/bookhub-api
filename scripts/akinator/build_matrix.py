"""
scripts/akinator/build_matrix.py — corpus in, playable artifacts out.

    python scripts/akinator/build_matrix.py
    python scripts/akinator/build_matrix.py --out-dir ../bookhub/games/data/akinator

WHAT IT PRODUCES. Everything the browser needs to play, with no server and
no API call at play time:

    questions.json   question ids and their wording
    books.json       title, author, year, popularity, richness
    matrix.bin       packed 2-bit answer states, books x questions
    characters.json  book -> character name tokens (endgame questions)
    authors.json     author entities, aliases, and their books
    meta.json        counts, versions, and how to read matrix.bin

WHY 2 BITS AND NOT A PROBABILITY. Each cell is one of three states —
present, absent, unknown — and the probability is *derived* from the state
plus the book's richness, using the same `absence_confidence()` ladder the
Python engine uses. Storing states rather than floats is what keeps 20k
books x ~60 questions at ~300 KB instead of ~5 MB, and it means the client
and this pipeline cannot drift: there is one rule for turning a state into
a probability and both sides read it from `meta.json`.

STATE ENCODING (also written into meta.json so the client never guesses):
    0 = absent   -> P(yes) = absence_confidence(richness)
    1 = present  -> P(yes) = 0.90
    2 = unknown  -> P(yes) = 0.50
    3 = reserved

Four cells per byte, question-major within a book, books in popularity
order — so the client can slice a book's row without an index.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from author_traits import AUTHOR_QUESTIONS, book_traits, load_wikidata  # noqa: E402
from authors import AuthorIndex                                     # noqa: E402
from series import series_for_docs                                  # noqa: E402
from work_traits import (WORK_QUESTIONS, load_protagonists,         # noqa: E402
                         load_works, merge_into)
from characters import extract_characters, usable_token             # noqa: E402
from corpus_filter import SHIPPED_BOOKS, filter_corpus              # noqa: E402
from site_books import supplement as site_supplement                # noqa: E402
from traits import TRAIT_QUESTIONS, apply_labels, load_traits       # noqa: E402
from series import VOLUME_DOMINANCE                                  # noqa: E402
from features import (EXCLUSIVE_GROUPS, FORCE_KEEP,                  # noqa: E402
                      PRESENCE_CONFIDENCE,
                      QUESTION_TEXT, STRUCTURAL_QUESTIONS,
                      UNKNOWN_CONFIDENCE, absence_confidence, extract)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
COVERS_PATH = os.path.join(REPO_ROOT, "data", "akinator_covers.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "data", "akinator_build")

# Same band as the Phase 0 gate: rarer than this splits nothing, commoner
# tells nothing.
MIN_FREQ = 0.05
MAX_FREQ = 0.60

# SHIPPED_BOOKS is the DEFAULT for --limit because the default should build
# the thing that ships. It used to be 0, meaning the whole 19,890-book
# corpus, and a plain `python build_matrix.py` therefore produced 45
# questions instead of 58 and a 237 KB first paint against a 78 KB budget —
# a different game, silently, with every column renumbered. Pass --limit 0
# for analysis. Defined in corpus_filter so simulate.py shares it.
#
# The stated first-paint budget: the matrix, the wording and the decoding
# rule, and nothing else. Checked at the end of a build rather than trusted,
# because exceeding it is invisible in every other output.
FIRST_PAINT_BUDGET_KB = 100

# A character name is useful precisely because it is rare. Only the ceiling
# matters, and it scales with corpus size rather than being a fixed count.
MAX_CHAR_SHARE = 0.02

# An author question is an endgame question, so the same logic applies —
# but an author with a single book in the corpus can only ever be asked
# about that one book, which the reveal already handles.
MIN_AUTHOR_BOOKS = 2

STATE_ABSENT, STATE_PRESENT, STATE_UNKNOWN = 0, 1, 2

# Set from --no-traits in main(); read by build_books.
NO_TRAITS = False


def load_corpus(path: str) -> list[dict]:
    docs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    # The corpus is written in popularity order, but a resumed run can
    # interleave pages. Sort so index really does mean rank.
    docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    # Filter before the caller truncates, so dropping a workbook promotes a
    # real book into the shipped corpus instead of leaving a hole.
    docs = filter_corpus(docs)
    # Books we publish are pinned in regardless of Open Library rank —
    # a player must never be told we don't know a book we host.
    return site_supplement(docs)


def load_covers(path: str = COVERS_PATH) -> dict[str, int]:
    """work key -> Open Library cover id, from fetch_covers.py.

    A separate file rather than a corpus field: `cover_i` was not in the
    original fetch, and a partial cover run must never be able to damage
    the corpus itself.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    return saved.get("covers", saved)


def build_books(docs: list[dict]) -> tuple[list[dict], AuthorIndex]:
    n = len(docs)
    authors = AuthorIndex()
    books = []

    # Phase 2 author facts. Missing file is fine — the game gets fewer
    # questions, never wrong ones.
    wd = load_wikidata()
    works = load_works()
    protagonists = load_protagonists()
    extracted = {} if NO_TRAITS else load_traits()
    if extracted:
        print(f"Extracted traits: {len(extracted)} books labelled by the model")
    book_counts: dict[str, int] = {}
    for doc in docs:
        for key in doc.get("author_key") or []:
            book_counts[key] = book_counts.get(key, 0) + 1
    if wd:
        print(f"Author facts: {len(wd)} authors from Wikidata")

    for rank, doc in enumerate(docs):
        book = extract(doc, rank, n)
        names, tokens = extract_characters(book.pop("persons"))
        book["char_names"] = sorted(names)
        book["char_tokens"] = sorted(t for t in tokens if usable_token(t))

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
                known_false.add(key)
        book["present"] = sorted(present)
        book["unknown"] = sorted(unknown)
        book["known_false"] = sorted(known_false)
        merge_into(book, doc, works, protagonists)

        # Asserted -> present, everything else -> unknown, never absent.
        # Shared with simulate.py so the artifact and the measurement of it
        # cannot drift apart; see traits.apply_labels.
        apply_labels(book, doc.get("key") or "", extracted)

        keys = doc.get("author_key") or []
        ids = []
        for i, name in enumerate(doc.get("author_name") or []):
            ol_key = keys[i] if i < len(keys) else None
            aid = authors.add(name, ol_key)
            if aid:
                ids.append(aid)
                authors.note_book(aid)
        book["author_ids"] = ids
        books.append(book)
    return books, authors


def select_features(books: list[dict]) -> list[str]:
    n = len(books)
    counts: dict[str, int] = {}
    for b in books:
        for f in b["present"]:
            counts[f] = counts.get(f, 0) + 1
    keys = set(counts) | {k for b in books for k in b["unknown"]}
    kept = []
    for k in sorted(keys):
        freq = counts.get(k, 0) / n
        if MIN_FREQ <= freq <= MAX_FREQ or (k in FORCE_KEEP and freq > 0):
            kept.append(k)
    return kept


def pack_matrix(books: list[dict], questions: list[str]) -> bytes:
    """Two bits per cell, four cells per byte, row-major by book."""
    per_row = len(questions)
    out = bytearray()
    for book in books:
        present = set(book["present"])
        unknown = set(book["unknown"])
        acc, filled = 0, 0
        for q in questions:
            if q in present:
                state = STATE_PRESENT
            elif q in unknown:
                state = STATE_UNKNOWN
            else:
                state = STATE_ABSENT
            acc |= state << (filled * 2)
            filled += 1
            if filled == 4:
                out.append(acc)
                acc, filled = 0, 0
        if filled:
            out.append(acc)
    assert len(out) == len(books) * ((per_row + 3) // 4)
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default=CORPUS_PATH)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--no-traits", action="store_true",
                    help="build without the model-extracted trait columns. "
                         "Measured over 12 seeds and 2,400 paired games they "
                         "are worth +0.38 points at p=0.37 — no effect — "
                         "while costing 8 questions of first paint against a "
                         "78 KB budget. Revisit when extraction is complete.")
    ap.add_argument("--limit", type=int, default=SHIPPED_BOOKS,
                    help=f"how many books to build ({SHIPPED_BOOKS} is what "
                         f"ships; 0 = the whole corpus, for analysis only)")
    args = ap.parse_args()

    global NO_TRAITS
    NO_TRAITS = args.no_traits

    docs = load_corpus(args.corpus)
    if args.limit:
        docs = docs[:args.limit]
    print(f"Corpus: {len(docs)} books")

    covers = load_covers()
    books, authors = build_books(docs)
    if covers:
        have = sum(1 for b in books if covers.get(b["key"]))
        print(f"Covers: {have}/{len(books)} books have one "
              f"({have * 100 // max(1, len(books))}%)")
    questions = select_features(books)
    print(f"Questions kept: {len(questions)}")

    # -- character questions -------------------------------------------
    char_counts: dict[str, int] = {}
    for b in books:
        for t in b["char_tokens"]:
            char_counts[t] = char_counts.get(t, 0) + 1
    ceiling = max(1, int(len(books) * MAX_CHAR_SHARE))
    char_tokens = sorted(t for t, c in char_counts.items() if c <= ceiling)
    char_set = set(char_tokens)
    with_cast = sum(1 for b in books if b["char_tokens"])
    print(f"Character tokens: {len(char_tokens)} "
          f"({with_cast}/{len(books)} books have a cast, "
          f"{with_cast * 100 // max(1, len(books))}%)")

    # -- author questions ----------------------------------------------
    author_rows = [a for a in authors.export() if a["book_count"] >= MIN_AUTHOR_BOOKS]
    print(f"Authors: {len(authors.by_id)} total, "
          f"{len(author_rows)} with {MIN_AUTHOR_BOOKS}+ books (askable)")

    os.makedirs(args.out_dir, exist_ok=True)

    def write(name: str, payload) -> int:
        path = os.path.join(args.out_dir, name)
        if isinstance(payload, bytes):
            with open(path, "wb") as fh:
                fh.write(payload)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        return os.path.getsize(path)

    sizes = {}
    # PER-QUESTION BASE RATE, and it is a correction rather than a
    # refinement. A flat p_unknown of 0.5 is only neutral for a question
    # that is a coin flip; "is the author British?" is true of 14% of the
    # corpus, so scoring an unmatched author at 0.5 asserts they are three
    # times more likely to be British than anyone else. That is what let
    # "is the author American? yes" still be followed by "is the author
    # from Asia, Africa or Latin America?" even after the known-false state
    # went in: the contradiction was gone for matched authors (0.03) and
    # alive for the 31% unmatched ones (0.50), which averaged to 0.17 and
    # cleared the engine's filter.
    #
    # Third appearance of this exact error — phase 0 found it on character
    # names, and it was written up as "unknown must mean the base rate, not
    # a coin flip". Applying it per question rather than per feature-family
    # is what stops a fourth.
    present_counts: dict[str, int] = {}
    for b in books:
        for f in b["present"]:
            present_counts[f] = present_counts.get(f, 0) + 1
    sizes["questions.json"] = write("questions.json", [
        # TRAIT_QUESTIONS belongs in this chain: without it a trait that
        # clears the frequency floor ships with `q` as its own wording, and
        # the player is asked "t:sea". simulate.py's lookup already had it,
        # so the simulation printed the real question while the artifact
        # would have carried the key — a divergence no offline run could
        # show, because only the builder writes questions.json.
        {"id": q,
         "text": (QUESTION_TEXT.get(q) or STRUCTURAL_QUESTIONS.get(q)
                  or AUTHOR_QUESTIONS.get(q) or WORK_QUESTIONS.get(q)
                  or TRAIT_QUESTIONS.get(q, q)),
         "base": round(present_counts.get(q, 0) / max(1, len(books)), 4)}
        for q in questions
    ])
    sizes["books.json"] = write("books.json", [
        {
            "k": b["key"],
            "t": b["title"],
            "a": b["author"],
            "y": b["year"],
            "p": b["popularity"],
            "r": b["richness"],
            "w": b["wikidata"],
            # Open Library cover id. The page builds the image URL from it;
            # storing the id rather than the URL keeps books.json small and
            # lets the size suffix be chosen at display time.
            "c": covers.get(b["key"]),
        }
        for b in books
    ])
    sizes["matrix.bin"] = write("matrix.bin", pack_matrix(books, questions))
    # Index lookup must be a dict: `list.index()` inside this loop is
    # O(tokens x mentions), which at 20k books is ~100k tokens scanned
    # tens of thousands of times.
    token_ix = {t: i for i, t in enumerate(char_tokens)}
    sizes["characters.json"] = write("characters.json", {
        "tokens": char_tokens,
        "books": [[token_ix[t] for t in b["char_tokens"] if t in char_set]
                  for b in books],
    })
    sizes["authors.json"] = write("authors.json", {
        "authors": [{"id": a["id"], "n": a["name"], "s": a["surname"],
                     "c": a["book_count"]} for a in author_rows],
        "books": [b["author_ids"] for b in books],
    })
    # Series grouping, so the page can name "Harry Potter" instead of
    # picking one of its twelve volumes. Index-aligned with books.json.
    series_of, series_names = series_for_docs(docs)
    grouped = sum(1 for s in series_of if s)
    print(f"Series: {grouped} books in {len(series_names)} named groups")
    sizes["series.json"] = write("series.json", {
        "names": series_names,
        "books": series_of,
        "volume_dominance": VOLUME_DOMINANCE,
    })

    # Fingerprint of the question list, in order. tools/akinator_sync.py
    # refuses to append a row unless this still matches — the matrix is
    # column-positional, so a row packed against a different question list
    # is silent corruption that leaves the file size correct.
    import hashlib as _hashlib
    q_hash = _hashlib.sha256("|".join(questions).encode("utf-8")).hexdigest()[:16]

    sizes["meta.json"] = write("meta.json", {
        "version": 1,
        "question_hash": q_hash,
        "books": len(books),
        "questions": len(questions),
        "bytes_per_row": (len(questions) + 3) // 4,
        "states": {"absent": STATE_ABSENT, "present": STATE_PRESENT,
                   "unknown": STATE_UNKNOWN},
        # The client derives probabilities from these — one rule, both sides.
        "p_present": PRESENCE_CONFIDENCE,
        "p_unknown": UNKNOWN_CONFIDENCE,
        # Published rather than duplicated in the page: one definition of
        # what contradicts what, read by both engines.
        "exclusive_groups": [[q for q in g if q in set(questions)]
                             for g in EXCLUSIVE_GROUPS],
        "p_absent_by_richness": [
            {"max_richness": 2, "p": absence_confidence(2)},
            {"max_richness": 6, "p": absence_confidence(6)},
            {"max_richness": 15, "p": absence_confidence(15)},
            {"max_richness": None, "p": absence_confidence(999)},
        ],
    })

    # What the browser pays before it can ask question one, versus what it
    # can fetch in the background while the player reads that question.
    # Nothing but the matrix, the wording and the decoding rule is needed to
    # run inference; titles, cast and authors are only wanted once there is
    # something to guess or reveal.
    CORE = {"questions.json", "matrix.bin", "meta.json"}
    # series.json is deferred: needed only once there is something to guess.

    print("\nArtifacts:")
    total = core_total = 0
    for name, size in sizes.items():
        total += size
        tag = "core" if name in CORE else "deferred"
        if name in CORE:
            core_total += size
        print(f"  {name:<20} {size / 1024:8.1f} KB   {tag}")
    print(f"  {'TOTAL':<20} {total / 1024:8.1f} KB  ({total / 1024 / 1024:.2f} MB)")
    print(f"  {'first paint (core)':<20} {core_total / 1024:8.1f} KB")
    if core_total / 1024 > FIRST_PAINT_BUDGET_KB:
        print(f"\n! FIRST PAINT IS OVER BUDGET: {core_total / 1024:.1f} KB "
              f"against {FIRST_PAINT_BUDGET_KB} KB.", file=sys.stderr)
        print(f"  {len(books)} books x {len(questions)} questions. If that is "
              f"more than {SHIPPED_BOOKS} books, --limit was set to 0 or too "
              f"high; this build should not be published.", file=sys.stderr)

    try:
        import gzip
        gz = sum(len(gzip.compress(open(os.path.join(args.out_dir, n), "rb").read(), 6))
                 for n in sizes)
        print(f"  {'gzipped (as served)':<20} {gz / 1024:8.1f} KB  "
              f"({gz / 1024 / 1024:.2f} MB)")
    except Exception:  # noqa: BLE001 — reporting only
        pass

    print(f"\n  -> {args.out_dir}")


if __name__ == "__main__":
    main()
