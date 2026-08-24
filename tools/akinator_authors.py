"""
tools/akinator_authors.py — the admin page's Authors tab, write side.

WHAT AN AUTHOR PROFILE IS HERE. Not a new record. `AuthorIndex` in
`scripts/akinator/authors.py` already decides who an author is, and every
fact the game knows about one is recomputed from Wikidata at each rebuild.
This module writes an OVERLAY on that — `games/data/akinator/
author_overrides.json`, keyed by exactly the identity the build itself
computes (an Open Library author key, or `name:{merge_key}`) — and nothing
else. `scripts/akinator/author_overrides.py` owns the format and the key
rule; this file imports it rather than restating it, because a hand-set
fact whose key does not line up with what the build recomputes is not a
correction, it is a file that silently does nothing.

TWO ENDPOINTS, AND THE FIRST ONE WRITES NOTHING.

    resolve   read-only    Open Library author search by NAME (genuinely
                           new — `book_data.py` only ever fetched an author
                           by an already-known key, confirmed by grep
                           across both repos), the single-author version of
                           `harvest_authors.py`'s P648 Wikidata join, the
                           `name:` key the build would compute, and any
                           override entry that already claims it.
    save      instant,     one `_commit_files` write of the overlay. Facts
              next full    take effect at the NEXT FULL REBUILD only, like
              rebuild      every other fact that feeds a matrix bit.
              for facts

WHY A CANDIDATE LIST AND NOT A MATCH. Searching Open Library for "John
Gillow" returns the textile historian (OL37837A, 19 works, top work "Indian
textiles" — three of which this game ships) AND a different John Gillow who
died in 1877. "Colleen Hoover" returns four records, one of them a French
translator's name glued to hers. A name is not an identifier, which is the
same reason `harvest_authors_bysearch.py` refuses to accept a Wikidata
candidate unless exactly one qualifies. Here there is a human at the
screen, so the honest design is to show them the list.

UNAVAILABLE IS NOT NOT_FOUND, again. A Wikidata timeout says nothing about
the author; it is reported as `wikidata_error` with `wikidata: null`, never
as an absence of facts. This repo has conflated those two four times, and
Wikidata's query service has not been proven reachable from Render's
datacenter IP at all — see CLAUDE.md on Gutendex, which works locally and
403s from Render.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from tools.akinator_admin import (                                # noqa: E402
    ARTIFACT_DIR,
    _commit_files,
    _dump,
    _get_json,
    _require_admin,
)

# Importing tools.akinator_admin above is what puts scripts/akinator on
# sys.path, so these two resolve. Stated rather than repeated: a second path
# insert here would hide the fact that the guard and the path come from the
# same place.
import author_overrides as AO                                     # noqa: E402
from author_traits import AUTHOR_QUESTIONS, traits_for            # noqa: E402

log = logging.getLogger("bookhub-api.akinator_authors")

AUTHOR_OVERRIDES_PATH = f"{ARTIFACT_DIR}/author_overrides.json"
AUTHORS_PATH = f"{ARTIFACT_DIR}/authors.json"

OL_AUTHOR_SEARCH = "https://openlibrary.org/search/authors.json"
UA = "Litheca/1.0 (https://litheca.com; hello@litheca.com)"

admin_router = APIRouter(prefix="/akinator/admin/authors", tags=["akinator"],
                         dependencies=[Depends(_require_admin)])


# ── reading the shipped author list ──────────────────────────────────────

def _shipped() -> tuple[dict, list[list[str]], dict[str, str]]:
    """(id -> exported row, per-book author id lists) from the live game.

    TWO THINGS `authors.json` DOES NOT TELL YOU on its own, both measured
    against the live artifacts rather than assumed:

    1. It only LISTS authors with 2+ books — the askable set, 795 of them.
       Its `books` array references 3,939, and a one-book author is
       precisely the case this tab exists for. So the id list comes from
       `books`; the exported row is a bonus when there is one.

    2. It is STALE by however many rows `akinator_sync.append_book_row` has
       appended since the last full rebuild. That function rewrites
       books.json, matrix.bin and meta.json and leaves authors.json alone —
       measured at 6 rows behind on 2026-08-24, and every one of them a
       manually added `/site/` book, which is exactly the population whose
       author identity is fragile. Those rows are padded back in here from
       books.json: a synced row always carries `author_key: []`, so its
       `name:{merge_key}` key IS its identity and `a` is the name it was
       computed from.
    """
    data, _ = _get_json(AUTHORS_PATH, None)
    if not isinstance(data, dict):
        return {}, [], {}
    listed = {a["id"]: a for a in (data.get("authors") or [])
              if isinstance(a, dict) and a.get("id")}
    per_book = [row if isinstance(row, list) else []
                for row in (data.get("books") or [])]

    books, _ = _get_json(f"{ARTIFACT_DIR}/books.json", None)
    if isinstance(books, list) and len(books) > len(per_book):
        for row in books[len(per_book):]:
            nk = AO.name_key((row or {}).get("a") or "")
            per_book.append([nk] if nk else [])

    # id -> the name that id is known by. books.json's `a` names the FIRST
    # author of a row and nobody else, so a co-author gets no name here —
    # claiming the lead author's would put one person's name on another's
    # id. The exported row fills in whoever has 2+ books.
    names: dict[str, str] = {}
    if isinstance(books, list):
        for i, ids in enumerate(per_book):
            if ids and i < len(books) and ids[0] not in names:
                a = (books[i] or {}).get("a") or ""
                if a:
                    names[ids[0]] = a
    for author_id, row in listed.items():
        names.setdefault(author_id, row.get("n") or "")
    return listed, per_book, names


def _book_count(author_id: str, listed: dict, per_book: list[list[str]]) -> int:
    """Counted from the rows, never read off the exported `c`.

    The exported count is what the LAST FULL REBUILD saw, and it was wrong
    here by exactly the appended rows: John Gillow shows `c: 2` while three
    of his books ship, because "African textiles" was synced in afterwards.
    Reading `c` would have made this tab quietly authoritative about a
    number it had no way to know was stale. `c` is kept for the display
    name only.
    """
    if per_book:
        return sum(1 for ids in per_book if author_id in ids)
    row = listed.get(author_id)
    return row["c"] if row and isinstance(row.get("c"), int) else 0


# ── POST /akinator/admin/authors/resolve ─────────────────────────────────

class ResolveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    ol_key: str | None = Field(default=None, max_length=24)


def _fetch(url: str, timeout: int, attempts: int = 3,
           headers: dict | None = None) -> dict:
    """One JSON GET, retried immediately rather than backed off.

    The failure mode this exists for is a connection that never
    establishes, not a server pushing back — `harvest_authors_bysearch.py`
    measured two requests in three timing out at ~22s while a connected one
    answered in 0.8s, and sleeping does not make a dropped connection more
    likely to succeed. Caught it again building this: the Open Library
    search timed out on one run of the smoke test and answered in 0.4s on
    the next. One attempt would make an admin tool look like it has no
    data whenever the network hiccups.
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:                                  # noqa: BLE001
            last = exc
    raise last if last else RuntimeError("no attempt made")


def _ol_search(name: str, limit: int = 6) -> list[dict]:
    """Open Library's author search. Raises nothing the caller can't see."""
    url = OL_AUTHOR_SEARCH + "?" + urllib.parse.urlencode(
        {"q": name, "limit": limit})
    data = _fetch(url, 20)
    out = []
    for doc in (data.get("docs") or [])[:limit]:
        key = doc.get("key") or ""
        # OL returns the bare key here ("OL37837A"), unlike the /authors/OL…A
        # form on a work record. Normalised so what comes back is always the
        # thing author_overrides keys on.
        key = key.rsplit("/", 1)[-1]
        if not AO.is_valid_key(key):
            continue
        out.append({
            "ol_key": key,
            "name": doc.get("name") or "",
            "birth_date": doc.get("birth_date"),
            "death_date": doc.get("death_date"),
            "work_count": doc.get("work_count") or 0,
            "top_work": doc.get("top_work"),
            "alternate_names": (doc.get("alternate_names") or [])[:6],
        })
    return out


def _wikidata_one(ol_key: str) -> dict | None:
    """The single-author version of harvest_authors.py's P648 join.

    THE SAME QUERY AND THE SAME FOLD, imported rather than retyped. Those
    OPTIONAL clauses and the row-per-combination collapse are the part that
    was got wrong once already (H. G. Wells carries two spellings of the
    United Kingdom, and assignment instead of a set kept whichever came
    last) — a second copy here is a second chance to get it wrong, in the
    one place where a wrong fact would be applied by hand and trusted.
    """
    from harvest_authors import ENDPOINT, HEADERS, QUERY, fold    # noqa: E402
    url = ENDPOINT + "?" + urllib.parse.urlencode(
        {"query": QUERY % f'"{ol_key}"', "format": "json"})
    out: dict = {}
    fold(_fetch(url, 45, headers=HEADERS)["results"]["bindings"], out)
    return out.get(ol_key)


@admin_router.post("/resolve")
def resolve(body: ResolveRequest):
    """Everything known about one author, before anything is written.

    Called twice in a normal session: once with a name to get the candidate
    list, once more with the chosen `ol_key` to fill the facts in. Step two
    is skipped entirely for an author Open Library does not have — the
    `name:` key is computed either way and returned every time, because for
    a manually added book that key IS the identity and the admin needs to
    see it before deciding anything.
    """
    name = body.name.strip()
    name_key = AO.name_key(name)

    ol_key = (body.ol_key or "").strip().rsplit("/", 1)[-1] or None
    if ol_key and not AO.is_valid_key(ol_key):
        raise HTTPException(status_code=400, detail="malformed Open Library author key")

    raw, _ = _get_json(AUTHOR_OVERRIDES_PATH, {})
    overrides = raw if isinstance(raw, dict) else {}
    listed, per_book, _names = _shipped()

    # Which entries already speak for this author. Checked BEFORE anything
    # is offered, because the one thing this tab must not do is quietly
    # create a second entry for someone who already has one.
    existing = {}
    for key in (ol_key, name_key):
        if key and isinstance(overrides.get(key), dict):
            existing[key] = overrides[key]
    claimed_by = None
    if name_key:
        for key, entry in overrides.items():
            if key == name_key or not isinstance(entry, dict):
                continue
            if any(AO.name_key(a) == name_key
                   for a in (entry.get("aliases") or []) if isinstance(a, str)):
                claimed_by = key
                break

    out = {
        "name": name,
        "name_key": name_key,
        "ol_key": ol_key,
        "existing": existing,
        "claimed_by": claimed_by,
        "questions": AUTHOR_QUESTIONS,
        "in_game": {
            "name_key": _book_count(name_key, listed, per_book) if name_key else 0,
            "ol_key": _book_count(ol_key, listed, per_book) if ol_key else 0,
        },
        "effect": "facts reach players at the next full build_matrix.py run "
                  "only; an alias takes effect at that same rebuild and never "
                  "rewrites a book count already exported",
    }

    if not ol_key:
        try:
            out["candidates"] = _ol_search(name)
            out["searched"] = True
        except Exception as exc:                                  # noqa: BLE001
            # A failed search is not "Open Library has never heard of them".
            log.warning("OL author search failed for %r: %s", name, str(exc)[:120])
            out["candidates"] = []
            out["searched"] = False
            out["search_error"] = str(exc)[:120]
        return out

    try:
        record = _wikidata_one(ol_key)
        out["wikidata"] = record
    except Exception as exc:                                      # noqa: BLE001
        log.warning("Wikidata lookup failed for %s: %s", ol_key, str(exc)[:120])
        out["wikidata"] = None
        out["wikidata_error"] = str(exc)[:120]
        return out

    # What the build WOULD compute from that record, so the admin edits the
    # same eight answers the game asks rather than raw Wikidata fields.
    # `author:prolific` comes from our own corpus, so it is real here even
    # when Wikidata found nothing at all.
    out["traits"] = traits_for(record, out["in_game"]["ol_key"])
    return out


# ── POST /akinator/admin/authors/save ────────────────────────────────────

class SaveRequest(BaseModel):
    key: str = Field(..., max_length=180)
    # `facts`/`aliases` REPLACE what the entry holds; `add_aliases` appends.
    # Replace is what the profile panel needs (it shows the whole entry and
    # submits the whole entry); append is what Merge needs (it knows one
    # name and must not clobber an entry it never displayed).
    facts: dict[str, bool | None] | None = None
    aliases: list[str] | None = None
    add_aliases: list[str] | None = None
    note: str = Field(default="", max_length=200)


def _clean_aliases(names: list[str], key: str, overrides: dict,
                   shipped_names: dict[str, str]) -> list[str]:
    """Aliases that will actually do something, or a 400 saying why not.

    Three refusals, and all three are "this would look like a correction and
    change nothing" — the same bar `/display` sets when it refuses a rename
    keyed to no shipped row:

      * a name with no identifying content, which `merge_key` returns "" for
      * a name already claimed as an alias by a DIFFERENT entry, which
        `alias_index` would resolve to whichever key sorts first
      * a name that belongs to an author the corpus already has an Open
        Library key for. `AuthorIndex` consults aliases only when there is
        no key — a real id outranks a spelling — so such an alias can never
        fire. Two OL records for one person is a fact about Open Library,
        and this file is deliberately not able to overrule it.

    That last check reads EVERY shipped first-author, not just the 795 the
    export lists: an OL-keyed author with one book is absent from
    `authors.json`'s `authors` array, and checking only that array would
    have let exactly the low-book-count authors this tab is for slip
    through with an alias that does nothing.
    """
    out: list[str] = []
    seen = set()
    ol_named = {}
    for author_id, shipped in shipped_names.items():
        if not author_id.startswith("name:"):
            nk = AO.name_key(shipped)
            if nk:
                ol_named.setdefault(nk, author_id)

    for raw in names:
        alias = (raw or "").strip()
        if not alias:
            continue
        nk = AO.name_key(alias)
        if not nk:
            raise HTTPException(
                status_code=400,
                detail=f"{alias!r} has no identifying content — it folds to an "
                       f"empty key and would never match anything")
        if nk == key:
            raise HTTPException(
                status_code=400,
                detail=f"{alias!r} IS this entry's own key — an alias to itself "
                       f"teaches nothing")
        for other, entry in overrides.items():
            if other == key or not isinstance(entry, dict):
                continue
            if any(AO.name_key(a) == nk
                   for a in (entry.get("aliases") or []) if isinstance(a, str)):
                raise HTTPException(
                    status_code=409,
                    detail=f"{alias!r} is already an alias of {other} — remove it "
                           f"there first, so one name never points at two authors")
        target = ol_named.get(nk)
        if target and target != key:
            raise HTTPException(
                status_code=400,
                detail=f"{alias!r} is the shipped name of {target}, which has an "
                       f"Open Library key. An alias is only ever consulted for an "
                       f"author with no key, so this one could never take effect. "
                       f"Two OL records for one person is an Open Library fix, not "
                       f"an overlay one.")
        if nk not in seen:
            seen.add(nk)
            out.append(alias)
    return out


@admin_router.post("/save")
def save(body: SaveRequest):
    """Write one author's overlay entry. One commit, whole file.

    NOTHING HERE IS INSTANT AND THE RESPONSE SAYS SO. A fact feeds a matrix
    bit, and an alias feeds the author grouping — both are computed by
    `build_matrix.py` and by nothing that runs on Render. The same landing
    time `admin_corrections.json` has had since it shipped; an admin page
    that implies otherwise is worse than one that cannot change the field
    at all.
    """
    key = body.key.strip()
    if not AO.is_valid_key(key):
        raise HTTPException(
            status_code=400,
            detail="key must be an Open Library author key (OL…A) or "
                   "name:{merge_key} exactly as the build computes it — "
                   "call /authors/resolve to get the right one")

    current, _ = _get_json(AUTHOR_OVERRIDES_PATH, {})
    if not isinstance(current, dict):
        current = {}
    entry = dict(current.get(key) or {})
    facts = dict(entry.get("facts") or {})
    aliases = list(entry.get("aliases") or [])
    listed, per_book, shipped_names = _shipped()

    if body.facts is not None:
        unknown_ids = sorted(set(body.facts) - set(AUTHOR_QUESTIONS))
        if unknown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"not author questions: {unknown_ids} — "
                       f"settable ids are {sorted(AUTHOR_QUESTIONS)}")
        # `null` is a VERDICT, not a deletion: it forces the question back to
        # unknown over whatever Wikidata says, which is the answer this
        # codebase's central rule most often wants. Clearing an override is
        # done by leaving the id out of `facts` entirely.
        facts = dict(body.facts)

    if body.aliases is not None:
        aliases = _clean_aliases(body.aliases, key, current, shipped_names)
    if body.add_aliases:
        merged = aliases + [a for a in body.add_aliases
                            if AO.name_key(a) not in
                            {AO.name_key(x) for x in aliases}]
        aliases = _clean_aliases(merged, key, current, shipped_names)

    if facts or aliases:
        current[key] = {"facts": facts, "aliases": aliases}
    else:
        # An empty entry is not a correction, it is clutter that makes the
        # file look like it says something. Same reasoning as /display
        # dropping a work key whose overrides were all cleared.
        current.pop(key, None)

    note = f" — {body.note}" if body.note else ""
    what = []
    if body.facts is not None:
        what.append(f"{len(facts)} fact(s)")
    if body.aliases is not None or body.add_aliases:
        what.append(f"{len(aliases)} alias(es)")
    wrote = _commit_files(
        {AUTHOR_OVERRIDES_PATH: _dump(current)},
        f"mind reader admin: author {key} — {', '.join(what) or 'cleared'}{note}")
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")

    return {
        "ok": True,
        "key": key,
        "entry": current.get(key),
        "effect": "next full rebuild only",
        "note": "facts feed matrix bits and aliases feed the author grouping; "
                "only a local build_matrix.py run recomputes either. Merging "
                "does not rewrite a book count already exported.",
    }
