"""
scripts/verify_gtb_pool.py — the smoke test that makes tools/gtb_pool.py
trustworthy. Run it before committing any pool change:

    python scripts/verify_gtb_pool.py            # verify every entry
    python scripts/verify_gtb_pool.py --gid 2489 # just one
    python scripts/verify_gtb_pool.py --no-ol    # skip the Open Library check

It talks to the REAL Project Gutenberg (no mocks — this repo has repeatedly
caught real bugs that mocked tests would have missed) and asserts, per entry:

  HARD (a failure means the entry must not ship)
    · the text actually downloads from some mirror
    · the Gutenberg "Title:"/"Author:" header matches what the pool pins,
      so an upstream renumbering can never silently reground a puzzle in
      the wrong book
    · every listed character name appears VERBATIM in the text
    · setting_anchor appears verbatim in the text
    · the text is long enough to source quotes from
    · canonical_id and title are unique across the pool

  SOFT (printed for the human reviewer, never auto-resolved)
    · first_published vs Open Library's first_publish_year. Open Library
      indexes reprints as works and is routinely wrong by decades, so a
      disagreement is a prompt to check by hand — NOT a reason to overwrite
      the pool. If a year can't be established by a human, drop the year
      clue for that book rather than guessing it.
    · the Gutenberg "Translator:" line vs the pool's `translator`

Downloaded texts are cached under scratch/ (gitignored) so re-runs and the
puzzle generator don't re-download ~40MB every time.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.gtb_pool import GTB_POOL, PoolEntry  # noqa: E402

# Same mirrors as tools/reader.py, but this script deliberately does NOT go
# through reader.get_full_text_pages: that path stores into Redis and enforces
# a 3MB cap sized for the Upstash quota. Curation runs locally, has no quota,
# and must not refuse Les Misérables for being long.
MIRRORS = [
    "https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
    "https://www.gutenberg.org/files/{gid}/{gid}-0.txt",
    "https://gutenberg.pglaf.org/cache/epub/{gid}/pg{gid}.txt",
    "https://aleph.pglaf.org/cache/epub/{gid}/pg{gid}.txt",
]
UA = {"User-Agent": "Litheca/1.0 (mokhhtar@github.com)"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scratch", "gtb_text_cache")
MIN_TEXT_CHARS = 40_000   # below this there isn't enough prose to pick 6 clues from


def fetch_raw(gid: int) -> str | None:
    """Full raw text (Gutenberg header included), cached on disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"pg{gid}.txt")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        with open(path, encoding="utf-8") as f:
            return f.read()

    timeout = httpx.Timeout(connect=8.0, read=90.0, write=10.0, pool=8.0)
    for pattern in MIRRORS:
        url = pattern.format(gid=gid)
        for attempt in (1, 2):
            try:
                r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
                if r.status_code != 200:
                    print(f"    mirror {url} -> {r.status_code}")
                    break
                text = r.text
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                return text
            except Exception as e:
                print(f"    mirror {url} failed (attempt {attempt}): {e}")
    return None


def parse_header(raw: str) -> dict[str, str]:
    """The metadata block Gutenberg puts above the *** START *** marker."""
    start = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG", raw, re.I)
    head = raw[:start.start()] if start else raw[:4000]
    fields: dict[str, str] = {}
    for key in ("Title", "Author", "Translator", "Language", "Release date"):
        m = re.search(rf"^{key}:\s*(.+?)\s*$", head, re.I | re.M)
        if m:
            fields[key.lower()] = m.group(1).strip()
    return fields


def strip_boilerplate(text: str) -> str:
    """Copy of tools/reader.py's marker logic — kept here so curation never
    imports the runtime module (and its Redis client) just to cut a header."""
    m = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
                  text, re.I | re.S)
    if m:
        text = text[m.end():]
    m = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", text, re.I)
    if m:
        text = text[:m.start()]
    return text.strip()


def loose(s: str) -> str:
    """Comparison form: lowercase, accents kept, punctuation flattened."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def openlibrary_year(entry: PoolEntry) -> int | None:
    try:
        r = httpx.get(
            "https://openlibrary.org/search.json",
            params={"title": entry.title, "author": entry.author,
                    "fields": "title,author_name,first_publish_year", "limit": 1},
            headers=UA, timeout=20.0,
        )
        if r.status_code != 200:
            return None
        docs = r.json().get("docs") or []
        return docs[0].get("first_publish_year") if docs else None
    except Exception:
        return None


def verify(entry: PoolEntry, check_ol: bool) -> tuple[list[str], list[str], int | None]:
    """→ (hard failures, soft notes, Open Library's year when it disagrees)."""
    hard: list[str] = []
    soft: list[str] = []
    ol_disagrees: int | None = None

    raw = fetch_raw(entry.gutenberg_id)
    if not raw:
        return ([f"could not download text from any mirror (gid {entry.gutenberg_id})"],
                soft, None)

    header = parse_header(raw)
    body = strip_boilerplate(raw)
    body_l = body.lower()

    # ── identity: is this really the book the pool claims? ──
    got_title = header.get("title", "")
    if not got_title:
        # Older Gutenberg files (pg521, pg45, pg910 …) open straight at the
        # *** START *** marker with no metadata block at all. Identity is still
        # checkable — just against the title page that begins the body instead
        # of a header that isn't there. Weaker than an exact pin, so it is
        # reported; it is NOT a licence to skip verification.
        front = loose(body[:2500])
        missing = [w for w in loose(entry.title).split() if len(w) >= 4 and w not in front]
        if missing or loose(entry.author.split()[-1]) not in front:
            hard.append(f"no metadata header AND the title page doesn't name "
                        f'"{entry.title} / {entry.author}" (missing: {missing or "author"})')
        else:
            soft.append("no metadata header — identity verified from the title page instead")
    elif loose(got_title) != loose(entry.gutenberg_title):
        hard.append(f'Gutenberg title is "{got_title}", pool pins "{entry.gutenberg_title}"')

    got_author = header.get("author", "")
    if not got_author:
        pass  # headerless file — already handled by the title-page check above
    elif entry.gutenberg_author:
        # Pinned where the transliteration differs from the name we display.
        if loose(got_author) != loose(entry.gutenberg_author):
            hard.append(f'Gutenberg author is "{got_author}", pool pins '
                        f'"{entry.gutenberg_author}"')
    else:
        # Gutenberg writes "Herman Melville" but also "Doyle, Arthur Conan"
        # variants across older files — compare on surname presence.
        surname = entry.author.split()[-1]
        if loose(surname) not in loose(got_author):
            hard.append(f'Gutenberg author is "{got_author}", pool says "{entry.author}"')

    # ── enough prose to build clues from ──
    if len(body) < MIN_TEXT_CHARS:
        hard.append(f"only {len(body):,} chars of text after stripping boilerplate")

    # ── every clue-bearing name must be IN the book ──
    for name in entry.characters:
        # "the White Rabbit" is how we display it; the text says "White Rabbit".
        needle = re.sub(r"^(the|my)\s+", "", name.lower())
        if needle not in body_l:
            hard.append(f'character "{name}" never appears verbatim in the text')

    if entry.setting_anchor.lower() not in body_l:
        hard.append(f'setting_anchor "{entry.setting_anchor}" never appears in the text')

    # ── the character clue must not give the answer away ──
    # characters[0] is what the clue names. If it shares a word with ANY pool
    # title it either hands over its own answer ("Alice" → Alice's Adventures
    # in Wonderland) or points at the wrong book ("Tom Sawyer" inside Huck
    # Finn). Both are disqualifying, and both are catchable mechanically.
    clue_name = re.sub(r"^(the|my)\s+", "", entry.characters[0].lower())
    clue_words = {w for w in re.split(r"[^a-z0-9]+", clue_name)
                  if len(w) >= 3 and w not in {"mr", "mrs", "dr", "aunt", "lord", "lady", "miss"}}
    for other in GTB_POOL:
        title_words = set(re.split(r"[^a-z0-9]+", other.title.lower()))
        clash = clue_words & title_words
        if clash:
            whose = "its own title" if other is entry else f'the pool title "{other.title}"'
            hard.append(f'character clue "{entry.characters[0]}" shares {sorted(clash)} '
                        f"with {whose}")

    # ── soft: second opinions a human resolves ──
    got_translator = header.get("translator")
    if got_translator and not entry.translator:
        soft.append(f'Gutenberg lists Translator: "{got_translator}" — pool has none')
    elif entry.translator and got_translator and loose(entry.translator) not in loose(got_translator):
        soft.append(f'translator differs: Gutenberg "{got_translator}" vs pool "{entry.translator}"')

    if check_ol:
        ol = openlibrary_year(entry)
        if ol and ol != entry.first_published:
            ol_disagrees = ol
            soft.append(f"first_published {entry.first_published} vs Open Library {ol} "
                        f"(OL indexes reprints — verify by hand, do not auto-adopt)")

    return hard, soft, ol_disagrees


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gid", type=int, help="verify a single Gutenberg id")
    ap.add_argument("--no-ol", action="store_true", help="skip the Open Library year check")
    args = ap.parse_args()

    entries = [e for e in GTB_POOL if not args.gid or e.gutenberg_id == args.gid]
    if not entries:
        print(f"No pool entry with gid {args.gid}")
        return 2

    print(f"Verifying {len(entries)} pool entr{'y' if len(entries) == 1 else 'ies'} "
          f"against real Project Gutenberg text\n")

    # Pool-level invariants first — cheap, and a duplicate id would corrupt
    # both the answer hashing and the autocomplete list.
    failed = 0
    ids = [e.canonical_id for e in GTB_POOL]
    titles = [e.title.lower() for e in GTB_POOL]
    gids = [e.gutenberg_id for e in GTB_POOL]
    for label, values in (("canonical_id", ids), ("title", titles), ("gutenberg_id", gids)):
        dupes = {v for v in values if values.count(v) > 1}
        if dupes:
            print(f"POOL FAIL — duplicate {label}: {sorted(dupes)}")
            failed += 1

    soft_total = 0
    year_review: list[tuple[str, int, int]] = []
    for i, entry in enumerate(entries, 1):
        print(f"[{i}/{len(entries)}] {entry.title} — {entry.author} (gid {entry.gutenberg_id})")
        t0 = time.time()
        hard, soft, ol_year = verify(entry, check_ol=not args.no_ol)
        took = time.time() - t0
        if ol_year:
            year_review.append((entry.title, entry.first_published, ol_year))

        if hard:
            failed += 1
            for msg in hard:
                print(f"    FAIL  {msg}")
        else:
            print(f"    ok    id={entry.canonical_id}  initials={entry.author_initials}  "
                  f"banned={len(entry.banned_terms())} terms  ({took:.1f}s)")
        for msg in soft:
            soft_total += 1
            print(f"    note  {msg}")

    if year_review:
        print("\nYEAR CLUES NEEDING A HUMAN EYE (pool vs Open Library):")
        for title, ours, theirs in year_review:
            print(f"  {title:<45} pool {ours}   OL {theirs}")
        print("  Open Library is a second opinion, not an authority. Confirm each by hand;")
        print("  if a year cannot be established, drop the year clue rather than guess it.")

    print(f"\n{len(entries) - failed}/{len(entries)} entries verified, "
          f"{failed} failing, {soft_total} note(s) for human review.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
