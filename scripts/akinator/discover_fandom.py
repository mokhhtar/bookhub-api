"""
scripts/akinator/discover_fandom.py — stage 1 of the Fandom redesign:
DISCOVERY. Finds CANDIDATE wikis; never decides. Every candidate is handed
to `prove_fandom.py`'s `prove()`, unmodified, for the actual verdict.

    python scripts/akinator/discover_fandom.py                # the 19
    python scripts/akinator/discover_fandom.py --only "Worm"
    python scripts/akinator/discover_fandom.py --limit 4       # sample first

WHY THIS EXISTS SEPARATELY. `harvest_fandom.py` (the old script) conflates
discovery and proof — it guesses subdomains from the ORIGINAL TITLE ALONE
and checks each with a weak two-thirds-token-overlap test. It resolved 29
of 48 seeds; the other 19 (Omniscient Reader's Viewpoint, The Wandering
Inn, Worm, A Practical Guide to Evil, and 15 more) are not obscure — they
have large, active wikis — they were simply never found, because a book's
Fandom wiki is very often named for an ABBREVIATION, an ALTERNATE
TRANSLATION, or the ORIGINAL-LANGUAGE title, none of which the old script
ever tried.

THE OWNER'S REDESIGN ASKED FOR "SEARCH BY MANY NAMES" VIA AN EXTERNAL
SEARCH ENGINE PLUS FANDOM'S OWN COMMUNITY SEARCH. Both were tested live
before writing a line of this file, and neither held up:

  - Fandom's cross-wiki search endpoints (`Search/CrossWiki`,
    `SearchSuggestions/List`) answer 301 -> 404. Dead or moved.
  - DuckDuckGo (Lite or the full HTML page `tools/fandom.py` already
    scrapes as its last resort) answered HTTP 202 with an "anomaly"
    (rate-limit) page after the SECOND request in testing. Not viable as
    a base layer for the dozens of name-variant queries this needs.
  - Google Custom Search (`tools/fandom.py`'s Tier 3) needed
    GOOGLE_CUSTOM_SEARCH_API_KEY / GOOGLE_SEARCH_CX_ID. The owner obtained
    both, correctly configured (API enabled, key restrictions fixed) —
    and it 403'd anyway: "This project does not have the access to Custom
    Search JSON API." Confirmed against Google's own documentation
    (developers.google.com/custom-search/v1/overview): the API has been
    **closed to new customers since January 2026**, existing customers
    get until January 1, 2027. A key created today can never work here,
    regardless of Console configuration. Not a setup mistake — chased
    for three rounds of Console changes before this was verified.

So "many names" is supplied here as a small HAND-CURATED overlay
(`data/fandom_alt_titles.json`) instead of auto-discovered — 19 titles is
cheap to curate once, and it is more honest than trusting an unverifiable
search API. This is exactly the "guessing is a discovery tactic, not a
source of truth" principle the redesign note already established for
subdomain-guessing; it applies just as well to name-guessing.

FOUR CANDIDATE SOURCES, IN ORDER, for EVERY name (seed title + every
alias in the overlay):

  1. Subdomain guessing — `harvest_fandom.py`'s own `candidate_subdomains`,
     reused unmodified, applied to every name rather than just the seed
     title. Cheap: one siteinfo call per guess, no external search.
  2. Brave Search — `"{name}" site:fandom.com`, live-verified working
     (2,000 req/month free tier) after Google CSE turned out to be a dead
     end. Found `the-kings-avatar.fandom.com` for "The King's Avatar" on
     the first query — a hyphenation `candidate_subdomains()` cannot
     produce (it guesses `the-king-s-avatar`, squashing the possessive's
     apostrophe into its own hyphen). Optional: BRAVE_SEARCH_API_KEY
     unset skips this source with no candidates and no failure recorded,
     so its absence never looks like every query failed.
  3. Wikipedia — search for the name, and ONLY IF THE HIT PASSES
     `prove_fandom.py`'s `_same_work` (imported directly, not
     reimplemented) is its raw wikitext scanned for a `*.fandom.com`
     link. `_same_work` matters here more than it did as a cross-check
     signal in stage 2: live testing during planning found it accept the
     wrong article on a loose match — "The Wandering Inn" (a Nebula-
     nominated web serial) surfaced "Wandering Witch: The Journey of
     Elaina" (an unrelated anime) as the top hit for a looser query.
     Strict identity is not optional here.
  4. `tools/fandom.py`'s `resolve_fandom_subdomain` — the existing 5-tier
     cascade, UNCHANGED, exactly the fallback `harvest_fandom.py` already
     uses. No edits to that function; its production blast radius
     (/fandom/resolve, /fandom/universe, book_data.py, quiz.py,
     summary.py) is untouched by this file. Its own Google CSE tier is
     dead for the same reason as above and will simply keep failing
     silently, same as it already did before Brave was added.

Every candidate that reaches this far — regardless of source — is proved,
not accepted, via `prove_fandom.py.prove()`. This script never writes
CONFIRMED/LIKELY itself; it only ever proposes candidates for that
function to judge, then records what it decided.

UNAVAILABLE, NOT A FIFTH TIME. Two of eight live calls dropped mid-flight
while testing this design on this network. A request that fails to
complete records UNAVAILABLE and is retried on the next run — never
NOT_FOUND, which is a conclusion this script is not entitled to reach
from a timeout.

Output: data/akinator_fandom_discovery.json — every title's outcome,
including which name and which of the 3 sources produced the winning
candidate (audit trail). Titles that reach CONFIRMED here are ALSO
appended to data/akinator_fandom.json (same shape as harvest_fandom.py's
output, `{subdomain, why}`) so downstream tooling sees one merged file —
but LIKELY/AMBIGUOUS/NOT_FOUND/UNAVAILABLE are reported only, never
written there, matching how prove_fandom.py itself never edits that file
for a downgrade.

This script never touches tools/fandom.py's FANDOM_WIKIS map, which is
live production data consulted by the summary tool's /search endpoint.
See the closing report's "FANDOM_WIKIS proposal" section: a draft entry
per CONFIRMED title, for a human to review and paste in — not an
automatic edit to a file with that much blast radius.
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

from harvest_fandom import candidate_subdomains          # noqa: E402
from prove_fandom import Unavailable, _fetch, _same_work, prove  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEED_PATH = os.path.join(REPO_ROOT, "data", "fandom_seed.json")
FOUND_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
ALT_PATH = os.path.join(REPO_ROOT, "data", "fandom_alt_titles.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_discovery.json")

HEADERS = {"User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)"}


def names_for(title: str, alts: dict) -> list[str]:
    """The seed title plus every curated alias, order preserved, deduped."""
    out = [title]
    for a in alts.get(title, []):
        if a not in out:
            out.append(a)
    return out


def try_guessing(names: list[str]) -> list[tuple[str, str]]:
    """(name, subdomain) pairs worth proving, from cheap string transforms.

    No siteinfo call happens here -- candidate_subdomains() only builds
    strings. prove() does the actual network check, once per candidate,
    not twice.
    """
    out, seen = [], set()
    for n in names:
        for sub in candidate_subdomains(n):
            if sub not in seen:
                seen.add(sub)
                out.append((n, sub))
    return out


def try_wikipedia(names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, subdomain) pairs found via a Wikipedia external link, plus a
    list of transport failures (for the UNAVAILABLE record)."""
    candidates: list[tuple[str, str]] = []
    unavailable: list[str] = []
    for n in names:
        try:
            q = urllib.parse.urlencode({
                "action": "query", "list": "search", "format": "json",
                "srsearch": f"{n} novel", "srlimit": 5})
            d = _fetch("https://en.wikipedia.org/w/api.php?" + q)
        except Unavailable as exc:
            unavailable.append(f"wikipedia search {n!r}: {exc}")
            continue
        hits = [h.get("title", "") for h in
                ((d.get("query") or {}).get("search") or [])]
        match = next((h for h in hits if _same_work(n, h)), None)
        if not match:
            continue
        try:
            q2 = urllib.parse.urlencode({
                "action": "parse", "page": match, "prop": "wikitext",
                "format": "json", "section": 0})
            d2 = _fetch("https://en.wikipedia.org/w/api.php?" + q2)
        except Unavailable as exc:
            unavailable.append(f"wikipedia wikitext {match!r}: {exc}")
            continue
        text = (((d2.get("parse") or {}).get("wikitext") or {})
                .get("*") or "")
        for sub in re.findall(r"([a-zA-Z0-9_-]+)\.fandom\.com", text):
            candidates.append((f"{n} (via Wikipedia:{match})", sub.lower()))
    # dedupe on subdomain, keep first (order = discovery preference)
    seen, uniq = set(), []
    for n, sub in candidates:
        if sub not in seen:
            seen.add(sub)
            uniq.append((n, sub))
    return uniq, unavailable


BRAVE_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def try_brave(names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, subdomain) pairs from Brave Search's `site:fandom.com`.

    Added 2026-08-14, after Google Custom Search turned out to be a dead
    end for a NEW key -- Google closed it to new customers in Jan 2026
    (developers.google.com/custom-search/v1/overview), and the 403 this
    project hit chasing Console settings was never a config problem.
    Brave's free tier (2,000 req/month, verified live before writing this
    function) found `the-kings-avatar.fandom.com` for "The King's
    Avatar" on the first try -- a hyphenation candidate_subdomains()
    never produces (it guesses "the-king-s-avatar", squashing the
    possessive's apostrophe into its own hyphen; the real wiki drops it).

    Results are NOT trusted as-is -- same as every other source, each
    candidate still goes through prove()'s five signals. A search engine
    finding a fandom.com URL only means the URL exists, not that it is
    about this specific book (Chrysalis/Delve-shaped ambiguity applies
    here as much as anywhere).

    No key configured -- BRAVE_KEY empty -- returns no candidates and no
    failure; this source is optional, and its absence must not look like
    every query failed.
    """
    if not BRAVE_KEY:
        return [], []
    candidates: list[tuple[str, str]] = []
    unavailable: list[str] = []
    for n in names:
        q = urllib.parse.urlencode({"q": f'"{n}" site:fandom.com', "count": 6})
        url = f"{BRAVE_ENDPOINT}?{q}"
        last = ""
        hits = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={
                    **HEADERS, "Accept": "application/json",
                    "X-Subscription-Token": BRAVE_KEY})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    hits = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code == 429:            # free tier is 1 req/s
                    time.sleep(2 + attempt * 2)
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last = type(exc).__name__
            if attempt < 2:
                time.sleep(1.5)
        if hits is None:
            unavailable.append(f"brave search {n!r}: {last or 'unknown'}")
            continue
        for r in (hits.get("web") or {}).get("results") or []:
            host = urllib.parse.urlparse(r.get("url", "")).hostname or ""
            m = re.match(r"([a-z0-9_-]+)\.fandom\.com$", host)
            if m:
                candidates.append((f"{n} (via Brave)", m.group(1).lower()))
        time.sleep(1.1)                        # stay under 1 req/s
    seen, uniq = set(), []
    for n, sub in candidates:
        if sub not in seen:
            seen.add(sub)
            uniq.append((n, sub))
    return uniq, unavailable


def try_resolver(title: str) -> tuple[str, str] | None:
    """The existing, unmodified 5-tier cascade -- last resort, same as
    harvest_fandom.py's own fallback. Any failure here is swallowed the
    same way harvest_fandom.py already treats it: no candidate, not an
    UNAVAILABLE record of its own, since this tier's own internal retries
    and fail-open behavior are pre-existing, documented elsewhere."""
    try:
        from tools.fandom import resolve_fandom_subdomain
        sub = resolve_fandom_subdomain(title)
        return (f"{title} (via resolver)", sub) if sub else None
    except Exception:  # noqa: BLE001
        return None


# Quality of a verdict, worst to best. UNAVAILABLE is not a decision at
# all -- it must never block trying the next source, which is exactly the
# bug the first full run caught: 12 of 17 titles never reached the
# resolver tier because a single UNAVAILABLE guess (a wrong subdomain
# guess 404ing, not a real network failure) made `best` non-None, and
# the old code's `if not best:` gate treated ANY dict, including an
# UNAVAILABLE one, as "already decided." A wrong guess is not a decision.
_RANK = {"UNAVAILABLE": 0, "NOT_FOUND": 1, "AMBIGUOUS": 2, "LIKELY": 3,
        "CONFIRMED": 3}


def discover(title: str, alts: dict, delay: float) -> dict:
    """Try every source; keep the BEST verdict seen, not the first one.
    Stops early only on CONFIRMED/LIKELY. The resolver tier always gets a
    turn unless guess+wikipedia already reached CONFIRMED/LIKELY -- an
    UNAVAILABLE or NOT_FOUND result from an earlier source must not skip
    it, since the resolver is a genuinely different mechanism (Wikidata /
    Google CSE / DuckDuckGo), not a repeat of the same guess."""
    names = names_for(title, alts)
    attempts: list[dict] = []
    unavailable: list[str] = []
    best: dict | None = None

    def consider(source: str, name: str, sub: str) -> bool:
        """Prove one candidate, fold it into `best`. Returns True if this
        reached CONFIRMED/LIKELY (caller should stop trying more)."""
        nonlocal best
        try:
            result = prove(title, sub, do_independent=True)
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"prove({title!r}, {sub!r}): {exc}")
            return False
        attempts.append({"source": source, "tried_as": name,
                         "subdomain": sub, "status": result["status"]})
        if best is None or _RANK[result["status"]] > _RANK[best["status"]]:
            best = {**result, "discovered_via": source, "tried_as": name}
        return result["status"] in ("CONFIRMED", "LIKELY")

    for name, sub in try_guessing(names):
        if consider("guess", name, sub):
            best["attempts"], best["unavailable"] = attempts, unavailable
            return best
        time.sleep(delay)

    # Brave before Wikipedia: it is the source that actually found the 11
    # titles guessing and Wikipedia both missed on 2026-08-14, and its
    # 2,000/month free tier easily covers this list. Skipped for free
    # (no candidates, no failure recorded) when BRAVE_SEARCH_API_KEY is
    # unset, so this stays optional rather than load-bearing.
    brave_cands, brave_unavail = try_brave(names)
    unavailable.extend(brave_unavail)
    for name, sub in brave_cands:
        if consider("brave", name, sub):
            best["attempts"], best["unavailable"] = attempts, unavailable
            return best
        time.sleep(delay)

    wiki_cands, wiki_unavail = try_wikipedia(names)
    unavailable.extend(wiki_unavail)
    for name, sub in wiki_cands:
        if consider("wikipedia", name, sub):
            best["attempts"], best["unavailable"] = attempts, unavailable
            return best
        time.sleep(delay)

    # Always reached unless guess/wikipedia already confirmed -- an
    # UNAVAILABLE-only or NOT_FOUND-only result from either must not skip
    # this genuinely different mechanism.
    resolver_hit = try_resolver(title)
    if resolver_hit:
        name, sub = resolver_hit
        consider("resolver", name, sub)

    if best is None:
        return {"title": title, "status": "UNAVAILABLE" if unavailable
                else "NOT_FOUND",
                "why": "no candidate reached a decision" if not unavailable
                else "; ".join(unavailable[:3]),
                "attempts": attempts, "unavailable": unavailable}

    best["attempts"], best["unavailable"] = attempts, unavailable
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", default=SEED_PATH)
    ap.add_argument("--found", default=FOUND_PATH)
    ap.add_argument("--alts", default=ALT_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N titles (a small live sample first)")
    ap.add_argument("--delay", type=float, default=1.2)
    args = ap.parse_args()

    with open(args.seed, encoding="utf-8") as fh:
        seed = json.load(fh)
    with open(args.found, encoding="utf-8") as fh:
        found = json.load(fh)
    alts = {}
    if os.path.exists(args.alts):
        with open(args.alts, encoding="utf-8") as fh:
            alts = json.load(fh)

    targets = [t for t in seed if t not in found]
    if args.only:
        targets = [t for t in targets if t in args.only]
    if args.limit:
        targets = targets[:args.limit]

    print(f"Discovering {len(targets)} of {len(seed)-len(found)} unresolved "
          f"titles ({len(seed)} seeds, {len(found)} already resolved)\n")

    results = []
    for title in targets:
        r = discover(title, alts, args.delay)
        results.append(r)
        icon = {"CONFIRMED": "++", "LIKELY": " +", "AMBIGUOUS": " ?",
                "NOT_FOUND": " -", "UNAVAILABLE": " !"}[r["status"]]
        via = r.get("discovered_via", "-")
        sub = r.get("subdomain", "-")
        print(f"{icon} {title[:40]:<40} {r['status']:<12} "
              f"via={via:<10} subdomain={sub}")

    tally: dict[str, int] = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print("\n" + "=" * 78)
    for state in ("CONFIRMED", "LIKELY", "AMBIGUOUS", "NOT_FOUND", "UNAVAILABLE"):
        if tally.get(state):
            print(f"  {state:<12} {tally[state]:>3}")

    newly_found = [r for r in results if r["status"] == "CONFIRMED"]
    if newly_found:
        for r in newly_found:
            found[r["title"]] = {"subdomain": r["subdomain"],
                                 "why": f"[discovered via {r['discovered_via']}] "
                                        f"{r.get('why', '')}"[:120]}
        with open(args.found, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False, indent=1)
        print(f"\n{len(newly_found)} title(s) appended to {args.found} "
              f"(CONFIRMED only)")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"results": results}, fh, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")

    proposals = [r for r in results if r["status"] == "CONFIRMED"]
    if proposals:
        print("\n--- FANDOM_WIKIS proposal (review before pasting into "
              "tools/fandom.py) ---")
        for r in proposals:
            key = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:24]
            print(f'    "{key}": {{"subdomain": "{r["subdomain"]}", '
                  f'"aliases": ["{r["title"].lower()}"]}},')

    unresolved = [r for r in results
                  if r["status"] in ("LIKELY", "AMBIGUOUS", "NOT_FOUND",
                                     "UNAVAILABLE")]
    if unresolved:
        print("\nNot auto-written — needs a human look:")
        for r in unresolved:
            print(f"  [{r['status']}] {r['title']}: {r.get('why', '')[:70]}")


if __name__ == "__main__":
    main()
