"""
scripts/akinator/harvest_fandom_authors.py — author and year for the census novels.

    python scripts/akinator/harvest_fandom_authors.py --limit 10
    python scripts/akinator/harvest_fandom_authors.py --provider groq

WHY, with a number. `simulate.py --target-prefix /fandom/` scores the
added rows at 11.8%; the same measurement against Open Library's own books
at the same ranks (`--target-rank 3000:3220 --target-prefix /works/`)
scores 29.2%. That 17.4-point gap is what the Fandom rows still owe, and
`fandom_books.py` names the two things they are missing: no author and no
first-publish year. Between them those feed the era questions and — via
the author key — every Wikidata author fact the game asks about
constantly: nationality, gender, living, prolific.

An author also unlocks the 44 census titles currently SKIPPED because
they collide with an Open Library row. Merging on title alone is how
"Against the Gods" would inherit the cast of Peter L. Bernstein's book on
risk management; `site_books.py` matches on title plus author surname for
exactly that reason, and this is the field that makes that possible.

TWO SOURCES, STRUCTURED FIRST. The infobox on a real book article carries
the answer exactly and for free:

    Dune (novel)   | author = [[Frank Herbert]]
                   | date published = 1965

That is the same reasoning the character harvest settled on — a filled
infobox field is a fact, not an extraction. But the infobox only exists
when the article IS a book article, and `harvest_fandom_text.py` verified
prose on pages that are often something else (an author's biography, the
wiki's own about page). Measured on the first five: one had an infobox.

So the model reads the verified prose when the infobox does not answer,
under the same discipline as every other extraction here — it is told to
return null rather than guess, because a wrong author is a claim the game
will then ask four questions about.

REGEX WAS TRIED FIRST AND IS NOT ENOUGH. Patterns found an author in 76
of the 97 texts and got the boundary wrong on most of them: "Frank
Herbert and published", "Jeff Kinney. It is", and — matching `by` in the
wrong clause entirely — "of the Camp Half-Blood" for Riordan and "best
known for his" for Stephen King. Years came back for 19. A dirty author
string is worse than none: it will not match the corpus lookup, and if it
did it would match the wrong person.

Output: data/akinator_fandom_authors.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from prove_fandom import Unavailable, _fetch          # noqa: E402
from extract_traits import _generate                  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_text.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_authors.json")

# Infobox parameter names that mean "who wrote it" and "when". Fandom
# wikis each invent their own, so this is a family rather than a name.
_AUTHOR_FIELD = re.compile(r"^(author|writer|written[ _]by|creator|novelist)s?$",
                           re.IGNORECASE)
_YEAR_FIELD = re.compile(
    r"^(date[ _]published|published|publication[ _]date|release[ _]date"
    r"|first[ _]published|pub[ _]date|released|year)$", re.IGNORECASE)

_WIKILINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
_YEAR_IN = re.compile(r"(1[5-9]\d{2}|20[0-4]\d)")


def infobox_fields(subdomain: str, page: str) -> tuple[str | None, int | None]:
    """(author, year) from the article's infobox, or (None, None).

    Reads the RAW wikitext, which is exactly what harvest_fandom_text.py
    throws away: `clean_wikitext` strips templates because they carry no
    sentences, and the infobox is a template. Same page, different half
    of it.
    """
    try:
        d = _fetch(f"https://{subdomain}.fandom.com/api.php?" +
                   urllib.parse.urlencode({
                       "action": "query", "format": "json", "titles": page,
                       "prop": "revisions", "rvprop": "content",
                       "rvslots": "main"}))
        pg = next(iter((d.get("query") or {}).get("pages", {}).values()))
        raw = pg["revisions"][0]["slots"]["main"]["*"]
    except (Unavailable, KeyError, IndexError, StopIteration, TypeError):
        return None, None

    author = year = None
    for key, value in re.findall(
            r"^\s*\|\s*([A-Za-z_ ]{2,24})\s*=\s*([^\n|]{1,120})",
            raw[:4000], re.M):
        key, value = key.strip(), value.strip()
        if not value:
            continue
        if author is None and _AUTHOR_FIELD.match(key):
            name = _WIKILINK.sub(r"\1", value)
            name = re.sub(r"<[^>]+>|'{2,}", "", name).strip(" ,.")
            # A field holding a sentence is not a name.
            if name and len(name) <= 60 and name.count(" ") <= 4:
                author = name
        if year is None and _YEAR_FIELD.match(key):
            m = _YEAR_IN.search(value)
            if m:
                year = int(m.group(1))
    return author, year


def _build_prompt(title: str, text: str) -> str:
    return (
        "From the wiki text below, identify who WROTE this work and the "
        "year it was first published.\n\n"
        f"WORK: {title}\n"
        f"TEXT:\n{text[:2500]}\n\n"
        "Rules, in order of importance:\n"
        "1. Answer ONLY from the text above. Do not use anything you know "
        "about this work from elsewhere.\n"
        "2. If the text does not clearly state the author, return null for "
        "it. Returning null is always better than guessing — a wrong "
        "author makes the game ask several wrong questions about the "
        "book.\n"
        "3. The author is the person who wrote the BOOK. Not a character, "
        "not an illustrator, not a translator, not the wiki's editors.\n"
        "4. The year is when the work was FIRST published. If the text "
        "gives only a range or a later edition, return null.\n\n"
        'Reply with JSON only, in this exact shape:\n'
        '{"author": "Frank Herbert", "year": 1965}\n'
        'or {"author": null, "year": null}\n')


def _parse(raw: str) -> tuple[str | None, int | None]:
    if not raw:
        return None, None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None, None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None
    author = d.get("author")
    year = d.get("year")
    if isinstance(author, str):
        author = author.strip() or None
        # Same shape test as the infobox path: a sentence is not a name.
        if author and (len(author) > 60 or author.count(" ") > 4):
            author = None
    else:
        author = None
    try:
        year = int(year)
        if not (1500 <= year <= 2049):
            year = None
    except (TypeError, ValueError):
        year = None
    return author, year


def _is_the_title(author: str, title: str) -> bool:
    """An "author" that is just the work's own name is a field mismatch.

    EXACT equality only, and the two live near-misses are why. "The 39
    Clues" came back as its own author -- a multi-author series whose
    infobox files the series name in the author field. But "Asimov" ->
    "Isaac Asimov" and "The Walking Dead by EDStudios" -> "EDStudios" are
    both CORRECT: a wiki named for its author, and a title that names its
    author outright. A containment test would throw away both to catch
    the one.
    """
    from features import normalize
    return normalize(author) == normalize(title)


def harvest_one(title: str, rec: dict, provider: str) -> dict:
    sub, page = rec.get("subdomain"), rec.get("page")
    author = year = None
    source = None
    if sub and page:
        author, year = infobox_fields(sub, page)
        if author and _is_the_title(author, title):
            author = None
        if author or year:
            source = "infobox"
    if not author and rec.get("text"):
        a, y = _parse(_generate(_build_prompt(title, rec["text"]), provider))
        if a and _is_the_title(a, title):
            a = None
        if a:
            author, source = a, (source or "model")
        if y and not year:
            year = y
    return {"author": author, "year": year, "source": source}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--provider", choices=["auto", "groq"], default="groq")
    ap.add_argument("--groq-model", default=None,
                    help="Groq model id. The project default "
                         "(llama-3.3-70b-versatile) returns 404 'does not "
                         "exist or you do not have access to it' as of "
                         "2026-08-17 — verified live against four model ids; "
                         "openai/gpt-oss-120b answers 200.")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.groq_model:
        import extract_traits
        extract_traits.GROQ_MODEL_OVERRIDE = args.groq_model
        print(f"Groq model: {args.groq_model}\n")

    with open(TEXT_PATH, encoding="utf-8") as fh:
        texts = json.load(fh)
    targets = [(t, r) for t, r in texts.items() if r.get("text")]

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)
    todo = [(t, r) for t, r in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(targets)} book(s) with verified prose; {len(done)} done; "
          f"{len(todo)} to do\n")

    for i, (title, rec) in enumerate(todo, 1):
        try:
            res = harvest_one(title, rec, args.provider)
        except Exception as exc:                                # noqa: BLE001
            res = {"author": None, "year": None,
                   "source": f"{type(exc).__name__}"}
        done[title] = res
        print(f"  [{i:>3}/{len(todo)}] {title[:28]:<28} "
              f"{str(res['author'])[:26]:<26} {res['year'] or '----'}  "
              f"{res['source'] or ''}")
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    a = sum(1 for r in done.values() if r.get("author"))
    y = sum(1 for r in done.values() if r.get("year"))
    ib = sum(1 for r in done.values() if r.get("source") == "infobox")
    print(f"\n{a}/{len(done)} with an author, {y} with a year "
          f"({ib} answered by an infobox, no model call needed)")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
