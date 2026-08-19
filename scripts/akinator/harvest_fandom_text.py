"""
scripts/akinator/harvest_fandom_text.py — the prose the trait labeller needs.

    python scripts/akinator/harvest_fandom_text.py --limit 10
    python scripts/akinator/harvest_fandom_text.py            # resumes

WHY A SEPARATE HARVEST FROM THE LABELLING. `traits.py` turns a
DESCRIPTION into labels, and its prompt's first rule is "answer ONLY from
the description above". `harvest_descriptions.py` supplies that
description from Open Library, which is empty for every web novel the
Fandom work is about. This is the same job against a different source, and
it is split from the labelling for the same reason Open Library's is:
fetching text is cheap, resumable and repeatable; labelling it costs model
calls and should never re-fetch to re-label.

THE WHOLE DIFFICULTY IS PICKING THE RIGHT ARTICLE, and it is a much harder
problem here than the character harvest's. A character page IS a
character; there is no equivalent guarantee that the longest article
matching a book's title is ABOUT the book. Measured over the first
fourteen census novels (2026-08-16), taking the best search hit by length
produced usable prose 13 times out of 14 and the WRONG SUBJECT in four
of those:

    A Wheel of Time  "The Wheel of Time is a television series based on
                      the novels of the same name…"   -> the TV show
    Warriors         "The Warriors App was a downloadable application…"
                                                       -> a phone app
    Agatha Christie  "Dame Agatha Mary Clarissa, Lady Mallowan, DBE (15
                      September…"                      -> her biography
    Memory Beta      "Living Memory is a TOS novel…"    -> one novel of many

That is worse than finding nothing. `traits.py` is instructed to trust the
supplied text completely, so prose about a television adaptation yields
confident labels about a television adaptation, filed against the book —
and its own docstring is explicit that "a wrong label removes the correct
book from the game". Recall here is worth very little and precision is
worth almost everything.

SO THE TEXT IS VERIFIED BEFORE IT IS KEPT, reusing `prove_fandom.py`'s
`_IS_WRITTEN_WORK` / `_IS_OTHER_MEDIUM` rather than a new test. Those
patterns exist for exactly this distinction — that module's comment
("the lead has to say what kind of thing it found") is describing this
problem in a different context. An article's opening has to name a
written work, and must not instead name another medium, or it is dropped
and the book gets no text at all. Books that end with nothing here are a
FEATURE: they will simply carry no trait labels, which is what
`work_traits.py` calls "unknown", never "no".

Output: data/akinator_fandom_text.json (gitignored, regenerable).
Resumable. Feeds `traits.py`'s existing prompt unchanged — this module
writes text, never labels.
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

from prove_fandom import (  # noqa: E402
    _IS_OTHER_MEDIUM,
    _IS_OTHER_MEDIUM_CLAIM,
    _IS_WRITTEN_WORK,
    _PARENTHETICAL,
    Unavailable,
    _fetch,
    _same_work,
    _tokens,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS_PATH = os.path.join(REPO_ROOT, "data", "fandom_novel_census.json")
PROVEN_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_text.json")

MIN_TEXT = 300        # below this there is nothing for a labeller to judge

# Page names a single-work wiki uses for the work itself. Deliberately
# short: every entry can only mean "the written thing this wiki is about",
# and anything vaguer ("Story", "Plot", "Main Page") describes the contents
# rather than the work and is left out.
#
# COMPARED AS PLAIN TEXT, NOT THROUGH `_tokens`, which was the first attempt
# and silently could not work: `_tokens` drops title stopwords, and "novel"
# is one of them — correctly, since it is what makes "Dune (novel)" match
# "Dune". So `_tokens("Light Novel")` is `{"light"}` and no entry here could
# ever have matched it.
_CANONICAL_WORK_PAGES = frozenset({
    "novel", "novels", "the novel", "the novels",
    "light novel", "light novels", "web novel", "web novels", "webnovel",
    "book", "books", "the books", "volume", "volumes", "the volumes",
})


def _canonical_work_page(page: str) -> bool:
    return re.sub(r"[^a-z0-9 ]+", "", (page or "").lower()).strip() \
        in _CANONICAL_WORK_PAGES
MAX_TEXT = 4000       # traits.py judges an intro, not a whole wiki article
LEAD_WINDOW = 400     # how much of the opening the written-work test reads


def clean_wikitext(raw: str) -> str:
    """Wikitext -> plain prose, aggressively enough for a labelling prompt.

    Not a general parser and does not need to be: templates, infoboxes and
    file links carry no sentences, and everything from the first
    References/Gallery heading on is apparatus rather than description.
    """
    w = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    w = re.sub(r"\{\|.*?\|\}", "", w, flags=re.S)       # tables
    for _ in range(4):                                   # nested templates
        w = re.sub(r"\{\{[^{}]*\}\}", "", w)
    w = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", "", w, flags=re.I)
    w = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", w)
    w = re.sub(r"<ref[^>]*>.*?</ref>", "", w, flags=re.S | re.I)
    w = re.sub(r"<[^>]+>", " ", w)
    w = re.split(r"==+\s*(?:References|External links?|Navigation|Gallery"
                 r"|See also|Trivia)\s*==+", w, flags=re.I)[0]
    w = re.sub(r"==+[^=]+==+", " ", w)
    w = re.sub(r"'{2,}", "", w)
    w = re.sub(r"^[\*#:;]+", " ", w, flags=re.M)
    return re.sub(r"\s+", " ", w).strip()


# A page that only lists what a name could mean. Its prose is "X can
# refer to the following" -- true, useless, and it passes every other
# test here. Found live: the Wheel of Time wiki's "Wheel of Time
# (disambiguation)" is what survives once the TV article is excluded.
_DISAMBIGUATION = re.compile(
    r"\b(can refer to|may refer to|disambiguation)\b", re.IGNORECASE)

# THE AUTHOR-WIKI TRAP. Many census wikis are named for a writer rather
# than a work — Agatha Christie, Stephen King, Turtledove, Tamora Pierce,
# Brandon Sanderson, Tanith Lee. There the article whose title matches is
# the author's BIOGRAPHY, it passes `_same_work` outright, and it passes
# the written-work test because any novelist's lead mentions novels. The
# labeller would then read a life and file its labels against the books:
# "Dame Agatha Mary Clarissa, Lady Mallowan, DBE (15 September 1890…)"
# yields t:realevents for sixty-six murder mysteries.
#
# Lead-only, like every other test here: a birth date in parentheses or
# an "is/was a … novelist" copula is a biography's opening and nothing
# else's.
# THE TWO FORMS IT MISSED, both live on the Stephen King wiki — whose
# biography this harvest ACCEPTED, and whose life was therefore available to
# be labelled against his novels, which is precisely what the Agatha
# Christie note above says must not happen. Christie was caught and King was
# not, for reasons that are pure notation:
#
#   "(born September 21, 1947)"       month-first, and with no dash after it.
#                                     The second branch demanded a dash, which
#                                     only a dead author's dates carry.
#   "is an European-American author"  the intervening words were `\w+`, and a
#                                     hyphen is not a word character, so the
#                                     copula branch could never reach "author".
#
# Same shape as the `song` false positive fixed in prove_fandom.py this
# session: the rule was right about what it wanted and too narrow about how
# the text actually writes it.
_IS_PERSON = re.compile(
    r"\(\s*(?:born\s+)?\d{1,2}\s+\w+\s+\d{4}"           # (15 September 1890
    r"|\(\s*(?:born\s+)?\w+\s+\d{1,2},\s*\d{4}"         # (born September 21, 1947
    r"|\b(?:is|was)\s+(?:an?\s+)?(?:[\w-]+\s+){0,3}"
    r"(?:writer|author|novelist|poet|playwright|journalist)\b",
    re.IGNORECASE)


def describes_a_written_work(text: str) -> tuple[bool, str]:
    """Does this article's opening say it is about a book?

    Reads only the lead — an article about a TV adaptation will mention
    the novels somewhere further down, and that is not the same claim as
    opening with them. Same reasoning as `prove_fandom.signal_identity`
    reading the lead rather than the whole page.
    """
    lead = text[:LEAD_WINDOW]
    if _DISAMBIGUATION.search(lead):
        return False, "disambiguation page"
    if _IS_PERSON.search(lead):
        return False, "lead describes the author, not a work"
    # An eastern adaptation only counts when the lead CLAIMS to be one --
    # see _EASTERN_MEDIA for the four web novels a bare-mention test cost.
    claim = _IS_OTHER_MEDIUM_CLAIM.search(lead)
    if claim and not _IS_OTHER_MEDIUM.search(lead):
        return False, f"lead claims another medium: {claim.group(0)!r}"
    other = _IS_OTHER_MEDIUM.search(lead)
    if other:
        # A lead that names another medium is a finding, not a gap -- the
        # Wheel of Time TV article is the live example.
        #
        # THIS STAYS A BARE MENTION, AND THAT WAS TESTED THE OTHER WAY. It
        # over-rejects: "Omniscient Reader's Viewpoint" is thrown away
        # because its authors' pen name renders as "sing N song" and `song`
        # is on the list. Anchoring the test on a copula ("is a film") fixes
        # that one case and was measured across all 228 wikis -- it let in
        # "Frankenstein (1910 film)", "Jack Reacher (2012 film)", "The Dark
        # Tower (film)" and "The Work and the Glory (film)" as though they
        # were the books, because a film article's lead does not always use
        # the copula. Seven of eleven newly accepted pages were adaptations.
        #
        # One web novel lost against four films admitted is the wrong trade
        # in this module: recall is worth very little here and precision is
        # worth almost everything, because a wrong text is labelled with
        # confidence and removes the right book from the game. So the crude
        # test stands, and ORV stays missing until something sharper than a
        # word list can tell a pen name from a claim.
        return False, f"lead names another medium: {other.group(0)!r}"
    if not _IS_WRITTEN_WORK.search(lead):
        return False, "lead never says it is a written work"
    return True, ""


def candidate_articles(subdomain: str, title: str) -> list[tuple[str, str]]:
    """(page title, wikitext) for the search hits worth testing."""
    params = {
        "action": "query", "format": "json", "redirects": 1,
        "generator": "search", "gsrsearch": title, "gsrlimit": 5,
        "gsrnamespace": 0, "prop": "revisions", "rvprop": "content",
        "rvslots": "main",
    }
    d = _fetch(f"https://{subdomain}.fandom.com/api.php?"
               + urllib.parse.urlencode(params))
    out: list[tuple[str, str]] = []
    for p in ((d.get("query") or {}).get("pages") or {}).values():
        try:
            out.append((p["title"], p["revisions"][0]["slots"]["main"]["*"]))
        except (KeyError, IndexError, TypeError):
            continue
    return out


def harvest_one(title: str, subdomain: str) -> dict:
    try:
        candidates = candidate_articles(subdomain, title)
    except Unavailable as exc:
        return {"subdomain": subdomain, "text": None,
                "reason": f"unavailable: {exc}"}
    if not candidates:
        return {"subdomain": subdomain, "text": None,
                "reason": "search returned no article"}

    rejected: list[str] = []
    # THE ARTICLE MUST BE THE WORK, not merely a book on the same wiki.
    # Verifying "is this about a written work" was not enough: it cleared
    # the TV-adaptation articles but still handed back "The Warriors
    # Guide" for Warriors, "Conspiracy in Death" (one novel of ~60) for
    # In Death, and "Origins of the Wheel of Time" (a companion volume)
    # for The Wheel of Time. All three are real books, none is the book
    # asked about, and traits.py would have labelled the series from a
    # field guide.
    #
    # `_same_work` is the existing answer to exactly this -- strict set
    # equality on significant words after a trailing disambiguator is
    # stripped, so "Dune" accepts "Dune (novel)" and rejects every one of
    # the above for carrying an extra significant word. Its docstring
    # documents this being learned the hard way twice already.
    # THE DISAMBIGUATOR IS EVIDENCE, and throwing it away was a real cost.
    # `_same_work` strips a trailing parenthetical so "Dune" can match "Dune
    # (novel)" — necessary, and it also discards the one part of the title
    # that says what the page IS. Loosening the lead test earlier this
    # session made that hole visible immediately: the harvest came back
    # holding "Frankenstein (1910 film)", "Jack Reacher (2012 film)", "The
    # Dark Tower (film)" and "The Work and the Glory (film)" as though they
    # were the books. Every one announces itself in the title.
    #
    # Read before stripping, and only for a medium — "(novel)", "(web
    # serial)" and "(2019)" stay as harmless as they were.
    candidates = [(p, raw) for p, raw in candidates
                  if not _IS_OTHER_MEDIUM.search(
                      " ".join(_PARENTHETICAL.findall(p) or []))]

    named = [(p, raw) for p, raw in candidates if _same_work(title, p)]

    # WHERE A WEB-NOVEL WIKI ACTUALLY PUTS THE WORK, which is not under the
    # work's name. A wiki that IS about one novel has no reason to repeat
    # its own title: it describes the book on a page called "Light Novel",
    # "Books", "Novel" or "Volumes" and spends the rest of itself on
    # characters and chapters. Measured on the 153 works this harvest left
    # empty, 47 were refused here, at the title, having never been read —
    # and "Light Novel" on the Mushoku Tensei wiki (1,204 words) passes the
    # written-work test comfortably once it is allowed through.
    #
    # THE GUARD IS WHAT MAKES THIS SAFE, because `_same_work` is strict for
    # good reasons its own docstring records twice over. An author wiki also
    # has a "Novels" page and it is a LIST — sixty-six Christie mysteries,
    # from which traits.py would happily label one book with all of them. So
    # a canonical page counts only when its own lead names the work we asked
    # about. A list page opens by naming the author or nothing; the Mushoku
    # Tensei "Light Novel" page opens "Mushoku Tensei: Jobless Reincarnation
    # … is a series of high fantasy light novel".
    if not named:
        wanted = _tokens(_PARENTHETICAL.sub("", title))
        for page, raw in candidates:
            if not _canonical_work_page(page):
                continue
            lead = clean_wikitext(raw)[:LEAD_WINDOW]
            if wanted and wanted <= _tokens(lead):
                named.append((page, raw))

    if not named:
        return {"subdomain": subdomain, "text": None,
                "reason": "no article names this work",
                "rejected": [p for p, _ in candidates][:5]}

    # Longest first, among the articles that ARE this work: a wiki may
    # hold both a stub and a full article under equivalent titles.
    for page, raw in sorted(named, key=lambda c: -len(c[1])):
        text = clean_wikitext(raw)
        if len(text) < MIN_TEXT:
            rejected.append(f"{page}: too short ({len(text)})")
            continue
        ok, why = describes_a_written_work(text)
        if not ok:
            rejected.append(f"{page}: {why}")
            continue
        return {"subdomain": subdomain, "page": page,
                "text": text[:MAX_TEXT], "chars": min(len(text), MAX_TEXT),
                "rejected": rejected}

    # Nothing verifiable. No text is the correct outcome, not a failure --
    # the book will simply carry no trait labels.
    return {"subdomain": subdomain, "text": None,
            "reason": "no candidate article describes a written work",
            "rejected": rejected}


def load_targets(source: str) -> list[tuple[str, str]]:
    if source == "proven":
        with open(PROVEN_PATH, encoding="utf-8") as fh:
            return [(t, e["subdomain"]) for t, e in json.load(fh).items()
                    if e.get("subdomain")]
    with open(CENSUS_PATH, encoding="utf-8") as fh:
        results = json.load(fh)["results"]
    keep = {"NOVEL", "LITERARY_MULTI_MEDIA"}
    return [(r["title"], r["subdomain"]) for r in results
            if r["type"] in keep and r.get("subdomain")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("census", "proven"), default="census")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    done: dict[str, dict] = {}
    if os.path.exists(OUT_PATH) and not args.refresh:
        with open(OUT_PATH, encoding="utf-8") as fh:
            done = json.load(fh)

    targets = load_targets(args.source)
    todo = [(t, s) for t, s in targets if t not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(targets)} target(s); {len(done)} already harvested; "
          f"{len(todo)} to do\n")

    for i, (title, sub) in enumerate(todo, 1):
        try:
            res = harvest_one(title, sub)
        except Exception as exc:                                # noqa: BLE001
            res = {"subdomain": sub, "text": None,
                   "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
        done[title] = res
        mark = f"{res['chars']:>5}c" if res.get("text") else "  --  "
        note = res.get("page") or res.get("reason", "")
        print(f"  [{i:>3}/{len(todo)}] {title[:30]:<30} {mark}  {note[:44]}")
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump(done, fh, ensure_ascii=False, indent=1)
        time.sleep(args.delay)

    got = [t for t, r in done.items() if r.get("text")]
    print(f"\n{len(got)}/{len(done)} book(s) have verified prose")
    print(f"-> {OUT_PATH}")
    print("\nText only. Labelling is traits.py's job and a separate run; "
          "books without text here will carry no labels, never 'no'.")


if __name__ == "__main__":
    main()
