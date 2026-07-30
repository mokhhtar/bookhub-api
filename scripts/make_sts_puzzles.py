"""
scripts/make_sts_puzzles.py — builds the daily "Spot the Slop" puzzle files.

    python scripts/make_sts_puzzles.py --from 2026-08-20 --count 3 --dry-run
    python scripts/make_sts_puzzles.py --from 2026-08-20 --count 30

Five pairs a day. Each pair is one real passage from a public-domain book and
one Gemini wrote in the same author's voice; the player picks the real one.
One pair would be a coin flip, five make 5/5 an achievement and 2/5 a signal.

CALIBRATION IS INVERTED FROM THE OBVIOUS. We are NOT trying to make the fake
as convincing as possible. If players usually lose, the game says "AI is
indistinguishable from great literature" — the opposite of what this site
argues. Aim for a 70-80% success rate, so the takeaway is "you can tell, if
you look". At review the question is "is this the right level?", never "did
it fool me?".

GROUNDING — the mirror image of Guess the Book:

    real passage   normalized substring match PROVING it is in the text
    fake passage   normalized substring match PROVING it is NOT

That second check is not a formality. Models reproduce famous text verbatim;
if Gemini "invents" a line that is actually in Moby-Dick then BOTH options are
real and the game is lying to the player. Only the mechanical check catches
it. The giveaway and proper-noun filters apply to both passages — a name in
one and not the other is a free answer that has nothing to do with prose.

The served JSON does NOT say which passage is fabricated. It carries a
file-level `contains_fabricated_text` marker so no future script mistakes any
of it for real book text, while the answer itself rides in the same
XOR+base64 reveal payload Guess the Book uses. Same honest limit as there:
this stops casual spoiling, not a determined reader.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Everything shared with the sibling game is imported, not copied — and this
# import comes FIRST because that module loads .env at import time. Pulling in
# gemini_client before it left the client built with no API key.
from make_gtb_puzzles import (  # noqa: E402
    DEFAULT_SITE, SCHEMA_VERSION, _has_stray_proper_noun, _looks_like_verse,
    _pace, answer_hash, decode_reveal, encode_reveal, litheca_url,
    puzzle_number, read_epoch, shuffled_pool,
)

from google.genai import types as genai_types  # noqa: E402

import gemini_client  # noqa: E402
from tools.gtb_pool import GTB_POOL, PoolEntry  # noqa: E402
from tools.quiz_core import _chunk_text, _normalize_for_match  # noqa: E402
from verify_gtb_pool import fetch_raw, strip_boilerplate  # noqa: E402

ROUNDS_PER_DAY = 5
MIN_PASSAGE_CHARS = 180
MAX_PASSAGE_CHARS = 420
LENGTH_TOLERANCE = 0.30      # the fake must be within ±30% of the real one
SAMPLE_CHUNKS = 10
OPENING_SKIP = 0.08
ENDING_SKIP = 0.94
DATA_SUBPATH = os.path.join("games", "data", "spot-the-slop")


def pick_real_passage(entry: PoolEntry, chunks: list[str], report: list[str]) -> dict | None:
    """A passage long enough to have a voice, verified to be in the book.

    Longer than a Guess-the-Book clue on purpose: two sentences don't carry
    enough style to judge, and style is the entire game here.
    """
    n = len(chunks)
    take = min(SAMPLE_CHUNKS, n)
    lo, hi = (round(n * OPENING_SKIP), round(n * ENDING_SKIP)) if n > 20 else (0, n - 1)
    indices = sorted({round(lo + i * (hi - lo) / max(take - 1, 1)) for i in range(take)})
    sampled = {i: chunks[i] for i in indices}
    excerpts = "\n\n".join(f"[Chunk {i}]\n{c}" for i, c in sampled.items())

    prompt = f"""Below are numbered excerpts from one novel. Choose up to 6 passages that best show the author's VOICE.

A passage is acceptable ONLY if every rule holds:
- Between {MIN_PASSAGE_CHARS} and {MAX_PASSAGE_CHARS} characters.
- Copied EXACTLY, character for character, from a single excerpt.
- Contains NO proper nouns — no character, place, ship or house names.
- Reads as continuous prose that stands on its own, not a fragment.
- Is the book's own writing, not something it quotes.

If fewer than 6 qualify, return fewer. Returning fewer is correct; altering a passage is not.

EXCERPTS:
{excerpts}

Return ONLY JSON: [{{"text": "...", "chunk_index": 0}}]"""

    config = genai_types.GenerateContentConfig(
        temperature=0.4, max_output_tokens=2048, response_mime_type="application/json")
    _pace()
    try:
        data = json.loads(gemini_client.generate(prompt, config))
    except Exception as e:
        report.append(f"    real-passage proposal failed: {type(e).__name__}")
        return None
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])

    normalized = {i: _normalize_for_match(c) for i, c in sampled.items()}
    banned = entry.banned_terms()

    for item in data if isinstance(data, list) else []:
        text = str((item or {}).get("text", "")).strip()
        short = text[:56].replace("\n", " ")
        if not (MIN_PASSAGE_CHARS <= len(text) <= MAX_PASSAGE_CHARS):
            continue
        needle = _normalize_for_match(text)
        found = next((i for i, nc in normalized.items() if needle in nc), None)
        if found is None:
            report.append(f"    dropped real (NOT VERBATIM): {short}…")
            continue
        if any(re.search(r"\b" + re.escape(t) + r"\b", needle) for t in banned):
            report.append(f"    dropped real (giveaway): {short}…")
            continue
        if _has_stray_proper_noun(text) or _looks_like_verse(text):
            report.append(f"    dropped real (proper noun / verse): {short}…")
            continue
        return {"text": text, "chunk": found}
    return None


def write_fake(entry: PoolEntry, real: str, chunks: list[str], report: list[str]) -> str | None:
    """Gemini writes in the author's voice — then we prove it is NOT the book."""
    target = len(real)
    prompt = f"""Write ONE original passage of prose in the style of {entry.author}, as it appears in {entry.first_published}.

Match the sample below in period, register, sentence rhythm and subject matter. Length must be close to {target} characters.

Hard rules:
- It must be ORIGINAL. Do not reproduce, adapt or lightly reword any sentence from any published book — least of all from this author.
- No proper nouns at all: no character, place or ship names.
- No quotation marks around the whole passage, no title, no preamble, no commentary. Output the prose only.

SAMPLE OF THE VOICE TO MATCH:
{real}

Return ONLY JSON: {{"text": "..."}}"""

    config = genai_types.GenerateContentConfig(
        temperature=0.9, max_output_tokens=1024, response_mime_type="application/json")
    _pace()
    try:
        text = str(json.loads(gemini_client.generate(prompt, config)).get("text", "")).strip()
    except Exception as e:
        report.append(f"    fake generation failed: {type(e).__name__}")
        return None

    short = text[:56].replace("\n", " ")
    if not text:
        return None
    if not (target * (1 - LENGTH_TOLERANCE) <= len(text) <= target * (1 + LENGTH_TOLERANCE)):
        report.append(f"    dropped fake (length {len(text)} vs real {target}): {short}…")
        return None

    # THE INVERSE CHECK. If the model reproduced real text, both options would
    # be real and the puzzle would have no correct answer.
    needle = _normalize_for_match(text)
    for chunk in chunks:
        if needle in _normalize_for_match(chunk):
            report.append(f"    dropped fake (REGURGITATED from the book): {short}…")
            return None

    if _has_stray_proper_noun(text):
        report.append(f"    dropped fake (proper noun): {short}…")
        return None
    banned = entry.banned_terms()
    if any(re.search(r"\b" + re.escape(t) + r"\b", needle) for t in banned):
        report.append(f"    dropped fake (giveaway): {short}…")
        return None
    return text


def flag_authorial_echo(entry: PoolEntry, fake: str, report: list[str]) -> bool:
    """Flag a fake that may be the author's OWN words from another work.

    The inverse check compares against THIS book's text, and that is not
    enough. The first trial run produced, as a fake Oscar Wilde, "To be
    natural is a pose, and the most irritating one I know" — Wilde's actual
    aphorism, which is nowhere in Dorian Gray's text and so passed cleanly.
    Shipping it would have presented Wilde's own line as AI slop, inverting
    the exact claim the game is built to make.

    Flags rather than drops: the same yes/no check used for borrowed lines in
    Guess the Book proved to have a high false-positive rate, and the human
    review gate is where this is decided. A flagged pair is not unusable — it
    is unusable UNTIL someone looks.
    """
    prompt = f"""PASSAGE:
{fake}

Is any sentence in this passage a known, published line by {entry.author}, or a close rewording of one?

Answer true ONLY if you recognise it as that author's actual writing. Generic period pastiche is not a match.

Return ONLY JSON: {{"echo": true or false}}"""
    try:
        _pace()
        verdict = json.loads(gemini_client.generate(prompt, genai_types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=256, response_mime_type="application/json")))
        if verdict.get("echo") is True:
            report.append(f"    REVIEW — the fake may echo {entry.author}'s own published words")
            return True
    except Exception as e:
        report.append(f"    note: authorial-echo check skipped ({type(e).__name__})")
    return False


def build_round(entry: PoolEntry, site_root: str, report: list[str]) -> dict | None:
    raw = fetch_raw(entry.gutenberg_id)
    if not raw:
        report.append("    text unavailable from every mirror")
        return None
    chunks = _chunk_text(strip_boilerplate(raw))
    real = pick_real_passage(entry, chunks, report)
    if not real:
        report.append("    no verified real passage — book skipped")
        return None
    fake = write_fake(entry, real["text"], chunks, report)
    if not fake:
        report.append("    no usable fake — book skipped")
        return None
    echo = flag_authorial_echo(entry, fake, report)
    return {
        "echo_flagged": echo,
        "author": entry.author,
        "real": real["text"],
        "fake": fake,
        "chunk": real["chunk"],
        "book": {"title": entry.title, "url": litheca_url(entry, site_root),
                 "gutenberg": f"https://www.gutenberg.org/ebooks/{entry.gutenberg_id}"},
        "id": entry.canonical_id,
    }


def build_day(day: dt.date, puzzle_no: int, pool: list[PoolEntry], site_root: str,
              report: list[str]) -> dict | None:
    date = day.isoformat()
    rounds, used = [], []
    for entry in list(pool):
        if len(rounds) >= ROUNDS_PER_DAY:
            break
        report.append(f"  · {entry.title} — {entry.author}")
        r = build_round(entry, site_root, report)
        pool.remove(entry)
        if r:
            rounds.append(r)
            used.append(entry)

    if len(rounds) < ROUNDS_PER_DAY:
        report.append(f"    FAILED: only {len(rounds)}/{ROUNDS_PER_DAY} rounds")
        pool.extend(used)
        return None

    # Which side is real is decided per round and never written in the clear.
    served, reveal = [], []
    for i, r in enumerate(rounds):
        real_first = (sum(ord(c) for c in f"{date}{i}{r['id']}") % 2) == 0
        passages = [r["real"], r["fake"]] if real_first else [r["fake"], r["real"]]
        served.append({"i": i, "author": r["author"], "passages": passages,
                       "answer_hash": answer_hash(date, f"{i}|{0 if real_first else 1}")})
        reveal.append({"i": i, "real_index": 0 if real_first else 1,
                       "echo_flagged": r.get("echo_flagged", False), **r["book"]})

    return {
        "v": SCHEMA_VERSION,
        "n": puzzle_no,
        "date": date,
        # Declared at file level so nothing downstream can mistake these for
        # real book text, without saying WHICH passage is the fabricated one.
        "contains_fabricated_text": True,
        "rounds": served,
        "reveal_enc": encode_reveal(reveal, date),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--out", default=DEFAULT_SITE)
    ap.add_argument("--epoch")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    if not gemini_client.is_configured():
        print("GEMINI_API_KEY missing — aborting.")
        return 2
    site_root = os.path.abspath(args.out)
    data_dir = os.path.join(site_root, DATA_SUBPATH)
    if not args.dry_run:
        os.makedirs(data_dir, exist_ok=True)

    start = dt.date.fromisoformat(args.start)
    epoch = (dt.date.fromisoformat(args.epoch) if args.epoch
             else read_epoch(data_dir) or start)
    pool = shuffled_pool(list(GTB_POOL), start, [])

    print(f"Building {args.count} day(s) x {ROUNDS_PER_DAY} pairs into {data_dir}"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    made = failed = 0
    for i in range(args.count):
        day = start + dt.timedelta(days=i)
        path = os.path.join(data_dir, f"{day.isoformat()}.json")
        if os.path.exists(path) and not args.overwrite:
            print(f"{day}  SKIP — exists")
            continue
        report: list[str] = []
        print(f"{day}  #{puzzle_number(day, epoch)}")
        puzzle = build_day(day, puzzle_number(day, epoch), pool, site_root, report)
        for line in report:
            print(line)
        if not puzzle:
            failed += 1
            print("    → no file written\n")
            continue
        if not args.dry_run:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(puzzle, f, ensure_ascii=False, indent=1)
        made += 1
        answers = decode_reveal(puzzle["reveal_enc"], puzzle["date"])
        print("    ┌─ REVIEW — read BOTH passages of every pair ─────────")
        for rnd, ans in zip(puzzle["rounds"], answers):
            print(f"    │ {rnd['i']}. {ans['title']} — {rnd['author']}")
            for idx, p in enumerate(rnd["passages"]):
                mark = "REAL" if idx == ans["real_index"] else "FAKE"
                warn = "  ⚠ MAY ECHO THE AUTHOR" if (mark == "FAKE" and ans.get("echo_flagged")) else ""
                print(f"    │    [{mark}] {p[:150]}{warn}")
        print("    └─────────────────────────────────────────────────────\n")

    print(f"{made} day(s) written, {failed} refused.")
    print("\nJudge every pair on ONE question: is the fake convincing at the RIGHT")
    print("level? Obvious pastiche is a boring round. A fake that reads better than")
    print("the original argues the opposite of what this site exists to say.")
    return 1 if failed and not made else 0


if __name__ == "__main__":
    raise SystemExit(main())
