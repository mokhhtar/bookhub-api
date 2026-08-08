"""
scripts/akinator/harvest_fandom.py — web novels the catalogue never ranks.

    python scripts/akinator/harvest_fandom.py --verify   # check the wikis
    python scripts/akinator/harvest_fandom.py

WHY. The owner reported that the game could not guess *Lord of Mysteries*,
and it genuinely could not: it is a Chinese web novel with a large Fandom
wiki and no presence in Open Library's reading-log ranking, so nothing in
the pipeline would ever surface it. The same is true of most of the
48-title seed list — Solo Leveling, Omniscient Reader's Viewpoint, Reverend
Insanity, The Wandering Inn.

There is no enumerable source for "books with Fandom wikis". Fandom's own
`Wikis/List` API returns 403, and Wikidata's Fandom-article property covers
2,843 works that skew hard to tie-in fiction — Star Trek novelisations,
Forgotten Realms — while missing exactly this category. So the list is
curated by the owner, the same way the Guess the Book pool is, and this
turns it into corpus entries.

THE RESOLVER FAILS OPEN, AND HERE THAT IS NOT ACCEPTABLE.
`fandom.py`'s `resolve_fandom_subdomain` is used elsewhere to enrich a page
that already exists, where a wrong wiki degrades a section. Here it would
mint a whole book from the wrong source. Measured on the first eight seeds:
seven resolved correctly and **"Re:Zero kara Hajimeru Isekai Seikatsu"
resolved to `turtledove`** — Harry Turtledove's alternate-history wiki —
because the relevance check timed out and the failure defaulted to
accepting the match.

So every resolution here is re-verified against the wiki's own front page,
and a check that cannot complete is treated as a FAILURE, not a pass. A
missing book is a book the game says it does not know; a wrong book is the
game asserting something false.

Output: data/akinator_fandom.json (gitignored, regenerable).
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

from features import normalize  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEED_PATH = os.path.join(REPO_ROOT, "data", "fandom_seed.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

# Words to ignore when checking that a wiki is about the right book.
_STOP = {"the", "a", "an", "of", "and", "in", "my", "that", "with", "to",
         "is", "it", "for", "s", "kara", "no"}


def _title_tokens(title: str) -> set[str]:
    return {t for t in normalize(title).split() if t not in _STOP and len(t) > 2}


def wiki_about(subdomain: str, timeout: int = 25) -> dict | None:
    """The wiki's own description of itself, via its site info."""
    url = (f"https://{subdomain}.fandom.com/api.php?"
           + urllib.parse.urlencode({
               "action": "query", "meta": "siteinfo",
               "siprop": "general|statistics", "format": "json"}))
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    q = data.get("query") or {}
    gen = q.get("general") or {}
    stats = q.get("statistics") or {}
    return {
        "sitename": gen.get("sitename") or "",
        "base": gen.get("base") or "",
        "articles": stats.get("articles") or 0,
    }


def verify(title: str, subdomain: str) -> tuple[bool, str]:
    """Does this wiki actually cover this book?

    Fails CLOSED. A check that times out or errors returns False, because
    the alternative — the behaviour that sent Re:Zero to a Harry Turtledove
    wiki — is to invent a book from unrelated source material.
    """
    try:
        info = wiki_about(subdomain)
    except Exception as exc:  # noqa: BLE001
        return False, f"site info failed ({str(exc)[:40]})"
    if not info:
        return False, "no site info"

    want = _title_tokens(title)
    got = _title_tokens(info["sitename"]) | _title_tokens(subdomain)
    # A shared significant word is weak on its own, so require either a
    # decent overlap or the subdomain literally containing the title.
    overlap = want & got
    squashed_title = re.sub(r"[^a-z0-9]", "", normalize(title))
    squashed_sub = re.sub(r"[^a-z0-9]", "", subdomain)

    # ONE-WAY containment only: the wiki name may be inside the title's
    # opening, never the reverse.
    #
    # The reverse test looked symmetric and is not. "Harry Potter and the
    # Methods of Rationality" is a fanfiction with its own wiki, and the
    # resolver offered `harrypotter` — the main Harry Potter wiki. Because
    # "harrypotte" appears inside the long title, a reverse test accepted
    # it, and the game would have built a fanfiction's entry from canon.
    # A wiki about a BROADER work is not a wiki about this book.
    contained = bool(squashed_title) and squashed_sub and (
        squashed_sub in squashed_title[:len(squashed_sub) + 4]
        and len(squashed_sub) >= max(8, len(squashed_title) // 2))

    # Two thirds of the title's significant words, not half.
    #
    # Half let "Harry Potter and the Methods of Rationality" match the main
    # `harrypotter` wiki: {harry, potter} out of {harry, potter, methods,
    # rationality} is exactly half, and the two words it MISSES are the two
    # that identify the book. Requiring two thirds keeps Shadow Slave (2/2),
    # The Wandering Inn (2/2) and Against the Gods (2/2) while rejecting a
    # wiki that only knows the franchise a book belongs to.
    enough = want and len(overlap) * 3 >= len(want) * 2
    if contained or enough:
        return True, f"{info['sitename'][:40]} ({info['articles']} articles)"
    return False, f"unrelated: {info['sitename'][:40]}"


def candidate_subdomains(title: str) -> list[str]:
    """Subdomains a wiki for this title plausibly lives at.

    Tried BEFORE the resolver, because the resolver leans on Wikidata,
    DuckDuckGo and a Google CSE key, and web novels are exactly the
    category those miss — the first full pass rejected "He Who Fights with
    Monsters" and "Trash of the Count's Family" as having no wiki when both
    have large ones. Fandom subdomains are usually just the title with the
    spaces taken out, so guessing and CHECKING is cheaper and more reliable
    than searching. Every candidate still goes through verify().
    """
    n = normalize(title)
    words = [w for w in n.split() if w]
    squashed = "".join(words)
    hyphen = "-".join(words)
    no_stop = [w for w in words if w not in _STOP]
    out = [squashed, hyphen, "".join(no_stop), "-".join(no_stop)]
    # Series titles are often known by their first few words.
    if len(no_stop) > 2:
        out += ["".join(no_stop[:3]), "-".join(no_stop[:3])]
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen and len(c) > 3:
            seen.add(c)
            uniq.append(c)
    return uniq


def resolve(title: str, attempts: int = 3) -> tuple[str | None, str]:
    """Resolve with retries — the failures here are mostly transport.

    The first full pass rejected 26 of 48, and the log shows why: SSL
    handshake timeouts on the resolver's own Wikidata and DuckDuckGo
    lookups, reported as "no wiki found". A network blip and a book with no
    wiki are not the same finding and must not read the same.
    """
    # Guess-and-check first; it costs one cheap API call per candidate and
    # succeeds where the search-based resolver cannot.
    for cand in candidate_subdomains(title):
        ok, why = verify(title, cand)
        if ok:
            return cand, f"[direct] {why}"

    from tools.fandom import resolve_fandom_subdomain
    sub = None
    for attempt in range(attempts):
        try:
            sub = resolve_fandom_subdomain(title)
            if sub:
                break
        except Exception:  # noqa: BLE001
            pass
        if attempt < attempts - 1:
            time.sleep(2 + attempt * 2)
    if not sub:
        return None, "no wiki found (after retries)"
    ok, why = verify(title, sub)
    return (sub if ok else None), why


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", default=SEED_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--verify", action="store_true",
                    help="resolve and check only; write nothing")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    sys.path.insert(0, REPO_ROOT)
    with open(args.seed, encoding="utf-8") as fh:
        titles = json.load(fh)

    found: dict[str, dict] = {}
    if os.path.exists(args.out) and not args.verify:
        with open(args.out, encoding="utf-8") as fh:
            found = json.load(fh)

    ok = rejected = 0
    for title in titles:
        if title in found:
            continue
        sub, why = resolve(title)
        if sub:
            ok += 1
            print(f"  OK    {title[:42]:<42} -> {sub:<28} {why[:34]}")
            found[title] = {"subdomain": sub, "why": why}
        else:
            rejected += 1
            print(f"  REJECT {title[:42]:<42}    {why[:52]}")
        if not args.verify:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(found, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    print(f"\n{ok} verified, {rejected} rejected of {len(titles)}")
    if not args.verify:
        print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
