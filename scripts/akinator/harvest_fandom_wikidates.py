"""
scripts/akinator/harvest_fandom_wikidates.py — when each wiki was created.

    python scripts/akinator/harvest_fandom_wikidates.py        # resumes
    python scripts/akinator/harvest_fandom_wikidates.py --refresh

WHY A WIKI'S AGE IS EVIDENCE ABOUT A BOOK. 33 of the 39 web novels in the
game carry no publication year: the wikis catalogue chapters and characters,
and a prose harvest, a model and a keyword rule between them recovered four.
`features.py` can settle five of the six era questions from the form alone —
nothing serialised on the web predates 1900, 1950, 1970 or 2000 — but the
sixth, "in the last 10 years", is exactly the one that varies.

A wiki is made after the thing it is about. So:

    wiki created before 2016  ->  the book is older than 2016. Sound.
    wiki created after 2016   ->  nothing follows. Solo Leveling's wiki is
                                  from 2018 and the novel from 2014.

Only the first direction is used, which is why an imprecise date is fine
here and would not be if we were storing a year. Checked against the three
of these books whose year is known: the wiki was never older than the book.

WHICH TIMESTAMP, and why the LATER of two. Two API answers exist and they
disagree:

    list=allrevisions&arvdir=newer   the oldest revision anywhere on the wiki
    Main Page's first revision       what that one page remembers

Shadow Slave returns 2008 for the first and 2022 for the second, for a novel
from 2020: Fandom recycles subdomains, and a migrated or re-created wiki
keeps revisions older than itself. Taking the LATER answer is the safe
error. Overestimating the wiki's age only costs the inference — the question
stays unknown — while underestimating it asserts "not recent" about a book
that is.

Output: data/akinator_fandom_wikidates.json (gitignored, regenerable).
Read by fandom_books.py, which puts it on the doc as `wiki_created`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prove_fandom import Unavailable, _fetch  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_text.json")
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_wikidates.json")


def _oldest(subdomain: str) -> list[str]:
    """Every creation timestamp the wiki will admit to, as ISO strings."""
    found: list[str] = []
    queries = (
        {"action": "query", "format": "json", "list": "allrevisions",
         "arvdir": "newer", "arvlimit": "1", "arvprop": "timestamp"},
        {"action": "query", "format": "json", "prop": "revisions",
         "titles": "Main Page", "rvdir": "newer", "rvlimit": "1",
         "rvprop": "timestamp"},
    )
    for params in queries:
        try:
            d = _fetch(f"https://{subdomain}.fandom.com/api.php?"
                       + urllib.parse.urlencode(params))
        except Unavailable:
            continue
        q = d.get("query") or {}
        if "allrevisions" in q:
            for page in q["allrevisions"]:
                for rev in page.get("revisions") or []:
                    if rev.get("timestamp"):
                        found.append(rev["timestamp"])
        for page in (q.get("pages") or {}).values():
            for rev in page.get("revisions") or []:
                if rev.get("timestamp"):
                    found.append(rev["timestamp"])
    return found


def harvest_one(subdomain: str) -> dict:
    stamps = _oldest(subdomain)
    if not stamps:
        return {"created": None, "reason": "no revision timestamp available"}
    # The later of the two. See the module docstring: overestimating the
    # wiki's age costs an inference, underestimating it makes a false claim.
    latest = max(stamps)
    return {"created": int(latest[:4]), "created_at": latest,
            "sources": len(stamps)}


def load_subdomains() -> list[tuple[str, str]]:
    """(title, subdomain), from whichever harvest recorded one."""
    out: dict[str, str] = {}
    if os.path.exists(TEXT_PATH):
        with open(TEXT_PATH, encoding="utf-8") as fh:
            for title, rec in json.load(fh).items():
                sub = (rec or {}).get("subdomain")
                if sub:
                    out[title] = sub
    if os.path.exists(CENSUS_PATH):
        with open(CENSUS_PATH, encoding="utf-8") as fh:
            census = json.load(fh)
        rows = census if isinstance(census, list) else census.get("wikis") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title, sub = row.get("title"), row.get("subdomain")
            if title and sub:
                out.setdefault(title, sub)
    return sorted(out.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)

    targets = load_subdomains()
    todo = [(t, s) for t, s in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(targets)} wiki(s); {len(done)} already dated; {len(todo)} to do\n")

    for i, (title, sub) in enumerate(todo, 1):
        try:
            rec = harvest_one(sub)
        except Exception as exc:                                # noqa: BLE001
            rec = {"created": None, "reason": type(exc).__name__}
        rec["subdomain"] = sub
        done[title] = rec
        print(f"  [{i:>3}/{len(todo)}] {title[:34]:<34} "
              f"{rec.get('created') or rec.get('reason', '')}")
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    dated = sum(1 for v in done.values() if v.get("created"))
    usable = sum(1 for v in done.values()
                 if v.get("created") and v["created"] < 2016)
    print(f"\n{dated}/{len(done)} dated; {usable} predate 2016 and therefore "
          f"settle 'in the last 10 years'")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
