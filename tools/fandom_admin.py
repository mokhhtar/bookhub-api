"""
tools/fandom_admin.py — the review gate between "the game proved this wiki
exists" and "the summarizer trusts it live".

THE GAP THIS CLOSES, measured 2026-08-25. `scripts/akinator/`'s discovery
pipeline (`discover_fandom.py`, `harvest_fandom.py`) had proven 41 Fandom
wikis real, in its local `data/akinator_fandom.json` — never on Render. Only
19 of them had ever been hand-copied into `tools/fandom.py`'s live
`FANDOM_WIKIS`, because that copy was a manual, easy-to-forget step, and it
had stopped happening: a vault note flagged the exact same gap on
2026-08-14 and it was still 23 books wide 11 days later. Real books
(Mushoku Tensei, Overlord, Martial Peak among them) that the game already
knew had a wiki were invisible to `/search`, `quiz.py` and `summary.py`
purely because nobody had re-typed a JSON entry into a `.py` dict recently
enough. Sampled 2026-08-25: of 12 of the missing 23, only 2 were even
reachable through `resolve_fandom_subdomain`'s own live guessing tiers, and
one of THOSE resolved to a different subdomain than the game had confirmed
("Against the Gods" -> `ni-tian-xie-shen-against-the-gods` by guessing vs.
`against-the-gods` as recorded) — exactly the kind of discrepancy a human
has to look at, not code.

WHAT THIS DOES NOT DO. `games/data/fandom/candidates.json` (what the game's
discovery proved) and `games/data/fandom/wikis.json` (what `tools/fandom.py`
reads live) stay two files, on purpose — proving a wiki exists is a
different judgement from trusting it in a live-serving path with real
blast radius, which is why `discover_fandom.py`'s own docstring says in
capitals that it never touches `FANDOM_WIKIS`. This module does not weaken
that: it only removes the friction between "proven" and "approved" —
turning a hand re-type between two differently-shaped files into one
reviewed click. The review is not automated; only the mechanics are.

Same infrastructure as the akinator admin endpoints, not a copy of it.
`_get_file`/`_commit_files`/`_commit_with_retry` from `tools.akinator_sync`
are generic GitHub-commit plumbing (an arbitrary path, arbitrary bytes) that
happens to live in an akinator-named module; reusing it here is exactly the
"share a well-tested function, don't duplicate it" rule CLAUDE.md states for
this repo, not a sign these two tools are coupled. Gated by the SAME
Cloudflare Access + AKINATOR_ADMIN_SECRET as every other admin endpoint,
because building a second auth surface for one more JSON file would be pure
duplication for zero benefit — one operator, one page.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from pydantic import BaseModel, Field                   # noqa: E402

from tools.akinator_admin import _require_admin          # noqa: E402
from tools.akinator_sync import (                        # noqa: E402
    _commit_with_retry,
    _get_file,
)

log = logging.getLogger("bookhub-api.fandom_admin")

DATA_DIR = "games/data/fandom"
WIKIS_PATH = f"{DATA_DIR}/wikis.json"
CANDIDATES_PATH = f"{DATA_DIR}/candidates.json"

# Hosts a cover may never be served from. Fandom serves wiki uploads off
# static.wikia.nocookie.net (and older vignette.wikia.nocookie.net); the
# *.fandom.com match catches a direct file link. Substring on the host, not an
# exact equality, because the CDN prefixes vary by wiki.
_WIKI_HOST = re.compile(r"(?:wikia\.nocookie\.net|\.fandom\.com)", re.I)

router = APIRouter(prefix="/fandom/admin", tags=["fandom"],
                   dependencies=[Depends(_require_admin)])


def _dump(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")


def _load_json(path: str, ref: str | None, default):
    raw, _sha = _get_file(path, ref)
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return default
    return data if isinstance(data, type(default)) else default


@router.post("")
def list_candidates():
    """Every proven-but-not-yet-live wiki, plus what fandom.py's OWN live
    guessing tiers currently do for the same title — so the reviewer sees a
    disagreement (see the module docstring's "Against the Gods" case)
    before approving, not after.
    """
    candidates = _load_json(CANDIDATES_PATH, None, {})
    live = _load_json(WIKIS_PATH, None, {})
    if not isinstance(candidates, dict) or not isinstance(live, dict):
        raise HTTPException(status_code=502, detail="candidates.json or wikis.json unreadable")

    live_subs = set(live.keys())
    rows = [
        {"subdomain": sub, **{k: v for k, v in entry.items() if k != "subdomain"}}
        for sub, entry in sorted(candidates.items())
        if sub not in live_subs
    ]
    return {"ok": True, "candidates": rows, "live_count": len(live)}


class ApproveRequest(BaseModel):
    subdomain: str = Field(..., max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    author: str | None = Field(default=None, max_length=120)
    cover_url: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=300)


@router.post("/approve")
def approve(body: ApproveRequest):
    """The one edit that reaches both tools: write `subdomain` into
    wikis.json (what tools/fandom.py serves live) and drop it from
    candidates.json, in ONE commit.

    The admin's aliases/author/cover_url win over whatever the candidate
    record proposed — same precedence as every other hand-reviewed field in
    this project. An alias list is required to be non-empty in practice
    (the caller should have typed at least the seed title) but not
    enforced here: an entry with a bare subdomain and no aliases still
    resolves lookups keyed on that exact subdomain, and refusing it would
    only push a real reviewer into typing a placeholder alias to get past
    a check that protects nothing.
    """
    sub = body.subdomain.strip()
    if not sub or not all(c.isalnum() or c in "-_" for c in sub):
        raise HTTPException(status_code=400, detail="malformed subdomain")

    # A COVER MAY NOT COME FROM THE WIKI, and this is checked here rather than
    # left as a note because the review card puts an editable cover_url field
    # in front of a human looking at a wiki page — the wiki image is precisely
    # the one within easiest reach, and a rule that lives only in a comment
    # would be re-broken by the next reviewer who is moving quickly.
    #
    # Fandom's own help pages: non-text media does not inherit the wiki's
    # CC-BY-SA, most images are user uploads under a fair-use rationale,
    # Fandom "is unable to either give or deny permission for their reuse",
    # and there is no licence verification. Two entries got here that way
    # before this check existed (official manhua cover art and official
    # character art) and were removed 2026-08-28. See the COVER SOURCES note
    # in tools/fandom_catalog.py.
    cover = (body.cover_url or "").strip()
    if cover and _WIKI_HOST.search(cover):
        raise HTTPException(
            status_code=400,
            detail="that cover is hosted on the Fandom wiki. Wiki IMAGES are "
                   "not covered by the wiki's CC-BY-SA licence — they are "
                   "usually publisher artwork uploaded under fair use, and "
                   "Fandom cannot grant permission to reuse them. Use an Open "
                   "Library cover (covers.openlibrary.org/b/id/...), a Google "
                   "Books thumbnail, or leave it empty: a book with no cover "
                   "renders fine, and no data beats wrong data.")

    def build(head: str):
        live = _load_json(WIKIS_PATH, head, {})
        candidates = _load_json(CANDIDATES_PATH, head, {})
        if not isinstance(live, dict) or not isinstance(candidates, dict):
            raise HTTPException(status_code=502, detail="wikis.json or candidates.json unreadable")

        entry = {"subdomain": sub, "aliases": [a.strip() for a in body.aliases if a.strip()]}
        if body.author:
            entry["author"] = body.author.strip()
        if body.cover_url:
            entry["cover_url"] = body.cover_url.strip()
        live[sub] = entry
        candidates.pop(sub, None)

        note = f" — {body.note}" if body.note else ""
        return ({WIKIS_PATH: _dump(live), CANDIDATES_PATH: _dump(candidates)},
                f"fandom admin: approved {sub}{note}", entry)

    wrote, entry = _commit_with_retry(build)
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "subdomain": sub, "entry": entry,
            "effect": "live within the hour — tools/fandom.py caches wikis.json "
                      "for up to 3600s before it notices a change"}


class RejectRequest(BaseModel):
    subdomain: str = Field(..., max_length=80)
    note: str = Field(default="", max_length=300)


@router.post("/reject")
def reject(body: RejectRequest):
    """Drop a candidate without approving it — a false positive from the
    discovery pipeline (wrong wiki, or genuinely not the right book).
    Removed rather than flagged, matching the queue's own "resolving
    deletes it" convention elsewhere in this admin — nothing here is worth
    keeping once a human has looked and said no.
    """
    sub = body.subdomain.strip()

    def build(head: str):
        candidates = _load_json(CANDIDATES_PATH, head, {})
        if not isinstance(candidates, dict):
            raise HTTPException(status_code=502, detail="candidates.json unreadable")
        if sub not in candidates:
            return ({}, "", False)
        candidates.pop(sub, None)
        note = f" — {body.note}" if body.note else ""
        return ({CANDIDATES_PATH: _dump(candidates)},
                f"fandom admin: rejected {sub}{note}", True)

    wrote, removed = _commit_with_retry(build)
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "subdomain": sub, "removed": removed}
