"""
scripts/akinator/propose_questions.py — ask the model for new questions,
then prove every one of them against real data before a human ever reads it.

    python scripts/akinator/propose_questions.py

WHY THIS EXISTS. The game's ~48 questions were each hand-authored: someone
noticed a dimension, hand-picked keywords, hand-counted how many books they
matched. That process does not scale with the corpus — it never ran out of
books to discriminate between, it ran out of a person's time to keep
inventing dimensions. This automates the INVENTING, not the deciding: the
model proposes candidates, this script measures every one of them against
the real shipped corpus (or, for a prose-only candidate, a real sample), and
NOTHING here ever writes to `features.py` or `traits.py`. It drafts the
exact tuple/entry text ready to paste and stops there — see the note at the
bottom of this docstring for why that stays a human's job.

TWO KINDS OF CANDIDATE, because the game already has two kinds of question:
  * SUBJECT — answerable from a library catalogue's subject tags. Measured
    EXACTLY, over the whole shipped corpus, using the identical matching
    discipline `features.extract()` uses for every existing subject rule
    (whole-word/stem matching via `_compile`, `is_stop_subject` filtering,
    `GENRE_MIN_SUPPORT` corroboration for `genre:` keys) — no shortcut, no
    approximation.
  * PROSE — only judgable by reading a description, like the existing
    `traits.py` vocabulary. No keyword shortcut exists, so this measures a
    SAMPLE via one short classification call, using `traits.py`'s own
    prompt/parse machinery with a synthetic one-entry vocabulary — the same
    "omit rather than guess" grounding discipline a shipped trait gets.

Every candidate is also checked for two kinds of redundancy before it is
shown to anyone: an EXACT wording collision with something that already
exists (the same check `build_matrix._drop_duplicate_wordings` runs at build
time), and a NEAR-DUPLICATE correlation check against the live shipped
matrix — a candidate that clears the frequency floor but agrees with an
existing question on >90% of sampled books tells the engine almost nothing
new, which is exactly the `t:magic`/`theme:magic` mistake, caught before it
ships instead of after.

Output: `bookhub/games/data/akinator/question_candidates.json` — same
directory, same flat-list shape, as `theme_requests.json`. Reviewed through
the admin page's "Mined questions" tab.

WHY THE PASTE STAYS MANUAL. Every real corpus-corruption incident this
project has hit — "science fiction" collapsing to "science", `elf` matching
inside `self-help`, `war` matching `Warsaw` — was a SEMANTIC mistake no
frequency or correlation number can see. Only a human reading the actual
keyword list and the actual matched titles catches that class of error, and
a model's first-draft keyword list is exactly as prone to it as a human's
first draft was. This script computes every number that CAN be computed and
stops exactly at the line only a person should cross.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import build_matrix                                                # noqa: E402
import gemini_client                                                # noqa: E402
import parity_trace                                                 # noqa: E402
from author_traits import AUTHOR_QUESTIONS                          # noqa: E402
from corpus_filter import SHIPPED_BOOKS                             # noqa: E402
from features import (GENRE_MIN_SUPPORT, MAX_FREQ, MIN_FREQ,        # noqa: E402
                      QUESTION_TEXT, STRUCTURAL_QUESTIONS, _compile,
                      is_stop_subject, keeps_question, normalize)
from traits import (TRAIT_QUESTIONS, build_batch_prompt,            # noqa: E402
                    parse_batch_response)
from work_traits import WORK_QUESTIONS                              # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
DESC_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")

# Same sibling-checkout convention build_matrix.py's own --out-dir example
# uses: a local script writes straight into the bookhub working tree, the
# owner commits it through their normal git workflow. Same directory
# theme_requests.json already lives in.
ARTIFACTS_DIR = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator"))
OUT_PATH = os.path.join(ARTIFACTS_DIR, "question_candidates.json")

_KEY_RE = re.compile(r"^[a-z]+:[a-z0-9_]+$")

# How many sampled books must resolve on BOTH sides before a correlation is
# trusted at all. Below this, "90% agreement" could be 9 of 10 books, which
# is noise wearing a percentage.
MIN_CORRELATION_N = 20
NEAR_DUPLICATE_THRESHOLD = 0.90


# ---------------------------------------------------------------------------
# What already exists
# ---------------------------------------------------------------------------

def existing_question_ids() -> dict[str, str]:
    """Every id this game already knows -> its CURRENT wording (admin
    overrides applied), via build_matrix.question_text() — the exact chain
    the build itself uses, so "already exists" means what a real build
    would mean by it."""
    ids = (set(QUESTION_TEXT) | set(STRUCTURAL_QUESTIONS)
           | set(AUTHOR_QUESTIONS) | set(WORK_QUESTIONS) | set(TRAIT_QUESTIONS))
    return {i: build_matrix.question_text(i) for i in sorted(ids)}


def load_shipped_corpus() -> list[dict]:
    """The population `select_features()` will actually score a real build
    against — not the full ~19,890-book candidate pool. `--with-fandom`
    because that is what the live game ships with today."""
    build_matrix.WITH_FANDOM = True
    docs = build_matrix.load_corpus(CORPUS_PATH)
    return docs[:SHIPPED_BOOKS]


# ---------------------------------------------------------------------------
# The proposal call
# ---------------------------------------------------------------------------

def build_proposal_prompt(existing: dict[str, str], sample: list[dict],
                          descriptions: dict[str, str],
                          max_candidates: int) -> str:
    existing_lines = "\n".join(f'- "{k}": {v}' for k, v in existing.items())
    blocks = []
    for d in sample:
        subjects = ", ".join((d.get("subject") or [])[:12])
        desc = (descriptions.get(d.get("key")) or "")[:500]
        blocks.append(f"TITLE: {d.get('title') or ''}\n"
                      f"SUBJECTS: {subjects}\n"
                      f"DESCRIPTION: {desc}")
    return (
        "You help design a Twenty-Questions-style guessing game about "
        f"books. The game already asks these {len(existing)} yes/no "
        "questions — never propose one of these again, even reworded:\n\n"
        f"{existing_lines}\n\n"
        "Below are real books from the game's library, each with its "
        "catalogue subjects and description.\n\n" + "\n\n".join(blocks) +
        "\n\n"
        f"Propose up to {max_candidates} NEW yes/no questions a player "
        "could answer about a book from memory, without looking anything "
        "up, that would help separate these books from each other and are "
        "NOT already covered above.\n\n"
        "Rules, in order of importance:\n"
        "1. Do not propose a question that duplicates or rewords one "
        "already listed above, even in different words — 'does it have "
        "supernatural elements' and 'is there magic in it' are the SAME "
        "question to a player.\n"
        "2. A question must split books meaningfully — not something "
        "almost every book, or almost none of them, would answer yes to.\n"
        "3. Declare each candidate as SUBJECT (answerable by matching "
        "keywords a library cataloguer might use — give 4 to 10 keywords "
        "or stems; a trailing * means stem, e.g. \"chef*\") or PROSE (only "
        "judgable by reading a description — give a one-sentence "
        "definition of exactly what makes it true, written the way a "
        "grounded classifier is told what to look for, not a vague "
        "impression).\n"
        "4. If you cannot think of a genuinely new, clean, answerable "
        "question, propose fewer than the maximum. Proposing nothing is "
        "better than proposing a bad one.\n\n"
        "Reply with JSON only, in this exact shape:\n"
        '{"candidates": [\n'
        '  {"key": "genre:cooking", "type": "subject", '
        '"question": "Is it about cooking or recipes?", '
        '"keywords": ["cooking", "recipes", "culinary", "chef*"], '
        '"rationale": "one sentence"},\n'
        '  {"key": "t:example", "type": "prose", '
        '"question": "...", "definition": "...", "rationale": "..."}\n'
        "]}\n"
    )


def parse_proposals(raw: str, existing_ids: set[str]) -> list[dict]:
    """Model JSON -> validated candidates. Anything malformed, or colliding
    on id with something that already exists, is dropped rather than kept
    with a caveat — a candidate this script cannot vouch for should not
    reach the reviewer at all."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    rows = data.get("candidates")
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        rtype = row.get("type")
        question = row.get("question")
        if not (isinstance(key, str) and _KEY_RE.match(key)):
            continue
        if key in existing_ids or key in seen:
            continue
        if rtype not in ("subject", "prose"):
            continue
        if not isinstance(question, str) or not question.strip():
            continue
        rationale = str(row.get("rationale") or "")[:300]

        if rtype == "subject":
            kws = row.get("keywords")
            if (not isinstance(kws, list) or not kws
                    or not all(isinstance(k, str) and k.strip() for k in kws)):
                continue
            out.append({"key": key, "type": "subject",
                       "question": question.strip(),
                       "keywords": [k.strip() for k in kws],
                       "rationale": rationale})
        else:
            defn = row.get("definition")
            if not isinstance(defn, str) or not defn.strip():
                continue
            out.append({"key": key, "type": "prose",
                       "question": question.strip(),
                       "definition": defn.strip(), "rationale": rationale})
        seen.add(key)
    return out


# ---------------------------------------------------------------------------
# Measurement — subject candidates get an exact answer, no LLM involved
# ---------------------------------------------------------------------------

def _book_signals(doc: dict) -> set[str]:
    """The exact normalized signal set features.extract() builds before any
    rule sees it — content subjects (stop-subjects dropped) plus place/time,
    deduplicated. A candidate is measured against the same input a real
    SUBJECT_RULES entry would see, not an approximation of it."""
    raw_subjects = doc.get("subject") or []
    normed = [normalize(s) for s in raw_subjects]
    content = [n for n in normed if n and not is_stop_subject(n)]
    signals = set(content)
    for extra in (doc.get("place") or []) + (doc.get("time") or []):
        n = normalize(extra)
        if n:
            signals.add(n)
    return signals


def measure_subject_candidate(key: str, keywords: list[str],
                              docs: list[dict]) -> dict:
    """Real, exact frequency over the whole shipped corpus. Mirrors
    features.extract()'s support-counting: a signal counts once no matter
    how many of the candidate's keywords it matches, and a genre-shaped key
    needs GENRE_MIN_SUPPORT independent signals — exactly like every
    shipped genre question, for exactly the reason GENRE_MIN_SUPPORT
    exists (see features.py: Moby Dick and one stray "History" subject).
    """
    patterns = [_compile(k) for k in keywords]
    needed = GENRE_MIN_SUPPORT if key.startswith("genre:") else 1
    answers: dict[str, bool] = {}
    present_titles, absent_titles = [], []
    for doc in docs:
        support = sum(1 for s in _book_signals(doc)
                      if any(p.search(s) for p in patterns))
        yes = support >= needed
        work_key = doc.get("key")
        if work_key:
            answers[work_key] = yes
        (present_titles if yes else absent_titles).append(doc.get("title") or "")

    n = len(docs)
    freq = (len(present_titles) / n) if n else 0.0
    return {
        "measured_on": "full_shipped_corpus", "sample_size": n,
        "frequency": round(freq, 4),
        "passes_band": keeps_question(key, freq),
        "example_present": present_titles[:4],
        "example_absent": absent_titles[:4],
        "_answers": answers,   # consumed by check_collisions, stripped before writing out
    }


# ---------------------------------------------------------------------------
# Measurement — prose candidates need a sampled classification call
# ---------------------------------------------------------------------------

def _short_config():
    """A judgment on ONE label needs far less room than the shared client's
    4096-token default. Same reasoning extract_traits.py's own
    _trait_config() states for itself — built locally rather than editing
    gemini_client.DEFAULT_CONFIG, which /summary and the quiz generators
    genuinely need at full size."""
    from google.genai import types as genai_types
    return genai_types.GenerateContentConfig(
        temperature=gemini_client.DEFAULT_CONFIG.temperature,
        max_output_tokens=800,
    )


def measure_prose_candidate(key: str, question: str, definition: str,
                            sample: list[dict],
                            descriptions: dict[str, str]) -> dict:
    """Sampled frequency only — there is no keyword shortcut for prose. One
    short classification call per batch of 8, using traits.py's own
    build_batch_prompt/parse_batch_response with a synthetic one-entry
    vocabulary, so a candidate gets the identical "omit rather than guess"
    grounding a shipped trait gets. A book the model does not assert the
    label for is UNKNOWN, never counted as a determined "no" — the same
    rule every trait in this game already follows, and the reason a prose
    candidate's `_answers` map below only ever holds True values.
    """
    rows = [(d.get("title") or "", (d.get("author_name") or [""])[0],
             descriptions[d["key"]], d.get("key"))
            for d in sample if d.get("key") in descriptions]
    vocab = {key: (question, definition)}
    config = _short_config()

    answers: dict[str, bool] = {}
    present_titles: list[str] = []
    determined = 0
    BATCH = 8
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        prompt_rows = [(t, a, txt) for t, a, txt, _k in chunk]
        try:
            raw = gemini_client.generate(build_batch_prompt(prompt_rows, vocab), config)
        except Exception:  # noqa: BLE001 — a failed batch is skipped, not fatal
            continue
        parsed = parse_batch_response(raw, len(chunk), vocab)
        if parsed is None:
            continue      # malformed reply: skip rather than guess alignment
        determined += len(chunk)
        for (title, _a, _txt, work_key), labels in zip(chunk, parsed):
            if key in labels:
                present_titles.append(title)
                if work_key:
                    answers[work_key] = True

    freq = (len(present_titles) / determined) if determined else 0.0
    return {
        "measured_on": "sample", "sample_size": determined,
        "frequency": round(freq, 4),
        "passes_band": (MIN_FREQ <= freq <= MAX_FREQ) if determined else False,
        "example_present": present_titles[:4],
        "example_absent": [],   # a prose candidate never determines a "no"
        "_answers": answers,
    }


# ---------------------------------------------------------------------------
# Redundancy — exact wording collision, and correlation against the shipped matrix
# ---------------------------------------------------------------------------

def load_shipped_states() -> tuple[dict[str, int], list[dict], list[str]]:
    """work_key -> row index, the decoded present/unknown state per shipped
    book, and the shipped question ids — read straight from the artifacts
    the browser plays, via the exact reconstruction parity_trace.py already
    does. Reused, not re-derived."""
    meta, questions, books_json, raw = parity_trace.load_artifacts(ARTIFACTS_DIR)
    books = parity_trace.books_from_artifacts(meta, questions, books_json, raw)
    qids = [q["id"] for q in questions]
    by_key = {b["key"]: i for i, b in enumerate(books) if b.get("key")}
    return by_key, books, qids


def _shipped_state(book: dict, qid: str) -> bool | None:
    if qid in book["present"]:
        return True
    if qid in book["unknown"]:
        return None
    return False


def check_collisions(question: str, answers: dict[str, bool],
                     existing_wordings: dict[str, str],
                     shipped_by_key: dict[str, int], shipped_books: list[dict],
                     shipped_qids: list[str]) -> dict:
    exact = next((eid for eid, etext in existing_wordings.items()
                 if etext == question), None)

    near: list[dict] = []
    for qid in shipped_qids:
        agree = total = 0
        for work_key, cand_val in answers.items():
            idx = shipped_by_key.get(work_key)
            if idx is None:
                continue
            shipped_val = _shipped_state(shipped_books[idx], qid)
            if shipped_val is None:
                continue
            total += 1
            agree += shipped_val == cand_val
        if total >= MIN_CORRELATION_N and agree / total > NEAR_DUPLICATE_THRESHOLD:
            near.append({"id": qid, "agreement": round(agree / total, 2), "n": total})
    near.sort(key=lambda x: -x["agreement"])
    return {"exact_wording_collision": exact, "near_duplicate": near[:3],
            "exclusive_with": _exclusive_with(answers, shipped_by_key,
                                              shipped_books, shipped_qids)}


# How rarely two questions may BOTH be true before they are worth declaring
# mutually exclusive. Expressed as a lift — observed co-occurrence over what
# independence predicts — so it does not need a per-pair threshold: 0.10
# means "they land together at a tenth of the rate chance alone would give".
EXCLUSIVE_MAX_LIFT = 0.10

# And both must actually happen often enough for "they never co-occur" to be
# a fact rather than an absence of data. Two questions at 1% each are
# expected to overlap on half a book in 5,000; observing zero says nothing.
EXCLUSIVE_MIN_SUPPORT = 0.03


def _exclusive_with(answers: dict[str, bool], shipped_by_key: dict[str, int],
                    shipped_books: list[dict], shipped_qids: list[str]) -> list[dict]:
    """Shipped questions this candidate can almost never be true alongside.

    A DIFFERENT CHECK FROM `near_duplicate`, AND THE ONE THAT WAS MISSING.
    Duplication and exclusion are opposite signatures of the same statistic:

        duplicate   P(A | B) ~ 1   ->  drop one, they ask the same thing
        exclusive   P(A | B) ~ 0   ->  KEEP both, but never ask the second
                                       after a firm yes to the first

    The correlation check above only ever fired on the first, so a candidate
    like "is it about China?" could ship beside "is it about France?" with
    nothing noticing, and a player who answered yes to one would be asked the
    other — the exact experience `EXCLUSIVE_GROUPS` exists to prevent, and
    the one the owner asked about.

    Run over the shipped matrix it finds three real pairs nobody has
    declared — genre:adventure + genre:psychology, t:otherworld +
    t:realevents, genre:fantasy + t:realevents — and recovers the author
    group (nonwestern at lift 0.00, british at 0.065).

    IT DOES NOT RECOVER EVERY DECLARED GROUP, and that is the honest
    headline rather than a shortfall to tune away. `place:usa` and
    `place:britain` are both true of 66 books; `audience:children` and
    `audience:ya` of 351. Those groups are not exclusive IN THE DATA at all
    — they are a promise to the player, paid for by throwing away a real
    "yes" on the overlap, and the promise is worth it only because it
    measured 65.0% against 58.3% with no exclusions.

    So this proposes the pairs where the corpus is unambiguous and stays
    silent on the ones that are a judgement about the game. Declaring those
    is a person's call, which is why the admin page has a form for it and
    this function has no verdict. Same line the rest of this file draws:
    compute every number that can be computed, stop before the call.
    """
    n_books = len(shipped_books)
    cand_yes = {k for k, v in answers.items() if v}
    if len(cand_yes) < n_books * EXCLUSIVE_MIN_SUPPORT:
        return []

    out = []
    for qid in shipped_qids:
        other = {i for i, b in enumerate(shipped_books)
                 if _shipped_state(b, qid) is True}
        if len(other) < n_books * EXCLUSIVE_MIN_SUPPORT:
            continue
        cand_idx = {shipped_by_key[k] for k in cand_yes if k in shipped_by_key}
        if not cand_idx:
            continue
        both = len(cand_idx & other)
        expected = len(cand_idx) * len(other) / n_books
        lift = both / expected if expected else 0.0
        if lift <= EXCLUSIVE_MAX_LIFT:
            out.append({"id": qid, "both": both,
                        "expected": round(expected, 1), "lift": round(lift, 3)})
    out.sort(key=lambda x: x["lift"])
    return out[:3]


def estimate_byte_cost(current_question_count: int, shipped_books: int) -> str:
    """The exact bytes_per_row arithmetic build_matrix.pack_matrix() uses,
    computed instead of remembered — the `t:animals` precedent (FORCE_DROP,
    scripts/akinator/features.py) was decided by exactly this arithmetic
    done by hand."""
    before = (current_question_count + 3) // 4
    after = (current_question_count + 1 + 3) // 4
    n = current_question_count + 1
    if after > before:
        delta_kb = (after - before) * shipped_books / 1024
        return (f"would be question #{n} of {current_question_count} -> "
                f"bytes_per_row {before}->{after} (+{delta_kb:.1f} KB first paint)")
    return (f"would be question #{n} of {current_question_count} -> "
            f"free (bytes_per_row stays {before})")


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=150,
                    help="books shown to the model when proposing")
    ap.add_argument("--measure-sample", type=int, default=150,
                    help="held-out books used to measure a PROSE candidate; "
                         "disjoint from --sample so the model isn't just "
                         "re-describing what it was shown")
    ap.add_argument("--max-candidates", type=int, default=8)
    ap.add_argument("--seed", type=int, default=None,
                    help="deterministic sampling; default is today's date, "
                         "so a re-run the same day reproduces the same run "
                         "and a run tomorrow samples differently")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    if not os.path.exists(DESC_PATH):
        print("No descriptions yet — run harvest_descriptions.py first.")
        return
    with open(DESC_PATH, encoding="utf-8") as fh:
        descriptions = json.load(fh)

    print("Loading the shipped corpus...")
    docs = load_shipped_corpus()
    with_desc = sum(1 for d in docs if d.get("key") in descriptions)
    print(f"{len(docs)} books in the shipped population, "
          f"{with_desc} with a harvested description.\n")

    existing = existing_question_ids()
    print(f"{len(existing)} question ids already exist across every "
          f"vocabulary — the model is told about every one of them.\n")

    seed = args.seed if args.seed is not None else int(
        datetime.date.today().strftime("%Y%m%d"))
    pool = [d for d in docs if d.get("key") in descriptions]
    rng = random.Random(seed)
    shown = rng.sample(pool, min(args.sample, len(pool)))
    shown_keys = {d["key"] for d in shown}
    holdout_pool = [d for d in pool if d["key"] not in shown_keys]
    holdout = rng.sample(holdout_pool, min(args.measure_sample, len(holdout_pool)))

    print(f"Asking the model for up to {args.max_candidates} candidate(s), "
          f"grounded on {len(shown)} sampled books (seed {seed})...")
    prompt = build_proposal_prompt(existing, shown, descriptions,
                                   args.max_candidates)
    raw = gemini_client.generate(prompt)
    candidates = parse_proposals(raw, set(existing))
    print(f"{len(candidates)} candidate(s) survived validation "
          f"(malformed or id-colliding proposals are dropped silently).\n")
    if not candidates:
        print("Nothing to measure. Nothing written.")
        return

    print("Loading the shipped matrix for the redundancy check...")
    shipped_by_key, shipped_books, shipped_qids = load_shipped_states()
    current_question_count = len(shipped_qids)

    today = datetime.date.today().isoformat()
    results = []
    for i, cand in enumerate(candidates, 1):
        print(f"  measuring {i}/{len(candidates)}: {cand['key']} "
              f"({cand['type']})...")
        if cand["type"] == "subject":
            measured = measure_subject_candidate(cand["key"], cand["keywords"], docs)
        else:
            measured = measure_prose_candidate(
                cand["key"], cand["question"], cand["definition"],
                holdout, descriptions)

        answers = measured.pop("_answers")
        collision = check_collisions(cand["question"], answers, existing,
                                     shipped_by_key, shipped_books, shipped_qids)
        byte_cost = estimate_byte_cost(current_question_count, len(shipped_books))

        entry = {
            "id": f"cand-{today}-{i:02d}",
            "generated": today,
            "status": "pending",
            "type": cand["type"],
            "key": cand["key"],
            "question": cand["question"],
            "measured": measured,
            "collision": collision,
            "byte_cost": byte_cost,
            "rationale": cand["rationale"],
        }
        if cand["type"] == "subject":
            entry["keywords"] = cand["keywords"]
            entry["draft_rule"] = (
                f'    ("{cand["key"]}", "{cand["question"]}",\n'
                f'     {cand["keywords"]!r}),')
        else:
            entry["definition"] = cand["definition"]
            entry["draft_entry"] = (
                f'    "{cand["key"]}": (\n'
                f'        "{cand["question"]}",\n'
                f'        "{cand["definition"]}"),')
        results.append(entry)

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    existing_pending = []
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            try:
                existing_pending = json.load(fh)
            except json.JSONDecodeError:
                existing_pending = []
        if not isinstance(existing_pending, list):
            existing_pending = []
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(existing_pending + results, fh, ensure_ascii=False, indent=1)

    print(f"\n{len(results)} candidate(s) -> {args.out}")
    print("Nothing was written to features.py or traits.py — review through "
          "the admin page's Mined questions tab, then paste the drafted "
          "rule by hand.\n")
    for r in results:
        band = "PASSES" if r["measured"]["passes_band"] else "below floor"
        dup = (f", near-dup of {r['collision']['near_duplicate'][0]['id']}"
              if r["collision"]["near_duplicate"] else "")
        exact = (" — EXACT WORDING COLLISION with "
                f"{r['collision']['exact_wording_collision']}"
                if r["collision"]["exact_wording_collision"] else "")
        print(f"  {r['key']:<20} {r['measured']['frequency']:>6.1%}  "
              f"{band}{dup}{exact}")


if __name__ == "__main__":
    main()
