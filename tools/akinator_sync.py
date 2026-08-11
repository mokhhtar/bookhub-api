"""
tools/akinator_sync.py — keep the mind-reader current between rebuilds.

WHY THIS EXISTS. Summarising a book already writes `_books/{slug}.md` with
themes, characters, ratings and free-ebook data, and the game's corpus
already pins every such page in. But nothing regenerated the artifacts, so a
newly summarised book did not reach the game until someone ran
`build_matrix.py` by hand.

WHY IT RUNS HERE AND NOT IN CI. The obvious fix is a GitHub Action running
the build. It cannot: the build needs `akinator_corpus.jsonl` and the
Wikidata harvests, all deliberately gitignored, and CI has none of them.
Every scheduled job in this project already has the answer — the Action is
a thin trigger and the work happens on Render, which holds `GITHUB_PAT` and
already commits to the `bookhub` repo. Render has no corpus either, but

    **an incremental update does not need one.**

Feature *selection* needs the whole corpus. *Encoding one book against an
already-fixed question list* needs only that book, and `questions.json` is
committed and publicly served.

WHAT IT DELIBERATELY DOES NOT DO. No feature re-selection: frequencies shift
as books arrive, so a question that should now clear the 5% floor will not
appear until the next full rebuild. Re-selecting would renumber every column
and invalidate every stored row. The monthly full rebuild keeps the question
set right; this keeps the book list current. Same division of labour as
refresh-bestsellers.yml.

THE FAILURE THAT MATTERS is a matrix whose rows stop lining up: every book
after the damage decodes as a different book's answers, silently, for
everyone. So every write is bracketed by an exact size assertion and a
question-set hash check, and any mismatch aborts the whole run. **A game one
book stale is fine; a game with a shifted matrix is not.**
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys

import httpx

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "akinator"))

log = logging.getLogger("bookhub-api.akinator_sync")

GITHUB_API = "https://api.github.com"
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "mokhhtar/bookhub")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

ARTIFACT_DIR = "games/data/akinator"
BOOKS_DIR = "_books"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_PAT}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
}

STATE_ABSENT, STATE_PRESENT, STATE_UNKNOWN = 0, 1, 2


# ── GitHub plumbing ────────────────────────────────────────────────────────
# Text-only helpers already exist in github_publisher.py; matrix.bin needs
# bytes, so these keep the base64 payload undecoded.

def _url(path: str) -> str:
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"


def _get_file(path: str) -> tuple[bytes, str]:
    """(raw bytes, blob sha). Empty bytes when absent."""
    r = httpx.get(_url(path), headers=_HEADERS,
                  params={"ref": GITHUB_BRANCH}, timeout=30.0)
    if r.status_code != 200:
        return b"", ""
    data = r.json()
    return base64.b64decode(data.get("content", "") or ""), data.get("sha", "") or ""


def _commit_files(files: dict[str, bytes], message: str) -> bool:
    """Write several files in ONE commit, via the Git Trees API.

    Three separate Contents PUTs would be three separate commits, and a run
    that died between them would leave matrix.bin longer than meta.json
    claims — every book after the join decoding as a different book's
    answers, silently, for everyone. This project has been bitten by
    partial-success writes repeatedly (the sub-cache poisoning, the
    published-page version gate, the series label fetcher abandoning its
    run); this is the one place where the failure is unrecoverable by a
    later pass, so it gets a real transaction.

    Either all three land or none do.
    """
    base = f"{GITHUB_API}/repos/{GITHUB_REPO}"
    try:
        ref = httpx.get(f"{base}/git/ref/heads/{GITHUB_BRANCH}",
                        headers=_HEADERS, timeout=30.0)
        ref.raise_for_status()
        head_sha = ref.json()["object"]["sha"]

        commit = httpx.get(f"{base}/git/commits/{head_sha}",
                           headers=_HEADERS, timeout=30.0)
        commit.raise_for_status()
        base_tree = commit.json()["tree"]["sha"]

        # Blobs are uploaded base64 so matrix.bin survives byte-for-byte.
        tree = []
        for path, blob in files.items():
            b = httpx.post(f"{base}/git/blobs", headers=_HEADERS, timeout=60.0, json={
                "content": base64.b64encode(blob).decode("ascii"),
                "encoding": "base64",
            })
            b.raise_for_status()
            tree.append({"path": path, "mode": "100644", "type": "blob",
                         "sha": b.json()["sha"]})

        t = httpx.post(f"{base}/git/trees", headers=_HEADERS, timeout=60.0,
                       json={"base_tree": base_tree, "tree": tree})
        t.raise_for_status()

        c = httpx.post(f"{base}/git/commits", headers=_HEADERS, timeout=60.0,
                       json={"message": message, "tree": t.json()["sha"],
                             "parents": [head_sha]})
        c.raise_for_status()

        u = httpx.patch(f"{base}/git/refs/heads/{GITHUB_BRANCH}",
                        headers=_HEADERS, timeout=30.0,
                        json={"sha": c.json()["sha"]})
        u.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("atomic commit failed: %s", exc)
        return False


def _list_book_pages() -> list[str]:
    r = httpx.get(_url(BOOKS_DIR), headers=_HEADERS,
                  params={"ref": GITHUB_BRANCH}, timeout=30.0)
    if r.status_code != 200:
        return []
    return [i["name"] for i in r.json()
            if i.get("type") == "file" and i.get("name", "").endswith(".md")]


# ── the sync ───────────────────────────────────────────────────────────────

def question_hash(questions: list[dict]) -> str:
    """Fingerprint of the question list, in order.

    The matrix is column-positional: row bit pairs mean nothing without the
    exact list they were packed against. Appending a row computed from a
    different list is the corruption this whole module is arranged to
    prevent, and it would be invisible — the file size would still be right.
    """
    ids = "|".join(q.get("id", "") for q in questions)
    return hashlib.sha256(ids.encode("utf-8")).hexdigest()[:16]


def _mark_uncomputed_unknown(book: dict, question_ids: list[str]) -> None:
    """Anything this sync cannot work out must be `unknown`, never absent.

    THE BUG THIS FIXES, found by asking what a newly published book actually
    claims. `_encode_row` writes ABSENT for any question in neither
    `present` nor `unknown`, and the monthly build fills those sets from
    FOUR sources while this sync only ever ran the first:

        extract()      subjects and structural facts      <- ran here
        book_traits()  Wikidata author facts              <- never ran
        merge_into()   film adaptation, protagonist       <- never ran
        apply_labels() the extracted traits               <- never ran

    So every book the owner published arrived asserting **no** to 55 of 66
    questions: no magic, no war, no family — and, absurdly, not American AND
    not British AND not European AND not non-western, all at once. A reader
    thinking of a book we ourselves publish, answering "yes, there's magic"
    correctly, pushed it DOWN.

    Subject-derived absence stays absent, and that is deliberate: a book
    whose subjects are known and do not include horror is weak evidence
    against horror, scaled by richness. The families below are different —
    they were never computed at all, and "we did not look" is what `unknown`
    is for. Same distinction as everywhere else here: a 429 is not a
    measurement, an unharvested description is not "no magic".
    """
    from author_traits import AUTHOR_QUESTIONS          # noqa: E402
    from work_traits import WORK_QUESTIONS              # noqa: E402
    from traits import TRAIT_QUESTIONS                  # noqa: E402

    uncomputed = set(AUTHOR_QUESTIONS) | set(WORK_QUESTIONS) | set(TRAIT_QUESTIONS)
    determined = set(book["present"]) | set(book["unknown"])
    book["unknown"] = sorted(
        set(book["unknown"]) | ((uncomputed & set(question_ids)) - determined))


def _label_traits(book: dict, page: dict, question_ids: list[str]) -> None:
    """Ask the model for this book's traits, from our own page's prose.

    The monthly rebuild labels traits from Open Library descriptions. A book
    we just published has something better: the summary on its own page,
    which a human reviewed. One call per newly published book — a handful a
    night — and it is build-time work on a schedule, never in the play path.

    FAILURE IS SAFE BY CONSTRUCTION. `_mark_uncomputed_unknown` has already
    set every trait to `unknown`, so a missing key, a spent quota, a timeout
    or an unparseable reply all leave the book exactly as if this function
    did not exist. It can only ever turn `unknown` into `present`, which is
    why it is allowed to run unattended at 02:40 with nobody watching.

    Grounded on the supplied prose only, by the same prompt the offline
    extractor uses — the model is told to omit anything the text does not
    support, and anything outside the vocabulary is dropped by the parser.
    """
    prose = (page.get("prose") or "").strip()
    if not prose:
        return
    try:
        from traits import TRAIT_QUESTIONS, build_prompt, parse_response
        wanted = set(TRAIT_QUESTIONS) & set(question_ids)
        if not wanted:
            return                      # this build ships no trait questions
        from gemini_client import generate
        labels = parse_response(generate(
            build_prompt(page.get("title") or "", page.get("author") or "",
                         prose[:4000])))
    except Exception as exc:            # noqa: BLE001
        log.warning("trait labelling skipped for %s: %s",
                    page.get("title"), str(exc)[:120])
        return

    hit = [l for l in labels if l in wanted]
    if not hit:
        return
    book["present"] = sorted(set(book["present"]) | set(hit))
    book["unknown"] = sorted(set(book["unknown"]) - set(hit))
    log.info("traits for %s: %s", page.get("title"), ", ".join(hit))


def _encode_row(book: dict, question_ids: list[str], bytes_per_row: int) -> bytes:
    """Pack one book's answers, exactly as build_matrix.pack_matrix does."""
    present = set(book["present"])
    unknown = set(book["unknown"])
    out = bytearray()
    acc, filled = 0, 0
    for q in question_ids:
        if q in present:
            state = STATE_PRESENT
        elif q in unknown:
            state = STATE_UNKNOWN
        else:
            state = STATE_ABSENT
        acc |= state << (filled * 2)
        filled += 1
        if filled == 4:
            out.append(acc)
            acc, filled = 0, 0
    if filled:
        out.append(acc)
    if len(out) != bytes_per_row:
        raise ValueError(f"row is {len(out)} bytes, artifact wants {bytes_per_row}")
    return bytes(out)


def sync(dry_run: bool = False) -> dict:
    """Append newly published books to the shipped game artifacts."""
    from features import extract, normalize                      # noqa: E402
    from characters import extract_characters, usable_token      # noqa: E402
    from authors import canonical_author, surname_token          # noqa: E402
    from site_books import parse_page                            # noqa: E402

    if not GITHUB_PAT:
        return {"ok": False, "reason": "GITHUB_PAT not configured"}

    meta_raw, _ = _get_file(f"{ARTIFACT_DIR}/meta.json")
    q_raw, _ = _get_file(f"{ARTIFACT_DIR}/questions.json")
    books_raw, _ = _get_file(f"{ARTIFACT_DIR}/books.json")
    matrix, _ = _get_file(f"{ARTIFACT_DIR}/matrix.bin")
    if not (meta_raw and q_raw and books_raw and matrix):
        return {"ok": False, "reason": "artifacts missing or unreadable"}

    meta = json.loads(meta_raw)
    questions = json.loads(q_raw)
    books = json.loads(books_raw)
    bpr = meta["bytes_per_row"]

    # Guard 1: the artifacts must already be self-consistent. If they are
    # not, something else is wrong and appending would only bury it.
    if len(matrix) != meta["books"] * bpr:
        return {"ok": False, "reason":
                f"matrix is {len(matrix)} bytes, expected {meta['books'] * bpr}"}
    if len(books) != meta["books"]:
        return {"ok": False, "reason":
                f"books.json has {len(books)} entries, meta says {meta['books']}"}

    # Guard 2: the question list must be the one this code would pack
    # against. A full rebuild that changed it invalidates every stored row.
    stamped = meta.get("question_hash")
    live = question_hash(questions)
    if stamped and stamped != live:
        return {"ok": False, "reason":
                "question_hash mismatch — artifacts were rebuilt; run a full build"}

    def ident(title: str, author: str) -> tuple[str, str]:
        import re
        main = re.split(r"[:;]| - ", title, 1)[0]
        return normalize(main), surname_token(canonical_author(author))

    have = {ident(b.get("t") or "", b.get("a") or "") for b in books}
    have_titles = {normalize(b.get("t") or "") for b in books}

    question_ids = [q["id"] for q in questions]
    added, skipped = [], 0

    for name in _list_book_pages():
        raw, _ = _get_file(f"{BOOKS_DIR}/{name}")
        page = parse_page(raw.decode("utf-8", errors="replace"))
        if not page:
            continue
        if (ident(page["title"], page["author"]) in have
                or normalize(page["title"]) in have_titles):
            skipped += 1
            continue

        # Popularity: the floor of what already ships. A new arrival is
        # findable without displacing books whose popularity was measured
        # on the same scale as everyone else's — never a converted
        # Goodreads count, for the fifteen-fold reason in site_books.py.
        floor = min((b.get("p") or 0) for b in books) if books else 1

        doc = {
            "key": "/site/" + (page["slug"] or normalize(page["title"]).replace(" ", "-")),
            "title": page["title"],
            "author_name": [page["author"]] if page["author"] else [],
            "author_key": [],
            "first_publish_year": int(page["year"]) if str(page["year"]).isdigit() else None,
            "subject": list(page.get("themes") or []) + list(page.get("categories") or []),
            "person": [c.get("name") for c in (page.get("characters") or [])
                       if isinstance(c, dict) and c.get("name")],
            "language": ["eng"],
            "readinglog_count": floor,
            "ebook_access": "public" if page.get("free_ebook") else "",
        }

        rank = meta["books"] + len(added)
        book = extract(doc, rank, rank + 1)
        names_, tokens = extract_characters(book.pop("persons"))
        book["char_tokens"] = sorted(t for t in tokens if usable_token(t))
        _mark_uncomputed_unknown(book, question_ids)
        _label_traits(book, page, question_ids)

        try:
            row = _encode_row(book, question_ids, bpr)
        except ValueError as exc:
            return {"ok": False, "reason": f"encoding {page['title']}: {exc}"}

        matrix += row
        books.append({"k": doc["key"], "t": page["title"], "a": page["author"],
                      "y": doc["first_publish_year"], "p": floor,
                      "r": book["richness"], "w": None, "c": None})
        have.add(ident(page["title"], page["author"]))
        have_titles.add(normalize(page["title"]))
        added.append(page["title"])

    if not added:
        return {"ok": True, "added": 0, "skipped": skipped}

    meta["books"] = len(books)
    meta["question_hash"] = live

    # Guard 3: the same size check, after. A row that packed to the right
    # length individually can still leave the file wrong if anything else
    # touched it.
    if len(matrix) != meta["books"] * bpr:
        return {"ok": False, "reason": "post-append size check failed; nothing written"}

    if dry_run:
        return {"ok": True, "dry_run": True, "added": len(added),
                "skipped": skipped, "titles": added[:20]}

    msg = f"mind reader: +{len(added)} newly published book(s)"
    wrote = _commit_files({
        f"{ARTIFACT_DIR}/matrix.bin": matrix,
        f"{ARTIFACT_DIR}/books.json": json.dumps(
            books, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        f"{ARTIFACT_DIR}/meta.json": json.dumps(
            meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    }, msg)
    return {"ok": wrote, "added": len(added), "skipped": skipped,
            "titles": added[:20]}


# ── HTTP surface ───────────────────────────────────────────────────────────

from fastapi import APIRouter, Header, HTTPException, Query  # noqa: E402

router = APIRouter(prefix="/akinator", tags=["akinator"])

SYNC_SECRET = os.environ.get("AKINATOR_SYNC_SECRET", "")


@router.post("/sync")
def sync_endpoint(
    x_sync_secret: str = Header(default=""),
    dry_run: bool = Query(default=False),
):
    """Append newly published books to the game artifacts.

    Secret-protected for the same reason /indexing/promote is: each run
    costs GitHub API writes and commits to a public repo. Unset secret
    means the endpoint is closed, not open — a missing secret must never
    be the thing that makes a write endpoint public.
    """
    if not SYNC_SECRET or x_sync_secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    result = sync(dry_run=dry_run)
    if not result.get("ok"):
        # 409, not 500: the guards refusing is the system working. A
        # question_hash mismatch means "run a full build", not "a bug".
        raise HTTPException(status_code=409, detail=result.get("reason", "sync refused"))
    return result
