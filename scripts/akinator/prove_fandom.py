"""
scripts/akinator/prove_fandom.py — stage 2 of the Fandom redesign: PROOF.

    python scripts/akinator/prove_fandom.py                 # prove the stored 29
    python scripts/akinator/prove_fandom.py --only "Apotheosis"
    python scripts/akinator/prove_fandom.py --no-independent  # wiki signals only

WHY THIS EXISTS SEPARATELY FROM harvest_fandom.py. That script conflates
discovery with proof: it guesses a subdomain, asks the wiki its name, and
accepts a two-thirds token overlap as the answer. 29 of 48 seeds passed
that test, and the whole web-novel corpus is meant to be built on them.
One name matching one name is not proof, and the very first spot check
found a book that should never have passed:

    Apotheosis -> apotheosis.fandom.com
      sitename   "Apotheosis Wiki"      <- the only thing the old test read
      MAIN PAGE  "Voracity Wiki"        <- a different work entirely
      articles   5, active users 1
      search for "Apotheosis"           -> Voracity Wiki, Cosmology, Magi
      categories                        -> Blog posts, Candidates for
                                           deletion, Documentation templates

Every category is Fandom's own boilerplate. There is no novel there. The
old test could not tell, because it never looked past the sitename — which
is editable metadata, while the main page is what a community maintains.

THE FIVE STATES, and why UNAVAILABLE is the important one. The first pass
printed "REJECT ... site info failed (404)" and "REJECT ... no wiki found
(after retries)". Neither is a finding. A 404, an SSL timeout, a rate
limit — none of them means the wiki does not exist, and collapsing "we
could not check" into "it is not there" is the exact conflation this
pipeline exists to avoid. `unknown` is a third state everywhere else here.
Worse, it hides: a rejected title looks handled, and nobody re-checks a
list of books the tool said do not exist.

So a transport failure that survives its retries ends as UNAVAILABLE,
which is RETRYABLE, and never as NOT_FOUND, which is a conclusion. A
timeout hit this script's own development run; that is not hypothetical.

WHAT COUNTS AS PROOF. Five independent signals, each True / False /
None(=could not check), and a status derived from them by a table you can
read rather than a score you have to trust:

  name          the wiki calls itself this work, by sitename OR main page
  page          the work's own article exists inside the wiki, directly or
                through a redirect or a title-matching search hit
  content       the wiki is shaped like a wiki ABOUT A WORK — characters,
                chapters, volumes — not just Fandom's stock scaffolding
  activity      articles and edits above the level of an abandoned shell
  independent   a NON-Fandom source (Wikipedia) records a work by this
                name, and its lead does not describe a different one

The independent check needs its own title guard, or it becomes a second
source of false positives rather than a check on the first: searching
Wikipedia for "Apotheosis novel" returns `Là-bas (novel)`, an 1891
Huysmans novel about Satanism in France. A cross-check that accepts the
top hit would have "confirmed" the wrong book twice over.

Novel Updates, the redesign's first-named independent source, answers 403
from here. That is recorded as an unreached source rather than quietly
dropped — an unreached check must never read as a passed one.

CONTAINMENT IS ONE-WAY, kept from the old script because it was right: a
wiki about a BROADER work is not a wiki about this book. "Harry Potter and
the Methods of Rationality" is a fanfiction with its own wiki, and the
resolver offered `harrypotter`.

Output: data/akinator_fandom_proof.json — every signal's raw evidence, not
just the verdict, so a human can audit any single call. Read-only: this
script never edits akinator_fandom.json. Deciding what to do with a
downgrade is the owner's call, not a side effect of measuring.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import normalize  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUND_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_proof.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}

# Shared with harvest_fandom.py's notion of an insignificant word, plus the
# romanisation particles that make a Japanese title's tokens meaningless.
_STOP = {"the", "a", "an", "of", "and", "in", "my", "that", "with", "to",
         "is", "it", "for", "s", "kara", "no", "wiki", "wikia", "novel",
         "wiki的", "fandom"}

# A wiki with only these has no content about any work — they are what
# Fandom creates on an empty community. Matched as whole-word patterns so
# "Character images" (real) survives while "Images" (stock) does not.
_GENERIC_CATEGORY = re.compile(
    r"^(browse|community|content|images?|media|maintenance|site maintenance"
    r"|stubs?|templates?|users?|wiki|blog posts|candidates for deletion"
    r"|disambiguations?|help|policy|policies|copyright|articles"
    r"|add category|organi[sz]ation|.*templates?|.*template documentation"
    r"|pages? with .*|articles? with .*|.*needing .*|.*for deletion"
    r"|.*license.*|navigation|hidden categories|tracking categories)$",
    re.IGNORECASE)

# The category families a wiki about a NARRATIVE WORK reliably grows. One
# of these is worth more than fifty stock maintenance categories.
_WORK_CATEGORY = re.compile(
    r"\b(characters?|chapters?|volumes?|episodes?|arcs?|locations?"
    r"|abilities|powers?|factions?|organizations?|races?|species"
    r"|items?|artifacts?|weapons?|techniques?|skills?|magic"
    r"|terminology|glossary|timeline|events?|families|clans?"
    r"|antagonists?|protagonists?|villains?|deities|gods?)\b",
    re.IGNORECASE)

# Below either of these a wiki is a shell someone registered and left.
MIN_ARTICLES = 50
MIN_EDITS = 500

# The cross-check must confirm a WRITTEN WORK, not merely a name. Stripping
# a trailing disambiguator makes "Overlord" match "Overlord (film)" and
# "Delve" match "Delve (video game)" — and six of the seed titles are a
# single common word (Overlord, Apotheosis, Delve, Chrysalis, Worm), which
# is exactly where a bare title cannot settle anything. So the lead has to
# say what kind of thing it found.
_IS_WRITTEN_WORK = re.compile(
    r"\b(web ?novel|light ?novel|web ?serial|novel|novella|book|manhwa"
    r"|manhua|webtoon|serial(?:ised|ized)?|written by|short story"
    r"|literary work|fiction)\b", re.IGNORECASE)

# A lead that positively names a different medium is stronger evidence than
# a lead that merely fails to mention a book — worth telling apart.
# WESTERN MEDIA, TESTED BY BARE MENTION. An adaptation article names its
# medium somewhere in the lead and a novel's lead almost never says "film",
# so mention is a good enough proxy and its bluntness costs little.
_OTHER_MEDIA = (r"film|movie|video game|television series|tv series|album|song"
                r"|band|audio drama|board game|painting|opera")

# EASTERN MEDIA, TESTED BY CLAIM ONLY, and the split is measured rather than
# stylistic. These belong on the list — the harvest was accepting the anime
# article for "7th Time Loop" and labelling the novel with it. But putting
# them in the bare-mention test above rejected four real web novels
# (Overlord, Release That Witch, The Greatest Estate Developer, The
# Legendary Mechanic) for the crime of mentioning their own adaptations,
# which every web-novel lead does.
#
# The two cases separate cleanly on grammar, which is why this works:
#
#   anime article   "…is a Japanese anime series adapted from…"   a claim
#   novel article   "…is a light novel series. A manga adaptation" a mention
_EASTERN_MEDIA = r"anime|manga|manhwa|manhua|webtoon|donghua|drama cd"

# MENTIONS a medium. Kept broad because that is the right shape for naming
# what a page seems to be about — it is what fills `other_medium` in the
# evidence below, which is a description, not a verdict.
_IS_OTHER_MEDIUM = re.compile(r"\b(" + _OTHER_MEDIA + r")\b", re.IGNORECASE)

# CLAIMS to be a medium — anchored on the copula, the shape `_IS_PERSON`
# uses for "is/was a novelist".
#
# TRIED AS A REPLACEMENT FOR THE ABOVE AND REJECTED ON MEASUREMENT, kept
# only because the result is worth not rediscovering. The bare-mention test
# over-rejects: it drops "Omniscient Reader's Viewpoint", 1,340 words of
# exactly the prose we want, because the authors' pen name renders as
# "sing N song". Anchoring fixes that and was run over all 228 census wikis
# — it admitted "Frankenstein (1910 film)", "Jack Reacher (2012 film)",
# "The Dark Tower (film)" and "The Work and the Glory (film)" as the books
# themselves, seven adaptations among eleven newly accepted pages, because
# an adaptation's lead does not reliably use the copula either.
#
# In `harvest_fandom_text.py` precision is worth almost everything and
# recall very little, so the crude test stands there. Anything reaching for
# this one should first say why a false accept is cheaper than a false
# reject in ITS context, because here it was not.
_IS_OTHER_MEDIUM_CLAIM = re.compile(
    r"\b(?:is|was|are|were)\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,3}"
    r"(?:" + _OTHER_MEDIA + r"|" + _EASTERN_MEDIA + r")\b", re.IGNORECASE)


# ── plumbing ──────────────────────────────────────────────────────────────

class Unavailable(Exception):
    """A check that could not complete. NEVER a finding about the work."""


def _fetch(url: str, attempts: int = 3, timeout: int = 25) -> dict:
    """GET JSON, retrying transport failures. Raises Unavailable, not False.

    Every failure mode here — timeout, 404 on api.php, a rate limit, an SSL
    handshake that never finishes — is technical. The redesign puts all of
    them in one state on purpose: they are retryable, and NOT_FOUND is not.
    """
    last = ""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (400, 404) and attempt == 0:
                # A hard 4xx will not change on retry, but per the redesign
                # it is still UNAVAILABLE rather than a verdict about the
                # book: this subdomain may simply be the wrong guess.
                break
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:60]}"
        if attempt < attempts - 1:
            time.sleep(2 + attempt * 3)
    raise Unavailable(last or "unknown")


def _wiki_api(subdomain: str, **params) -> dict:
    params.setdefault("format", "json")
    return _fetch(f"https://{subdomain}.fandom.com/api.php?"
                  + urllib.parse.urlencode(params))


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text or "").split()
            if t not in _STOP and len(t) > 2}


def _title_match(want: str, got: str) -> bool:
    """Do these two name the same work?

    Two thirds of the significant words, not half — half let "Harry Potter
    and the Methods of Rationality" match the plain Harry Potter wiki,
    since {harry, potter} is exactly half of four and the two words it
    MISSES are the two that identify the book.

    Stopword removal is what lets the real cases through: the Lord of the
    Mysteries wiki calls its article "Lord of Mysteries", and both reduce
    to {lord, mysteries}.
    """
    w, g = _tokens(want), _tokens(got)
    if not w or not g:
        return False
    return len(w & g) * 3 >= len(w) * 2


_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def _same_work(want: str, got: str) -> bool:
    """Strict identity, for the cross-check only: the SAME significant words.

    `_title_match` is one-directional by design — it asks whether a wiki
    covers this work, and a wiki may legitimately be named for the series.
    Applied to an independent source that guarantees a best-guess answer,
    that looseness is a way to be wrong twice:

        "Against the Gods"  ->  wikipedia "A God Against the Gods"
                                a 1976 Allen Drury novel about Akhenaten

    {against, gods} is two thirds of {god, against, gods}, so the loose
    test passed a completely different book, and its lead would then have
    been read as corroboration. A title with EXTRA significant words names
    a different work — the same one-way-containment lesson the old
    harvester learned from Harry Potter, which I failed to carry over here.

    So the cross-check demands set equality after a trailing disambiguator
    is stripped: "Lord of Mysteries" == "Lord of the Mysteries" ({lord,
    mysteries} both ways, "the" being a stopword), while "A God Against the
    Gods" is not. Strict is right for a corroborating signal — a false
    negative here reads as "no independent record", which is honest, and
    nothing is ever built on this signal alone.
    """
    w = _tokens(_PARENTHETICAL.sub("", want))
    g = _tokens(_PARENTHETICAL.sub("", got))
    return bool(w) and w == g


# ── the five signals ──────────────────────────────────────────────────────

def signal_identity(subdomain: str, title: str) -> tuple[bool, dict]:
    """`name` + `activity`, both from one siteinfo call.

    Reads the MAIN PAGE title as well as the sitename. That is the check
    the old test lacked and the one that catches Apotheosis: sitename
    "Apotheosis Wiki" against a main page called "Voracity Wiki".
    """
    d = _wiki_api(subdomain, action="query", meta="siteinfo",
                  siprop="general|statistics")
    gen = (d.get("query") or {}).get("general") or {}
    stats = (d.get("query") or {}).get("statistics") or {}

    sitename = gen.get("sitename") or ""
    # `base` is the URL of the main page; its last path segment is the page
    # the community actually maintains, which sitename can drift away from.
    base = gen.get("base") or ""
    mainpage = urllib.parse.unquote(base.rsplit("/", 1)[-1]).replace("_", " ")

    by_sitename = _title_match(title, sitename)
    by_mainpage = _title_match(title, mainpage)
    articles = int(stats.get("articles") or 0)
    edits = int(stats.get("edits") or 0)

    evidence = {
        "sitename": sitename, "mainpage": mainpage,
        "articles": articles, "pages": int(stats.get("pages") or 0),
        "activeusers": int(stats.get("activeusers") or 0), "edits": edits,
        "name_by_sitename": by_sitename, "name_by_mainpage": by_mainpage,
        # Recorded because it is the Apotheosis signature: the wiki's own
        # two names disagree about what it is about.
        "names_disagree": bool(sitename and mainpage
                               and not _title_match(sitename, mainpage)),
        "activity_ok": articles >= MIN_ARTICLES and edits >= MIN_EDITS,
    }
    return (by_sitename or by_mainpage), evidence


def signal_page(subdomain: str, title: str) -> tuple[bool, dict]:
    """Does the work's own article exist inside the wiki?

    Three ways, in order of strength: the exact title, a redirect from it,
    or a search hit whose title names the same work. The novel's page is
    very often NOT at the exact English title — the Lord of the Mysteries
    wiki files it under "Lord of Mysteries" — so exact-title absence is not
    evidence of anything on its own.
    """
    ev: dict = {"exact": False, "redirect": None, "search_hits": [],
                "matched_hit": None}

    d = _wiki_api(subdomain, action="query", titles=title, redirects=1,
                  prop="info")
    q = d.get("query") or {}
    for r in q.get("redirects") or []:
        ev["redirect"] = r.get("to")
    for _pid, page in (q.get("pages") or {}).items():
        if "missing" not in page:
            ev["exact"] = True
            ev["resolved_title"] = page.get("title")
    if ev["exact"]:
        return True, ev

    d = _wiki_api(subdomain, action="query", list="search", srsearch=title,
                  srlimit=8, srprop="redirecttitle")
    hits = [h.get("title", "") for h in ((d.get("query") or {}).get("search") or [])]
    ev["search_hits"] = hits
    for h in hits:
        # A hit called "<Work> Wiki" is the main page, which proves the wiki
        # exists, not that the WORK has an article. Excluded deliberately.
        if re.search(r"\bwikia?\b", h, re.IGNORECASE):
            continue
        if _title_match(title, h):
            ev["matched_hit"] = h
            return True, ev
    return False, ev


def signal_content(subdomain: str, title: str) -> tuple[bool, dict]:
    """Is this shaped like a wiki about a narrative work, or a bare shell?

    Categories are the cheapest honest read on this. An abandoned Fandom
    community still has "Blog posts" and "Documentation templates"; only a
    real one has "Characters", "Abraham Family", "Above the Sequence".

    READ FROM TWO PLACES, because either alone is biased.

    `allcategories` enumerates ALPHABETICALLY, and a limit truncates the
    tail. That is not a neutral sample on a big wiki: Swallowed Star has
    3,111 articles, 69,066 edits and 14 active editors, and its first 200
    categories alphabetically are `26a51`, `26a52`, `26a53` ... — image
    tags. Judged on those alone it scored as having no work-shaped content
    at all, which is the opposite of true. An alphabetical head is not a
    sample; it is whatever sorts first.

    So the primary read is a RANDOM sample of real content pages and the
    categories they actually carry. A wiki about a novel puts its articles
    in "Characters" and "Chapters" no matter what letter they start with.
    `allcategories` stays as a second opinion, unioned in rather than
    trusted alone.
    """
    cats: list[str] = []
    ev: dict = {}

    # Random content pages (namespace 0) and their categories — unbiased by
    # title, which is the whole point.
    d = _wiki_api(subdomain, action="query", generator="random",
                  grnnamespace=0, grnlimit=12, prop="categories", cllimit=200)
    sampled = (d.get("query") or {}).get("pages") or {}
    for page in sampled.values():
        for c in page.get("categories") or []:
            cats.append((c.get("title") or "").split(":", 1)[-1])
    ev["sampled_pages"] = len(sampled)
    ev["sample_titles"] = [p.get("title", "") for p in list(sampled.values())[:6]]

    d = _wiki_api(subdomain, action="query", list="allcategories", aclimit=200)
    listed = [c.get("*", "") for c in
              ((d.get("query") or {}).get("allcategories") or [])]
    cats.extend(listed)

    cats = [c.strip() for c in dict.fromkeys(cats) if c and c.strip()]
    specific = [c for c in cats if not _GENERIC_CATEGORY.match(c)]
    work_shaped = [c for c in specific if _WORK_CATEGORY.search(c)]
    ev.update({
        "categories_seen": len(cats),
        "listed_alphabetically": len(listed),
        "specific": len(specific),
        "work_shaped": len(work_shaped),
        "work_shaped_sample": work_shaped[:8],
        "specific_sample": specific[:10],
    })
    return bool(work_shaped) and len(specific) >= 10, ev


def signal_independent(title: str) -> tuple[bool, dict]:
    """A NON-Fandom source that records a work by this name.

    Wikipedia, because Novel Updates answers 403 from here and this repo
    already fetches Wikipedia leads elsewhere. Its value is that it carries
    exactly what the cross-check is for — the author, the original title
    and the original language:

        Lord of Mysteries (Chinese: 诡秘之主) is a Chinese web novel
        written by Cuttlefish That Loves Diving...

    THE GUARD MATTERS MORE THAN THE LOOKUP. Wikipedia search always returns
    its best guess, and for "Apotheosis novel" that is `Là-bas (novel)`, an
    1891 French novel about Satanism. Accepting a top hit unchecked would
    turn the independent check into a second way to be wrong, so the hit's
    TITLE must name the same work before its lead is read at all.
    """
    q = urllib.parse.urlencode({
        "action": "query", "list": "search", "format": "json",
        "srsearch": f"{title} novel", "srlimit": 5})
    d = _fetch("https://en.wikipedia.org/w/api.php?" + q)
    hits = [h.get("title", "") for h in ((d.get("query") or {}).get("search") or [])]
    ev: dict = {"source": "wikipedia", "hits": hits, "matched": None,
                "lead": "", "novelupdates": "403 from this network; unreached"}

    match = next((h for h in hits if _same_work(title, h)), None)
    if not match:
        # Recorded so an audit can see WHAT was rejected, not just that
        # nothing matched — the near-misses are where a real alternate
        # title would show up.
        ev["rejected_hits"] = hits[:5]
        return False, ev
    ev["matched"] = match

    q2 = urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "exintro": 1,
        "explaintext": 1, "format": "json", "titles": match})
    d2 = _fetch("https://en.wikipedia.org/w/api.php?" + q2)
    pages = (d2.get("query") or {}).get("pages") or {}
    lead = next(iter(pages.values()), {}).get("extract", "") or ""
    ev["lead"] = " ".join(lead.split())[:400]

    # A matching title is not yet corroboration. Stripping the trailing
    # disambiguator is what makes the match work at all ("Mother of
    # Learning (web serial)"), and the same stripping makes "Overlord"
    # match "Overlord (film)". Six seed titles are one common word, so the
    # lead is the only thing that can say WHICH Overlord this is.
    head = ev["lead"][:300]
    written = bool(_IS_WRITTEN_WORK.search(head))
    other = _IS_OTHER_MEDIUM.search(head)
    ev["is_written_work"] = written
    ev["other_medium"] = other.group(0).lower() if other else None
    if not written:
        ev["rejected_reason"] = (
            f"lead describes a {ev['other_medium']}, not a written work"
            if other else "lead does not describe a written work")
        return False, ev
    return True, ev


# ── verdict ───────────────────────────────────────────────────────────────

def classify(sig: dict) -> tuple[str, str]:
    """Signals -> one of the redesign's five states, by a readable table.

    THE STATES ARE ABOUT IDENTITY — is this wiki about this work — and
    nothing else. `activity` and `independent` are reported alongside
    rather than folded in, because they answer different questions:
    `against-the-gods` is unmistakably the right wiki (it holds articles on
    Yun Che and Xia Qingyue, the novel's own characters) and is also 28
    articles with zero active users. "Right wiki" and "wiki worth
    harvesting" are two findings, and collapsing them loses one.

    UNAVAILABLE wins over everything: a wiki we could not interrogate has
    told us nothing, and reporting a verdict on it would be inventing one.

    NOT_FOUND IS THE STATE TO BE MOST CAREFUL WITH, because it is the only
    conclusion here — the other four all invite another look. An earlier
    cut of this table dropped through to it whenever the work's own article
    was missing, and so called `against-the-gods` NOT_FOUND on evidence
    that included a matching name AND the novel's characters. That is the
    same over-confident negative the redesign was written to end, rebuilt
    one layer up. It now requires that NOTHING identified the wiki.
    """
    if sig["name"] is None or sig["page"] is None or sig["content"] is None:
        return "UNAVAILABLE", "one or more wiki checks could not complete"

    name, page, content = sig["name"], sig["page"], sig["content"]

    if name and page and content:
        return "CONFIRMED", "named for the work, holds its article, and is shaped like a wiki about it"
    if name and page:
        return "LIKELY", "named for the work and holds its article, but little content around it"
    if name and content:
        return "LIKELY", "named for the work and full of its material, but no article for the work itself"
    if name:
        # The Apotheosis shape: NAMED after the work and knowing nothing
        # about it. Not NOT_FOUND — the name is real evidence and a human
        # should look — but nothing may be built on it.
        return "AMBIGUOUS", "named for the work but holds no article about it and no material around it"
    if page or content:
        return "AMBIGUOUS", "holds matching material but the wiki is named for something else"
    return "NOT_FOUND", "nothing identifies this wiki with this work"


def prove(title: str, subdomain: str, do_independent: bool = True) -> dict:
    """Every signal for one (title, wiki). Never raises."""
    sig: dict = {"name": None, "page": None, "content": None,
                 "activity": None, "independent": None}
    ev: dict = {}
    unavailable: list[str] = []

    try:
        ok, e = signal_identity(subdomain, title)
        sig["name"], sig["activity"] = ok, e["activity_ok"]
        ev["identity"] = e
    except Unavailable as exc:
        unavailable.append(f"siteinfo: {exc}")
        ev["identity"] = {"unavailable": str(exc)}

    # Only worth asking the rest if the wiki answered at all.
    if sig["name"] is not None:
        for key, fn in (("page", signal_page), ("content", signal_content)):
            try:
                ok, e = fn(subdomain, title)
                sig[key], ev[key] = ok, e
            except Unavailable as exc:
                unavailable.append(f"{key}: {exc}")
                ev[key] = {"unavailable": str(exc)}

    if do_independent:
        try:
            ok, e = signal_independent(title)
            sig["independent"], ev["independent"] = ok, e
        except Unavailable as exc:
            # An unreachable cross-check is NOT a failed one, and must not
            # drag a wiki down. Left as None; classify() never requires it.
            unavailable.append(f"independent: {exc}")
            ev["independent"] = {"unavailable": str(exc)}

    status, why = classify(sig)
    # The question the corpus actually needs answered. Identity and
    # usefulness are separate: a wiki can be provably the right one and
    # still hold nothing worth extracting, and only this combination means
    # "a web novel the game could learn about from here".
    harvestable = status in ("CONFIRMED", "LIKELY") and sig["activity"] is True
    return {"title": title, "subdomain": subdomain, "status": status,
            "why": why, "harvestable": harvestable, "signals": sig,
            "unavailable": unavailable, "evidence": ev}


ICON = {"CONFIRMED": "++", "LIKELY": " +", "AMBIGUOUS": " ?",
        "NOT_FOUND": " -", "UNAVAILABLE": " !"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--found", default=FOUND_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--only", action="append", default=[],
                    help="prove only these titles (repeatable)")
    ap.add_argument("--no-independent", action="store_true",
                    help="skip the Wikipedia cross-check")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    with open(args.found, encoding="utf-8") as fh:
        found = json.load(fh)
    items = [(t, v["subdomain"]) for t, v in found.items()
             if not args.only or t in args.only]

    print(f"Proving {len(items)} wiki(s) under the redesign's acceptance "
          f"rules{'' if not args.no_independent else ' (wiki signals only)'}\n")
    print(f"   {'title':<42} {'status':<12} name page cont acty indp")
    print("   " + "-" * 78)

    results = []
    for title, sub in items:
        r = prove(title, sub, do_independent=not args.no_independent)
        results.append(r)
        s = r["signals"]
        flag = lambda v: " Y  " if v is True else (" n  " if v is False else " ?  ")  # noqa: E731
        print(f"{ICON[r['status']]} {title[:42]:<42} {r['status']:<12}"
              f"{flag(s['name'])}{flag(s['page'])}{flag(s['content'])}"
              f"{flag(s['activity'])}{flag(s['independent'])}")
        time.sleep(args.delay)

    tally: dict[str, int] = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1

    print("\n" + "=" * 82)
    for state in ("CONFIRMED", "LIKELY", "AMBIGUOUS", "NOT_FOUND", "UNAVAILABLE"):
        if tally.get(state):
            print(f"  {state:<12} {tally[state]:>3}")
    print(f"  {'total':<12} {len(results):>3}   (was: 29 'found' under the old test)")

    # The number that decides whether the Fandom path is worth building.
    harvestable = [r for r in results if r["harvestable"]]
    thin = [r for r in results
            if r["status"] in ("CONFIRMED", "LIKELY") and not r["harvestable"]]
    print(f"\n  HARVESTABLE  {len(harvestable):>3}   identity proven AND "
          f"more than {MIN_ARTICLES} articles / {MIN_EDITS} edits")
    if thin:
        print(f"  too thin     {len(thin):>3}   the right wiki, with almost "
              f"nothing in it:")
        for r in thin:
            i = r["evidence"].get("identity", {})
            print(f"                   {r['title'][:38]:<38} "
                  f"{i.get('articles', '?')} articles, "
                  f"{i.get('edits', '?')} edits, "
                  f"{i.get('activeusers', '?')} active users")

    downgraded = [r for r in results if r["status"] != "CONFIRMED"]
    if downgraded:
        print("\nNot confirmed — each needs a human look before anything is "
              "built on it:")
        for r in downgraded:
            print(f"  [{r['status']}] {r['title']}")
            print(f"      {r['why']}")
            for u in r["unavailable"]:
                print(f"      could not check -> {u}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"proved": results}, fh, ensure_ascii=False, indent=1)
    print(f"\n  -> {args.out}")
    print("  akinator_fandom.json is UNCHANGED; what to do with a downgrade "
          "is the owner's call.")


if __name__ == "__main__":
    main()
