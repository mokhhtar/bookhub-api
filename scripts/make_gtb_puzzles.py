"""
scripts/make_gtb_puzzles.py — builds the daily "Guess the Book" puzzle files
that the static game in the `bookhub` repo plays from.

    python scripts/make_gtb_puzzles.py --from 2026-08-01 --count 7
    python scripts/make_gtb_puzzles.py --from 2026-08-01 --gid 2489   # one book
    python scripts/make_gtb_puzzles.py --from 2026-08-01 --count 7 --dry-run

RUNS LOCALLY, ON PURPOSE. Nothing in the served product calls Gemini or
Render for this game: puzzles are generated here, reviewed by a human, and
committed as static JSON. Play time is pure client-side file reads, so the
game works while Render sleeps and costs nothing per player.

Requires Pillow (cover processing) and a GEMINI_API_KEY in .env. Pillow is
deliberately NOT added to requirements.txt — Render never runs this script
and shouldn't carry the dependency.

THE PIPELINE, and where the grounding rule bites at each stage:
  1. Pick a pool entry (tools/gtb_pool.py — hand-verified, see its docstring).
  2. Load the real Gutenberg text, chunk it with quiz_core._chunk_text — the
     same chunker the quiz pipeline trusts.
  3. Ask Gemini for candidate passages, instructed to return FEWER rather
     than invent or alter one.
  4. Verify every candidate by normalized substring match against the source
     chunks (quiz_core._normalize_for_match). Unverified → dropped.
  5. Reject any candidate containing a banned term (title, author, cast,
     curated giveaways) or a stray proper noun. Mechanical, not taste.
  6. Assemble 6 clues; a book that cannot produce 2 verified quotes produces
     NO puzzle for that day rather than a weaker one.
  7. Bake 5 blur levels of the Gutenberg cover. The sharp cover is never
     committed — its URL lives inside the obfuscated reveal payload, so
     browsing the repo's assets can't spoil a month of answers.
  8. Write the JSON + refresh manifest.json / titles.json.
  9. Print a REVIEW REPORT. The owner reads every clue before committing —
     that human gate is part of the design, not a formality.

Obfuscation, stated honestly: `answer_hash` is sha256(date|canonical_id) and
`reveal_enc` is XOR+base64 under a key derived from the date. Both stop
casual spoiling (view-source, the network tab, someone opening the JSON).
Neither is a secret: the client necessarily knows how to derive the key, and
the repo is public. Same position Wordle shipped in — do not oversell it.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """This repo has no dotenv loading anywhere (env comes from Render's
    dashboard in production), so local scripts parse .env themselves —
    the convention CLAUDE.md documents for local smoke tests."""
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

from google.genai import types as genai_types  # noqa: E402

import gemini_client  # noqa: E402
from tools.gtb_pool import GTB_POOL, PoolEntry  # noqa: E402
from tools.quiz_core import _chunk_text, _normalize_for_match  # noqa: E402
from verify_gtb_pool import UA, fetch_raw, strip_boilerplate  # noqa: E402

SCHEMA_VERSION = 1
CLUE_COUNT = 6
QUOTES_PER_PUZZLE = 2
# Ask for far more candidates than we need: the verification + giveaway +
# proper-noun filters routinely reject two thirds of them, and a first run at
# 8 candidates / 220 chars refused half the days outright.
CANDIDATES_WANTED = 12
SAMPLE_CHUNKS = 14
MAX_QUOTE_CHARS = 260
MIN_QUOTE_CHARS = 40
BOOKS_PER_DAY_ATTEMPTS = 3   # a book that can't ground a puzzle yields the day to another
OPENING_SKIP = 0.08     # ignore the first 8% of a book — famous openings
ENDING_SKIP = 0.94      # and the last 6% — endings spoil more than they hint
MIN_CHUNK_GAP = 6       # the two quotes must come from genuinely different places
# 5 blur levels: index 0 shows before the first guess, index 4 after the
# fifth miss. Widths the cover is crushed to before being blown back up.
#
# These numbers are deliberately tiny, and they were tuned by LOOKING at the
# output, not by taste: a first pass at [10,16,26,40,64] rendered "JANE EYRE"
# perfectly legible by level 2 — the game would have ended on the second miss.
# A second pass at [5,7,10,14,19] still let the top-line text of The War of the
# Worlds' cover start to resolve at level 4. Most Gutenberg covers are
# photographs of a title page, so the ladder has to stay at "colour, binding,
# ornament, roughly how many words" and never resolve a letterform.
# Re-check by eye (scripts' --rebake-covers) whenever these change.
BLUR_WIDTHS = [5, 7, 9, 12, 15]
COVER_OUTPUT_WIDTH = 320

DEFAULT_SITE = os.path.abspath(os.path.join(REPO_ROOT, "..", "bookhub"))
DATA_SUBPATH = os.path.join("games", "data", "guess-the-book")
COVER_SUBPATH = os.path.join("assets", "games", "gtb")

# Capitalised words that are not proper nouns in the "names a character or
# place" sense, so they must not trip the stray-proper-noun filter.
_CAPITAL_ALLOW = {
    "God", "Lord", "Heaven", "Hell", "Providence", "Nature", "Fate", "Death",
    "Christmas", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Time", "Man", "Earth", "Sea", "Devil", "Truth",
}


# ── the day's book ───────────────────────────────────────────

def shuffled_pool(entries: list[PoolEntry], start: dt.date,
                  already_used: list[str]) -> list[PoolEntry]:
    """Books still available, in a deterministic order (seeded on the start
    date so re-running the same batch reproduces the same schedule)."""
    pool = [e for e in entries if e.canonical_id not in already_used]
    random.Random(start.toordinal()).shuffle(pool)
    return pool


def candidates_for_day(remaining: list[PoolEntry], recent_authors: list[str]) -> list[PoolEntry]:
    """Preference order for one day: books whose author hasn't appeared in the
    last four puzzles first, everything else after — so a run never stalls
    just because the only books left share an author."""
    fresh = [e for e in remaining if e.author not in recent_authors[-4:]]
    return fresh + [e for e in remaining if e not in fresh]


def used_canonical_ids(data_dir: str, ignore_dates: set[str] | None = None) -> list[str]:
    """Which books existing puzzle files already spent. Read back by decoding
    their own reveal payload — no separate ledger to drift out of sync.

    `ignore_dates` are the days we're about to regenerate: a book must not be
    treated as "already used" by the very file we're replacing, or --overwrite
    silently reshuffles the whole schedule (it did, on the first real run).
    """
    used: list[str] = []
    if not os.path.isdir(data_dir):
        return used
    for name in sorted(os.listdir(data_dir)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
            continue
        if ignore_dates and name[:-5] in ignore_dates:
            continue
        try:
            with open(os.path.join(data_dir, name), encoding="utf-8") as f:
                puzzle = json.load(f)
            reveal = decode_reveal(puzzle["reveal_enc"], puzzle["date"])
            used.append(reveal["id"])
        except Exception as e:
            print(f"  ! could not read {name}: {e}")
    return used


# ── clue sourcing ────────────────────────────────────────────

def _build_quote_prompt(count: int, excerpts: str) -> str:
    """Mirrors the phrasing pattern of tools/fandom.py's extraction prompts:
    state the rules, then make returning LESS the explicitly correct answer."""
    return f"""Select quotations for a "guess the book" puzzle. Below are numbered excerpts from ONE book.

Choose up to {count} short passages that would work as puzzle clues.

A passage is acceptable ONLY if every rule holds:
- Between {MIN_QUOTE_CHARS} and {MAX_QUOTE_CHARS} characters, 1 to 3 complete sentences.
- Copied EXACTLY, character for character, from a single excerpt. Never merge two places, never trim mid-word, never paraphrase or modernise spelling.
- Contains NO proper nouns whatsoever — no character names, place names, ship or house names, no nationalities, no titles of works.
- Reads as a self-contained line. Not a fragment that depends on the sentence before it ("and then he said so"), and not a line of dialogue whose speaker matters.
- Does not name or describe the book, its author, or its plot summary.
- Is the BOOK'S OWN PROSE. Reject anything the book is merely quoting: a line from another author, a poem, a song, a hymn, a letter reproduced in full, an epigraph or a chapter motto. Such a passage appears in the text but is not of it, and it points a reader at the wrong book entirely.

Order the passages from LEAST identifying (abstract — could belong to many books) to MOST identifying (evokes one specific scene).

If fewer than {count} passages satisfy every rule, return fewer. Returning fewer passages is the correct answer. Inventing, trimming or "fixing" a passage is not.

EXCERPTS:
{excerpts}

Return ONLY a JSON array:
[{{"text": "...", "chunk_index": 0}}]"""


def _looks_like_verse(text: str) -> bool:
    """Verse capitalises the first word of every line, so once the line breaks
    are flattened into a single string it shows up as function words capitalised
    mid-sentence ("has no time to think Of sorrow or care"). Caught after a
    stanza from a song inside Little Women shipped as if it were the novel's
    own prose."""
    mid = re.findall(r"(?<=[a-z,;] )([A-Z][a-z]{1,4})\b", text)
    function_words = {"Of", "And", "The", "As", "We", "My", "In", "To", "Or",
                      "For", "But", "With", "That", "Then", "When", "So", "A"}
    return sum(1 for w in mid if w in function_words) >= 2


def _has_stray_proper_noun(text: str) -> str | None:
    """A capitalised word mid-sentence that isn't an allowed abstraction is
    almost always a name the model failed to filter. Over-rejection is fine —
    there are thousands of candidate passages in a novel and only two seats."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)
        for word in words[1:]:
            if re.fullmatch(r"[A-Z][a-z]{2,}", word) and word not in _CAPITAL_ALLOW:
                return word
    return None


_last_call = [0.0]


def _pace() -> None:
    """Keep under the Gemini free tier's 15 requests/minute. A 30-day batch
    now costs two calls per book, which blew the limit and made a whole audit
    run return nothing but 429s — silently, since every failure degrades to
    'check skipped'. Waiting four seconds is cheaper than a bad review."""
    gap = time.time() - _last_call[0]
    if gap < 4.2:
        time.sleep(4.2 - gap)
    _last_call[0] = time.time()


def _flag_borrowed_lines(candidates: list[dict], report: list[str]) -> list[dict]:
    """FLAG (never drop) passages the novel may be QUOTING rather than writing.

    Emma contains "The course of true love never did run smooth" — verbatim in
    the text, verifiable, and Shakespeare's. Shown as a clue it points a player
    at A Midsummer Night's Dream. No regex can recognise a borrowed line, and
    the prompt rule alone was ignored twice in a row, so this asks the model
    directly.

    WHY IT ONLY FLAGS. Run as a hard filter over the first 30-day bank it
    marked 6 puzzles, and 5 were plain wrong: Ahab's "strike through the mask",
    the Duchess's "everything's got a moral", Lord Henry on temptation — each
    the author's own famous prose, flagged for sounding quotable. Dropping
    those systematically strips every book of its best lines, which is a real
    cost paid for an unreliable signal. So the model's opinion goes to the
    human reviewer, who is the gate anyway, instead of silently deleting good
    writing. A failed call flags nothing."""
    if not candidates:
        return candidates
    listed = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(candidates))
    prompt = f"""Below are numbered passages taken from one novel.

Novels often quote OTHER works: Shakespeare, the Bible, hymns, ballads, poems, proverbs, nursery rhymes, popular songs.

List the index of every passage whose wording ORIGINATES in another work and is only being quoted by this novel.

Do NOT list a passage merely because it sounds proverbial or memorable. If you are not confident the wording comes from a specific earlier work, leave it out.

PASSAGES:
{listed}

Return ONLY JSON: {{"borrowed": [0, 3]}}"""
    try:
        _pace()
        raw = gemini_client.generate(prompt, genai_types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=512, response_mime_type="application/json"))
        data = json.loads(raw)
        borrowed = {int(i) for i in (data.get("borrowed") or []) if isinstance(i, (int, str))}
    except Exception as e:
        report.append(f"    note: borrowed-line check skipped ({type(e).__name__})")
        return candidates

    for i, candidate in enumerate(candidates):
        if i in borrowed:
            report.append(f"    REVIEW — may be quoting another work, check before "
                          f"committing: {candidate['text'][:70]}…")
    return candidates


def pick_quotes(entry: PoolEntry, chunks: list[str], report: list[str]) -> list[dict]:
    """Gemini proposes, verification disposes. One retry before giving up —
    the same "generate, and if the verifier ate everything, generate once
    more" shape tools/quiz.py uses."""
    chosen = _pick_quotes_once(entry, chunks, report)
    if len(chosen) < QUOTES_PER_PUZZLE:
        report.append("    retrying — first pass left too few verified quotes")
        chosen = _pick_quotes_once(entry, chunks, report)
    return chosen


def _pick_quotes_once(entry: PoolEntry, chunks: list[str], report: list[str]) -> list[dict]:
    n = len(chunks)
    take = min(SAMPLE_CHUNKS, n)
    # Skip the opening and closing stretch of the book. Famous first lines are
    # the single most identifying text a novel has ("Call me Ishmael", "It is a
    # truth universally acknowledged") and last pages spoil endings — neither
    # belongs in a clue meant to be hard. Found by running this on Moby-Dick and
    # watching both quotes come back from chunk 1.
    lo, hi = (round(n * OPENING_SKIP), round(n * ENDING_SKIP)) if n > 20 else (0, n - 1)
    indices = sorted({round(lo + i * (hi - lo) / max(take - 1, 1)) for i in range(take)})
    sampled = {i: chunks[i] for i in indices}
    excerpts = "\n\n".join(f"[Chunk {i}]\n{c}" for i, c in sampled.items())

    config = genai_types.GenerateContentConfig(
        temperature=0.4, max_output_tokens=2048, response_mime_type="application/json"
    )
    _pace()
    raw = gemini_client.generate(_build_quote_prompt(CANDIDATES_WANTED, excerpts), config)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = gemini_client.parse_json_response(raw)
    if isinstance(data, dict):
        data = data.get("quotes") or next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        report.append("    model returned no usable array")
        return []

    normalized = {i: _normalize_for_match(c) for i, c in sampled.items()}
    banned = entry.banned_terms()
    accepted: list[dict] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        short = text[:60].replace("\n", " ")

        if not (MIN_QUOTE_CHARS <= len(text) <= MAX_QUOTE_CHARS):
            report.append(f"    dropped ({len(text)} chars): {short}…")
            continue

        # 1. It must genuinely be in the book. This is the same contract
        #    quiz_core enforces for supporting_quote — never relaxed.
        needle = _normalize_for_match(text)
        found_in = None
        ci = item.get("chunk_index")
        if isinstance(ci, int) and ci in normalized and needle in normalized[ci]:
            found_in = ci
        else:
            found_in = next((i for i, nc in normalized.items() if needle in nc), None)
        if found_in is None:
            report.append(f"    dropped (NOT VERBATIM in source): {short}…")
            continue

        # 2. It must not hand over the answer. Matched on WORD BOUNDARIES, not
        #    raw substrings: a plain `in` test makes "Tom" ban "tomorrow" and
        #    "Pip" ban "pipe", which quietly starves short-named books of any
        #    usable quote at all.
        hit = next((t for t in banned
                    if re.search(r"\b" + re.escape(t) + r"\b", needle)), None)
        if hit:
            report.append(f'    dropped (giveaway "{hit}"): {short}…')
            continue

        stray = _has_stray_proper_noun(text)
        if stray:
            report.append(f'    dropped (proper noun "{stray}"): {short}…')
            continue

        if _looks_like_verse(text):
            report.append(f"    dropped (reads as verse, not the book's prose): {short}…")
            continue

        accepted.append({"text": text, "chunk": found_in})

    accepted = _flag_borrowed_lines(accepted, report)

    # Keep the model's ordering (least → most identifying) but refuse two
    # quotes from the same corner of the book: two lines off one page make the
    # second clue add almost nothing.
    chosen: list[dict] = []
    for candidate in accepted:
        if all(abs(candidate["chunk"] - c["chunk"]) >= MIN_CHUNK_GAP for c in chosen):
            chosen.append(candidate)
        if len(chosen) >= QUOTES_PER_PUZZLE:
            break
    if len(chosen) < QUOTES_PER_PUZZLE and len(accepted) >= QUOTES_PER_PUZZLE:
        report.append(f"    note: {len(accepted)} verified quotes but too close together "
                      f"(need {MIN_CHUNK_GAP} chunks apart)")
    return chosen


def build_clues(entry: PoolEntry, quotes: list[dict]) -> list[dict]:
    """Six clues, hardest first. Quotes carry their chunk index so a reviewer
    can find them in the source; facts carry where the fact came from."""
    clues: list[dict] = []
    for quote in quotes:
        clue = {
            "i": len(clues),
            "type": "quote",
            "text": quote["text"],
            "source": {"kind": "gutenberg_text", "gid": entry.gutenberg_id,
                       "chunk": quote["chunk"]},
        }
        if entry.translator:
            clue["note"] = f"trans. {entry.translator}"
        clues.append(clue)

    clues.append({
        "i": len(clues), "type": "fact", "label": "First published",
        "text": f"{entry.first_published} · originally in {entry.language}",
        "source": {"kind": "curated"},
    })
    clues.append({
        "i": len(clues), "type": "fact", "label": "Author",
        "text": f"{entry.author_initials} — {entry.nationality}, {entry.author_years}",
        "source": {"kind": "curated"},
    })
    clues.append({
        "i": len(clues), "type": "fact", "label": "A character",
        "text": f"One of them is called {entry.characters[0]}.",
        "source": {"kind": "gutenberg_text", "gid": entry.gutenberg_id},
    })
    clues.append({
        "i": len(clues), "type": "fact", "label": "The story",
        "text": entry.setting,
        "source": {"kind": "curated"},
    })
    return clues


# ── covers ───────────────────────────────────────────────────

def cover_url(gid: int) -> str:
    return f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg"


def bake_cover_levels(entry: PoolEntry, puzzle_no: int, site_root: str,
                      dry_run: bool, report: list[str]) -> dict | None:
    """Download the public-domain Gutenberg cover and commit ONLY blurred
    derivatives. The sharp image stays off our servers entirely — its URL
    rides inside the encrypted reveal payload, so a curious visitor browsing
    /assets/ finds nothing but unreadable mosaics.

    No cover upstream → returns None and the puzzle simply has no cover, the
    same way every other resolver in this codebase omits what it can't ground.
    """
    from PIL import Image, ImageFilter

    url = cover_url(entry.gutenberg_id)
    source = None
    # gutenberg.org drops connections often enough that a single attempt cost a
    # real puzzle its cover on the first batch — retry before giving up.
    for attempt in (1, 2, 3):
        try:
            r = httpx.get(url, headers=UA, timeout=30.0, follow_redirects=True)
            if r.status_code != 200:
                report.append(f"    no cover upstream ({r.status_code}) — puzzle ships without one")
                return None
            source = Image.open(io.BytesIO(r.content)).convert("RGB")
            break
        except Exception as e:
            if attempt == 3:
                report.append(f"    cover fetch failed 3× ({e}) — puzzle ships without one")
                return None

    ratio = source.height / source.width
    target = (COVER_OUTPUT_WIDTH, round(COVER_OUTPUT_WIDTH * ratio))
    rel_dir = os.path.join(COVER_SUBPATH, f"p{puzzle_no:04d}")
    out_dir = os.path.join(site_root, rel_dir)
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)

    levels = []
    for level, width in enumerate(BLUR_WIDTHS):
        small = source.resize((width, max(1, round(width * ratio))), Image.LANCZOS)
        # Blow it back up smoothly, then blur: pixel blocks alone can still be
        # read as letterforms, so a following blur kills the residual edges.
        blown = small.resize(target, Image.BICUBIC).filter(
            ImageFilter.GaussianBlur(radius=max(2.2, 7.0 - level * 1.0))
        )
        rel_path = f"/{rel_dir}/b{level}.webp".replace("\\", "/")
        if not dry_run:
            blown.save(os.path.join(out_dir, f"b{level}.webp"), "WEBP", quality=80)
        levels.append(rel_path)

    return {"levels": levels, "credit": f"Project Gutenberg #{entry.gutenberg_id} (public domain)"}


# ── answer hiding (obfuscation, not secrecy — see module docstring) ──

def answer_hash(date: str, canonical_id: str) -> str:
    return hashlib.sha256(f"{date}|{canonical_id}".encode()).hexdigest()


def _xor_key(date: str, length: int) -> bytes:
    seed = hashlib.sha256(f"litheca-gtb-{date}".encode()).digest()
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:length])


def encode_reveal(payload: dict, date: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = _xor_key(date, len(raw))
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, key))).decode("ascii")


def decode_reveal(blob: str, date: str) -> dict:
    raw = base64.b64decode(blob)
    key = _xor_key(date, len(raw))
    return json.loads(bytes(a ^ b for a, b in zip(raw, key)).decode("utf-8"))


# ── site links ───────────────────────────────────────────────

def litheca_url(entry: PoolEntry, site_root: str) -> str:
    """Prefer a real published static page; fall back to the dynamic
    Summarizer keyed on canonical_id (`?b=` — the clean share slug summary.html
    already resolves)."""
    books_dir = os.path.join(site_root, "_books")
    if os.path.isdir(books_dir):
        for name in os.listdir(books_dir):
            if not name.endswith(".md"):
                continue
            try:
                head = open(os.path.join(books_dir, name), encoding="utf-8").read(2000)
            except Exception:
                continue
            m = re.search(r'^canonical_id:\s*"?([^"\n]+)"?\s*$', head, re.M)
            if m and m.group(1).strip() == entry.canonical_id:
                s = re.search(r'^slug:\s*"?([^"\n]+)"?\s*$', head, re.M)
                if s:
                    return f"/summary/{s.group(1).strip()}/"
    return f"/summary/?b={urllib.parse.quote(entry.canonical_id)}"


# ── assembly ─────────────────────────────────────────────────

def build_puzzle(entry: PoolEntry, day: dt.date, puzzle_no: int, site_root: str,
                 dry_run: bool, report: list[str]) -> dict | None:
    date = day.isoformat()
    raw = fetch_raw(entry.gutenberg_id)
    if not raw:
        report.append("    FAILED: text unavailable from every mirror")
        return None

    chunks = _chunk_text(strip_boilerplate(raw))
    quotes = pick_quotes(entry, chunks, report)
    if len(quotes) < QUOTES_PER_PUZZLE:
        # A weaker puzzle is not an acceptable substitute for no puzzle.
        report.append(f"    FAILED: only {len(quotes)} verified quote(s), need {QUOTES_PER_PUZZLE}")
        return None

    cover = bake_cover_levels(entry, puzzle_no, site_root, dry_run, report)
    reveal = {
        "id": entry.canonical_id,
        "title": entry.title,
        "author": entry.author,
        "year": entry.first_published,
        "url": litheca_url(entry, site_root),
        "cover": cover_url(entry.gutenberg_id),
        "gutenberg": f"https://www.gutenberg.org/ebooks/{entry.gutenberg_id}",
    }
    if entry.translator:
        reveal["translator"] = entry.translator

    return {
        "v": SCHEMA_VERSION,
        "n": puzzle_no,
        "date": date,
        "answer_hash": answer_hash(date, entry.canonical_id),
        # Lets the page say "same author — wrong book" on a miss without ever
        # holding the answer: the client hashes the author of the book the
        # player picked and compares. Leaks exactly one bit, and only about a
        # book the player already named.
        "author_hash": answer_hash(date, entry.author),
        "clues": build_clues(entry, quotes),
        "cover": cover,
        "reveal_enc": encode_reveal(reveal, date),
    }


def write_titles(data_dir: str, dry_run: bool) -> int:
    """The autocomplete list players choose from: the answerable pool PLUS the
    decoys built by scripts/build_gtb_decoys.py. Pool-only would mean guessing
    one book out of ~48, and would publish the answer set."""
    entries = [{"id": e.canonical_id, "title": e.title, "author": e.author} for e in GTB_POOL]
    seen = {e["id"] for e in entries}

    decoys_path = os.path.join(REPO_ROOT, "data", "gtb_decoys.json")
    if os.path.exists(decoys_path):
        with open(decoys_path, encoding="utf-8") as f:
            for row in json.load(f):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    entries.append(row)
    else:
        print("  ! data/gtb_decoys.json missing — picker will list the answer pool only")

    # Last line of defence against listing one book twice. Two entries for the
    # same work means a player can name the right book and be told they're
    # wrong, so this shouts rather than fixes — the decoy list is curated.
    def stem(title: str) -> frozenset:
        words = set()
        for w in re.split(r"[^a-z0-9]+", title.lower()):
            if w and w not in {"the", "a", "an", "of", "and", "or", "in", "on", "to"}:
                words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
        return frozenset(words)

    by_stem: dict[frozenset, list[dict]] = {}
    for entry in entries:
        by_stem.setdefault(stem(entry["title"]), []).append(entry)
    for group in by_stem.values():
        if len(group) > 1:
            listed = " / ".join(f"{g['title']} — {g['author']}" for g in group)
            print(f"  ! DUPLICATE WORK IN PICKER, fix data/gtb_decoys.json: {listed}")

    titles = sorted(entries, key=lambda t: t["title"].lower())
    if not dry_run:
        with open(os.path.join(data_dir, "titles.json"), "w", encoding="utf-8") as f:
            json.dump(titles, f, ensure_ascii=False, indent=0)
    return len(titles)


def read_epoch(data_dir: str) -> dt.date | None:
    """Puzzle #1's date. Pinned in manifest.json because puzzle numbers are
    derived from it and a shifting epoch would renumber puzzles people have
    already shared."""
    path = os.path.join(data_dir, "manifest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get("epoch")
        return dt.date.fromisoformat(value) if value else None
    except Exception:
        return None


def puzzle_number(day: dt.date, epoch: dt.date) -> int:
    """Wordle-style: the number IS the day offset. Regenerating a file, or a
    failed day in the middle of a batch, must never renumber anything — a
    running counter did exactly that and produced two puzzles numbered #5."""
    return (day - epoch).days + 1


def write_manifest(data_dir: str, epoch: dt.date, dry_run: bool) -> dict:
    dates = sorted(n[:-5] for n in os.listdir(data_dir)
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n)) if os.path.isdir(data_dir) else []
    manifest = {
        "v": SCHEMA_VERSION,
        "epoch": epoch.isoformat(),
        "first_date": dates[0] if dates else None,
        "latest_date": dates[-1] if dates else None,
        "count": len(dates),
    }
    if not dry_run:
        with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def rebake_covers(data_dir: str, site_root: str) -> int:
    """Re-cut every committed puzzle's blur levels from the current
    BLUR_WIDTHS. Tuning the ladder must not cost a Gemini call or churn clues
    a human already reviewed — the gid comes back out of the reveal payload."""
    from tools.gtb_pool import by_canonical_id

    done = 0
    for name in sorted(os.listdir(data_dir)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
            continue
        path = os.path.join(data_dir, name)
        with open(path, encoding="utf-8") as f:
            puzzle = json.load(f)
        reveal = decode_reveal(puzzle["reveal_enc"], puzzle["date"])
        entry = by_canonical_id(reveal["id"])
        if not entry:
            print(f"  {name}: {reveal['id']} is no longer in the pool — skipped")
            continue
        report: list[str] = []
        cover = bake_cover_levels(entry, puzzle["n"], site_root, False, report)
        for line in report:
            print(line)
        puzzle["cover"] = cover
        with open(path, "w", encoding="utf-8") as f:
            json.dump(puzzle, f, ensure_ascii=False, indent=1)
        print(f"  {name}: {reveal['title']} — {'5 levels' if cover else 'no cover'}")
        done += 1
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", help="first puzzle date, YYYY-MM-DD")
    ap.add_argument("--rebake-covers", action="store_true",
                    help="re-cut blur levels for existing puzzles; no Gemini calls")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--gid", type=int, help="force a single book by Gutenberg id")
    ap.add_argument("--out", default=DEFAULT_SITE, help="path to the bookhub site repo")
    ap.add_argument("--epoch", help="date of puzzle #1 (defaults to manifest.json's, "
                                     "then to --from). Changing it renumbers everything.")
    ap.add_argument("--dry-run", action="store_true", help="build and report, write nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace existing dated files")
    args = ap.parse_args()

    site_root = os.path.abspath(args.out)
    if not os.path.isdir(site_root):
        print(f"Site repo not found: {site_root}")
        return 2
    data_dir = os.path.join(site_root, DATA_SUBPATH)

    if args.rebake_covers:
        print(f"Re-baking cover levels at {BLUR_WIDTHS} — LOOK AT THE RESULT:")
        return 0 if rebake_covers(data_dir, site_root) else 1

    if not args.start:
        print("--from is required (or use --rebake-covers)")
        return 2
    if not gemini_client.is_configured():
        print("GEMINI_API_KEY missing — cannot source quotes. Aborting.")
        return 2
    if not args.dry_run:
        os.makedirs(data_dir, exist_ok=True)

    start = dt.date.fromisoformat(args.start)
    entries = [e for e in GTB_POOL if not args.gid or e.gutenberg_id == args.gid]
    if not entries:
        print(f"No pool entry with gid {args.gid}")
        return 2

    count = args.count if not args.gid else 1
    target_dates = {(start + dt.timedelta(days=i)).isoformat() for i in range(count)}
    used = used_canonical_ids(data_dir, ignore_dates=target_dates if args.overwrite else None)
    remaining = shuffled_pool(entries, start, [] if args.gid else used)

    epoch = (dt.date.fromisoformat(args.epoch) if args.epoch
             else read_epoch(data_dir) or start)

    print(f"Building up to {count} puzzle(s) into {data_dir} (puzzle #1 = {epoch}, "
          f"{len(remaining)} book(s) available)"
          f"{' (DRY RUN — nothing written)' if args.dry_run else ''}\n")

    made = failed = 0
    recent_authors: list[str] = []
    for i in range(count):
        day = start + dt.timedelta(days=i)
        date = day.isoformat()
        path = os.path.join(data_dir, f"{date}.json")
        if os.path.exists(path) and not args.overwrite:
            print(f"{date}  SKIP — file exists (use --overwrite)")
            continue

        puzzle_no = puzzle_number(day, epoch)
        if puzzle_no < 1:
            print(f"{date}  SKIP — earlier than the epoch {epoch}\n")
            continue

        # A book that can't ground two verified quotes hands the day to the
        # next book rather than leaving a hole in the calendar — a missing day
        # means the game shows "no puzzle today" to everyone.
        puzzle = None
        for entry in candidates_for_day(remaining, recent_authors)[:BOOKS_PER_DAY_ATTEMPTS]:
            report: list[str] = []
            print(f"{date}  #{puzzle_no}  {entry.title} — {entry.author}")
            try:
                puzzle = build_puzzle(entry, day, puzzle_no, site_root, args.dry_run, report)
            except Exception as e:
                report.append(f"    FAILED: {type(e).__name__}: {e}")
                puzzle = None
            for line in report:
                print(line)
            # Either way this book is spent for this run: it either became a
            # puzzle, or it just proved it can't make one today.
            remaining.remove(entry)
            if puzzle:
                recent_authors.append(entry.author)
                break
            print("    → trying another book for this day\n")

        if not puzzle:
            failed += 1
            print(f"    → {date} left with NO puzzle (a missing day beats a weak one)\n")
            continue

        if not args.dry_run:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(puzzle, f, ensure_ascii=False, indent=1)
        made += 1

        # ── the human-review block: everything the player will ever see ──
        print("    ┌─ REVIEW ─────────────────────────────────────────")
        for clue in puzzle["clues"]:
            label = clue.get("label") or f"Quote (chunk {clue['source'].get('chunk')})"
            body = clue["text"] if len(clue["text"]) <= 150 else clue["text"][:147] + "…"
            print(f"    │ {clue['i']}. {label}: {body}")
        cov = "5 blurred levels" if puzzle["cover"] else "NONE"
        print(f"    │ cover: {cov}   answer: {entry.title}")
        print(f"    │ reveal → {litheca_url(entry, site_root)}")
        print("    └──────────────────────────────────────────────────\n")

    titles_n = write_titles(data_dir, args.dry_run) if not args.dry_run else len(GTB_POOL)
    manifest = write_manifest(data_dir, epoch, args.dry_run) if not args.dry_run else {}

    print(f"{made} puzzle(s) written, {failed} refused.")
    if not args.dry_run:
        print(f"titles.json: {titles_n} entries · manifest: {manifest}")
    print("\nREAD EVERY CLUE ABOVE BEFORE COMMITTING. A quote that names a place,")
    print("a fact you cannot personally confirm, or a clue ladder that gives the")
    print("answer away at step 1 — delete the file and regenerate.")
    return 1 if failed and not made else 0


if __name__ == "__main__":
    raise SystemExit(main())
