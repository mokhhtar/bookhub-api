"""
scripts/akinator/harvest_descriptions.py — the text an extractor can read.

WHY. The game already has a question "does it take place at sea?" and it is
NOT ASKED, because Open Library's subject strings flag it on **1.3%** of the
corpus — below the 5% floor, so feature selection drops it. The signal is
real (Moby Dick, Treasure Island, Robinson Crusoe, The Sea-Wolf all carry
it) and the coverage is not: Twenty Thousand Leagues Under the Sea is
missed, Little Women is flagged.

That is the shape of every evocative question the owner asked for — sea,
secret organisations, organised magic. The catalogue knows a little and
records it inconsistently. **Prose is where those facts actually live**, so
this fetches the prose.

WHAT WAS MEASURED BEFORE WRITING THIS:

    first_sentence (already fetched)   41%, median 114 chars  — too thin
    OL works-endpoint description      80%, median 762 chars (~127 words)
    our own published pages           100% of 129, median 713 words

Four books in five have real description text. It is not in the search
results — only the per-work endpoint carries it — so this is one request per
book, about 85 minutes for 5,000 at a polite rate. Monthly, alongside the
other harvests, and resumable like all of them.

Output: data/akinator_descriptions.json — `{work_key: text}`. Its own file,
so a partial run can never damage the corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

# Below this a "description" is a fragment or a publisher's one-liner, and
# an extractor reading it would be guessing from almost nothing.
MIN_CHARS = 120
# Above this we are paying for tokens on a full plot summary. The opening is
# where a description says what kind of story this is.
MAX_CHARS = 4000


def _text_of(value) -> str:
    """OL descriptions arrive as a bare string or as {type, value}."""
    if isinstance(value, dict):
        value = value.get("value")
    return (value or "").strip() if isinstance(value, str) else ""


def fetch_one(work_key: str, timeout: int = 25) -> str:
    url = f"https://openlibrary.org{work_key}.json"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    text = _text_of(data.get("description"))
    if len(text) < MIN_CHARS:
        # Some works carry the blurb under `first_sentence` instead.
        alt = _text_of(data.get("first_sentence"))
        if len(alt) > len(text):
            text = alt
    return text[:MAX_CHARS] if len(text) >= MIN_CHARS else ""


def fetch_google(title: str, author: str) -> str:
    """A description from Google Books, with Open Library as the fallback.

    THE SECOND SOURCE, and it exists because the first one is EXHAUSTED, not
    because it was never run. All 5,000 books were asked of Open Library's
    per-work endpoint; 3,648 had text and 1,292 of the shipped books were
    asked and genuinely had none. Re-running the OL pass on those cannot
    produce anything — 73% is the OL ceiling this project has cited for
    weeks, and this is the measurement behind it.

    `resolve_book` is the resolver every other tool here uses, so this
    inherits its Google Books -> Open Library chain and its author fix
    rather than opening a fourth way of asking about a book.

    Measured yield on a random 14 of the shipped books that OL had nothing
    for: **7 usable** (>=200 chars), and that sample included several Google
    503s, so it is a floor rather than an estimate. 12 Rules for Life, Dead
    Poets Society, The Ones Who Walk Away from Omelas and Grandma's Bag of
    Stories all came back with real text.

    THE 503 TRAP, and it bit this function within two minutes of first
    running. `resolve_book` swallows a provider error and answers
    `found=False` — the SAME value it gives for a book that genuinely has no
    record. So a Google Books 503 was being written down as "asked, nothing
    there", permanently, and *12 Rules for Life* — rank 99, richness 19 —
    was skipped on a transient server error and would never have been asked
    again. Unavailable recorded as absent: failure shape #4, and the reason
    `_catalogue_reachable` exists over in tools/akinator_suggest.py.

    So a miss is only reported as a miss when the catalogue is demonstrably
    up. If it is not, this RAISES, and the caller's failure path leaves the
    book out of the asked-set for the next run to retry.
    """
    sys.path.insert(0, REPO_ROOT)
    from book_data import resolve_book                     # noqa: E402
    record = resolve_book(title, author)
    if record.found:
        return _text_of(record.description)
    if not _catalogue_reachable():
        raise RuntimeError("catalogue unreachable; not recording a miss")
    return ""


def _catalogue_reachable() -> bool:
    """Did we look and find nothing, or could we not look?

    One query that must return something, asked only on the failure path.
    Open Library rather than Google Books on purpose: Google is the source
    that 503s here, and asking a flaky provider whether it is flaky answers
    the wrong question. What is being tested is whether THIS MACHINE can
    reach a catalogue at all.
    """
    import urllib.parse
    try:
        url = ("https://openlibrary.org/search.json?"
               + urllib.parse.urlencode({"q": "dune frank herbert",
                                         "fields": "key", "limit": 1}))
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.load(resp).get("docs"))
    except Exception:                                      # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--source", choices=["openlibrary", "google"],
                    default="openlibrary",
                    help="'google' asks Google Books (falling back to Open "
                         "Library) for the books the openlibrary pass asked "
                         "about and found nothing for. It GAP-FILLS: a book "
                         "that already has text is never re-fetched and never "
                         "overwritten, so a worse second answer cannot "
                         "replace a good first one.")
    ap.add_argument("--from-shipped", default=None,
                    help="a books.json URL or local path — read the book "
                         "list from the SHIPPED artifact instead of the "
                         "local, gitignored corpus. This is what lets a "
                         "GitHub Actions runner, which has no copy of the "
                         "corpus at all, run this incrementally. See "
                         "shipped_docs.py for exactly what it changes (only "
                         "which books are considered — nothing about how "
                         "they are fetched or resumed).")
    args = ap.parse_args()

    if args.from_shipped:
        from shipped_docs import load_shipped_docs
        docs = load_shipped_docs(args.from_shipped)
    else:
        docs = []
        with open(CORPUS_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        docs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    docs = docs[:args.limit]

    out: dict[str, str] = {}
    asked: set[str] = set()
    # Each source keeps its OWN asked-set. Sharing one would mean the Google
    # pass marking a book "asked" and the next Open Library run skipping it —
    # or, worse, the reverse: this pass skipping all 5,000 because the OL run
    # had already asked them, which is exactly the population it exists for.
    suffix = "_asked.json" if args.source == "openlibrary" else "_asked_google.json"
    asked_path = args.out.replace(".json", suffix)
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            out = json.load(fh)
    if os.path.exists(asked_path):
        with open(asked_path, encoding="utf-8") as fh:
            asked = set(json.load(fh))
    # Asked is tracked apart from found: a book with no description would
    # otherwise be re-requested on every run, forever. Same drift that the
    # cover fetcher's resume had to be fixed for.
    if asked:
        print(f"Resuming: {len(asked)} asked, {len(out)} with text.\n")

    todo = [d for d in docs if d.get("key") and d["key"] not in asked]
    # A book that already has usable text needs no further asking, whatever
    # the source — generalizes a check the google gap-fill pass already had
    # on its own.
    #
    # FOUND 2026-08-23, live, on the first --from-shipped run in GitHub
    # Actions: a fresh checkout has no _asked.json at all (it stays
    # gitignored on purpose — see the comment above), so `asked` starts
    # EMPTY every single run there. Before this line applied to the
    # openlibrary source too, that meant every weekly run re-asked all
    # ~4,122 already-described books from scratch — the write-back a few
    # lines down already refuses to overwrite existing text (`not
    # _text_of(out.get(key, ""))`), so nothing was ever actually LOST, only
    # ~30+ minutes wasted per run re-confirming answers already on disk.
    # Locally this never showed up: the asked-log persists on the
    # developer's own disk between manual runs even though git never sees
    # it, so the resume "worked" there by accident.
    todo = [d for d in todo if not _text_of(out.get(d["key"], ""))]
    if args.source == "google":
        print(f"{len(docs)} books, {len(todo)} with NO description to fill.\n")
    else:
        print(f"{len(docs)} books, {len(todo)} still to ask about.\n")

    failures = 0
    for i, doc in enumerate(todo, 1):
        key = doc["key"]
        try:
            if args.source == "google":
                text = fetch_google(doc.get("title") or "",
                                    (doc.get("author_name") or [""])[0])
            else:
                text = fetch_one(key)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures >= 6:
                print("\n! six failures in a row; stopping. Re-run to resume.",
                      file=sys.stderr)
                break
            time.sleep(3 * failures)
            continue

        asked.add(key)
        # `not out.get(key)` is belt and braces on top of the todo filter:
        # a long run could otherwise clobber text a concurrent process wrote.
        if text and not _text_of(out.get(key, "")):
            out[key] = text

        # Checkpoint often. At 200 the first smoke test was killed at ~175
        # items and saved nothing at all — a resumable job that only becomes
        # resumable after three minutes is not resumable.
        if i % 50 == 0 or i == len(todo):
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False)
            with open(asked_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(asked), fh)
            print(f"  {i:>5}/{len(todo)} asked   {len(out)} with text "
                  f"({len(out) * 100 // max(1, len(asked))}%)")
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    with open(asked_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(asked), fh)
    print(f"\n{len(out)} descriptions -> {args.out}")

    # A book skipped by a transient failure is left out of `asked` on purpose,
    # so the next run retries it. That is only true if somebody knows to re-run
    # it: the first pass reported "3,590 descriptions" and said nothing about
    # the 85 books it never managed to ask about, among them Nineteen
    # Eighty-Four. Silence read as completion for a week.
    # A book this pass never MEANT to ask about is not an unasked book.
    #
    # This report was written for the openlibrary pass, where every one of
    # the 5,000 should end up in `asked`, and it exists because a first run
    # once reported "3,590 descriptions" while silently skipping 85 books
    # including Nineteen Eighty-Four. Reused unchanged by the gap-fill pass
    # it inverted itself: `--source google` deliberately skips every book
    # that already HAS text, so the report counted all 4,121 of them as
    # transient failures and told the owner to re-run a twenty-minute job
    # to retry Atomic Habits — which has a 409-character description and was
    # never in question. A warning that fires on success trains people to
    # ignore warnings.
    skipped_on_purpose = ({d["key"] for d in docs
                           if d.get("key") and _text_of(out.get(d["key"], ""))}
                          if args.source == "google" else set())
    unasked = [d for d in docs if d.get("key") and d["key"] not in asked
               and d["key"] not in skipped_on_purpose]
    if unasked:
        print(f"! {len(unasked)} book(s) were never successfully asked — "
              f"transient failures, NOT 'no description exists'.")
        print("  Re-run this script to retry exactly those.")
        for d in unasked[:5]:
            print(f"    {(d.get('title') or '')[:60]}")
    else:
        print(f"all {len(docs)} books asked; "
              f"{len(docs) - len(out)} genuinely have no description text")


if __name__ == "__main__":
    main()
