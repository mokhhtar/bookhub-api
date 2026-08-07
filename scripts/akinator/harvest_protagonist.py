"""
scripts/akinator/harvest_protagonist.py — is the main character a woman?

    python scripts/akinator/harvest_protagonist.py --limit 2000

TWO SUB-PROBLEMS, and the hard one was not gender.

**Which character is the protagonist?** Wikidata's `P674` is an unordered
list, so it cannot say. Open Library can: cataloguers list the protagonist
first in the `person` field. Spot-checked against books whose protagonist
is not in doubt — Harry Potter, Elizabeth Bennet, Katniss Everdeen, Bilbo
Baggins, Jane Eyre, Nick Carraway, Matilda, and To Kill a Mockingbird's
"Jean Louise Finch" — **8 of 8**.

That Mockingbird row is worth keeping in mind: an automated check first
scored it a miss because it was looking for "Scout", and Jean Louise Finch
*is* Scout. A name test that does not know a character's other names
under-reports. Same alias problem as everywhere else in this pipeline.

**What gender is that character?** Deliberately NOT a model call, and not
a guess from the first name. Counting `he/him/his` against `she/her/hers`
in the character's own Wikipedia article was measured against thirteen
well-known characters:

    Harry Potter    134/34  -> male     Hermione Granger   17/101 -> female
    Elizabeth Bennet 31/139 -> female   Frodo Baggins     101/3   -> male
    Katniss Everdeen 23/147 -> female   Sherlock Holmes   115/10  -> male
    Bilbo Baggins    58/3   -> male     Atticus Finch      23/6   -> male
    Hercule Poirot  166/14  -> male     Aslan              59/8   -> male
    Jane Eyre        74/182 -> unknown  Scout Finch       123/129 -> unknown
    Lisbeth Salander 79/160 -> unknown

**10 correct, 0 wrong, 3 honest unknowns.** Deterministic, free, instant,
auditable, and structurally incapable of hallucinating — which is why it
runs instead of Gemini even though Gemini is available and free. The three
unknowns all have the same cause: the article spends much of its length on
*other* characters (Rochester, Atticus, Blomkvist). Returning unknown there
is correct behaviour, not a shortfall.

WHY NOT INFER GENDER FROM THE FIRST NAME. It is wrong often enough to
matter and badly wrong on non-Western names, and a confidently wrong answer
here eliminates the correct book. Unknown costs nothing; wrong costs the
game. See [[Grounding Rule]].

WHERE A MODEL WOULD EARN ITS PLACE, and this is not it: traits with no
countable signal — does the character use magic, are they royalty, a
detective, a child. Those need comprehension and belong in the extensible
`character_traits` table, extracted from cached article text under the
`fandom.py` prompt discipline.

Output: data/akinator_protagonist.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from characters import canonical_name  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_protagonist.json")

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

_MALE = re.compile(r"\b(he|him|his|himself)\b", re.I)
_FEMALE = re.compile(r"\b(she|her|hers|herself)\b", re.I)

# Thresholds, both set by the measurement above rather than by taste.
# The first version demanded 10+ pronouns and reported "unknown" for every
# one of thirteen characters whose signal was in fact unanimous (8/0, 0/6,
# 9/0) — the method was right and the threshold was wrong.
MIN_PRONOUNS = 3
MAJORITY = 0.75

# Article text is capped, not because of cost but because long articles
# drift into plot summary covering the whole cast, which is exactly what
# produced the three unknowns.
MAX_CHARS = 20000

# The opening paragraph, which introduces the subject and nobody else.
LEAD_CHARS = 800


def wikipedia_extract(title: str, timeout: int = 40) -> str:
    url = WIKI_API + "?" + urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": 1,
        "format": "json", "redirects": 1, "titles": title,
    })
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    for page in (data.get("query", {}).get("pages") or {}).values():
        return (page.get("extract") or "")[:MAX_CHARS]
    return ""


def _count(text: str) -> str | None:
    male = len(_MALE.findall(text))
    female = len(_FEMALE.findall(text))
    total = male + female
    if total < MIN_PRONOUNS:
        return None
    if male / total >= MAJORITY:
        return "male"
    if female / total >= MAJORITY:
        return "female"
    return None


def gender_from_text(text: str) -> str | None:
    """'male' / 'female' / None. None is a first-class answer.

    THE LEAD IS READ FIRST, and the reason is a bias worth naming. A full
    article about a female character spends much of its length on the men
    around her — Rochester, Blomkvist, Atticus — so the majority test comes
    out inconclusive far more often for women than for men. The first pass
    over the harvest showed it plainly: **74 male against 16 female** among
    resolved names. Not wrong answers, but asymmetric unknowns, which would
    have made "is the main character a woman?" quietly worse at detecting
    exactly the case it asks about.

    The lead paragraph is about the subject and nobody else, so it is much
    less contaminated. Measured over twelve well-known characters, the two
    passes each got 8 right with 0 wrong but failed on *different* people —
    the lead rescued Jane Eyre (0/3) where the full article was 74/182, and
    the full article rescued Jo March where the lead had a single pronoun.
    Trying the lead and falling back resolves both: **10 of 12, still zero
    wrong**, and the two it recovered were both women.
    """
    return _count(text[:LEAD_CHARS]) or _count(text)


def protagonist_of(doc: dict) -> str:
    """The first usable name in OL's `person` list — see the module note."""
    for raw in doc.get("person") or []:
        name = canonical_name(raw)
        if name and not any(ch.isdigit() for ch in name):
            return raw
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--limit", type=int, default=0,
                    help="only the N most-read books with a cast (0 = all)")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--retry-unknown", action="store_true",
                    help="re-resolve names previously cached as unknown. "
                         "Needed after any change to gender_from_text: the "
                         "cache skips names it already holds, so an "
                         "improvement to the resolver reaches none of them.")
    args = ap.parse_args()

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
    targets = [d for d in docs if d.get("person")]
    if args.limit:
        targets = targets[:args.limit]

    # Keyed on the character NAME, not the book: one lookup serves every
    # book that character appears in, and series protagonists repeat a lot.
    cache: dict[str, str | None] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            cache = json.load(fh)
        if args.retry_unknown:
            stale = [k for k, v in cache.items() if not v]
            for k in stale:
                del cache[k]
            print(f"Retrying {len(stale)} names previously cached as unknown.")
        print(f"Resuming with {len(cache)} names already resolved.")

    print(f"{len(targets)} books have a cast; resolving their first-listed name.\n")

    resolved = 0
    for i, doc in enumerate(targets):
        raw = protagonist_of(doc)
        if not raw:
            continue
        name = canonical_name(raw)
        if name in cache:
            continue
        try:
            text = wikipedia_extract(raw)
        except Exception:  # noqa: BLE001 — a missing article is the common case
            text = ""
        cache[name] = gender_from_text(text) if text else None
        resolved += 1

        if resolved % 100 == 0:
            known = sum(1 for v in cache.values() if v)
            print(f"  {i + 1:>6}/{len(targets)} books   "
                  f"{len(cache):>5} names, {known} with a gender")
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)

    female = sum(1 for v in cache.values() if v == "female")
    male = sum(1 for v in cache.values() if v == "male")
    print(f"\n{len(cache)} names resolved: {male} male, {female} female, "
          f"{len(cache) - male - female} unknown")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
