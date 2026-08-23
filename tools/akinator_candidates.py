"""
tools/akinator_candidates.py — the owner's verdict on a mined question.

READ `scripts/akinator/propose_questions.py` FIRST. That script asks a model
for candidate questions, measures every one against the real corpus (or a
real sample, for a prose-only candidate), checks it for redundancy against
what already ships, and writes the result straight into
`question_candidates.json` in the bookhub repo. This module is what turns
one entry there into a decision — nothing more.

THE BOUNDARY THIS DRAWS is the same one `akinator_suggest.py`'s
`decide_theme` draws for a reader-requested theme, one step earlier in a
similar pipeline: MEASURING is automated, DECIDING is a human reading a
number, and APPLYING the decision — pasting the drafted rule into
`features.py`/`traits.py`, then a `build_matrix.py` rebuild — stays a human
editing source code. `propose_questions.py`'s own docstring explains why
that paste is never automated: every real corpus-corruption incident this
project has hit was a semantic mistake no frequency or correlation number
could see, and only a person reading the actual keyword list and the actual
matched titles catches that class of error.

SIMPLER THAN THE READER QUEUE, and worth saying why. `akinator_suggest.py`'s
suggestions live in Redis first and only become a file on acceptance,
because they arrive continuously from live traffic. A mined candidate has no
such queue — `question_candidates.json` already IS the durable record,
written directly by a local script run. So `decide` here does not read from
Redis at all: it finds the entry by id in the same file, flips its status,
and commits the file back.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from tools.akinator_admin import (                                # noqa: E402
    ARTIFACT_DIR,
    _commit_files,
    _dump,
    _get_json,
    _require_admin,
)

CANDIDATES_PATH = f"{ARTIFACT_DIR}/question_candidates.json"

# Same reasoning as akinator_suggest.py's admin_router: _require_admin as a
# ROUTER dependency, not a line inside the handler, so a caller without the
# secret gets 403 before FastAPI ever validates the body — never a 422
# disclosing the schema.
admin_router = APIRouter(prefix="/akinator/admin/candidates", tags=["akinator"],
                         dependencies=[Depends(_require_admin)])


class CandidateDecision(BaseModel):
    id: str = Field(..., max_length=40)
    status: str = Field(..., pattern="^(accepted|rejected)$")


@admin_router.post("/decide")
def decide(body: CandidateDecision):
    """Record accepted/rejected against one mined candidate.

    Never writes to `features.py` or `traits.py`, and never triggers a
    rebuild — only `question_candidates.json` changes. The drafted rule text
    (`draft_rule` for a subject candidate, `draft_entry` for a prose one)
    stays exactly as `propose_questions.py` wrote it; accepting here only
    marks it worth pasting, it does not paste it.
    """
    current, _ = _get_json(CANDIDATES_PATH, [])
    if not isinstance(current, list):
        raise HTTPException(status_code=502,
                            detail="question_candidates.json unreadable")

    match = next((c for c in current
                 if isinstance(c, dict) and c.get("id") == body.id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="no such candidate")

    match["status"] = body.status
    wrote = _commit_files(
        {CANDIDATES_PATH: _dump(current)},
        f"mind reader admin: candidate {body.id} -> {body.status}")
    if not wrote:
        raise HTTPException(status_code=502, detail="commit failed")
    return {"ok": True, "id": body.id, "status": body.status,
            "effect": "recorded only — features.py/traits.py untouched, "
                      "no rebuild triggered"}
