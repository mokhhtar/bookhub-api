"""
scripts/akinator/extract_traits.py — run the model over the descriptions.

    python scripts/akinator/extract_traits.py --calibrate   # measure first
    python scripts/akinator/extract_traits.py --limit 5000

MODEL: `gemini-3.1-flash-lite` through the project's shared client —
temperature 0.3, thinking "low", rotating across GEMINI_API_KEYS, falling
back to Groq when every key fails. Free tier throughout, nothing new added.
That tier is not a compromise: this is classification against a fixed list
from supplied text, which `gemini_client.py`'s own docstring describes as
"not a hard reasoning task… fast, cheap, instruction-following". Every call
is BUILD TIME, baked into static files, never in the play path.

CALIBRATE BEFORE TRUSTING IT. `--calibrate` runs the extractor over the
books we publish and compares its labels against the themes a human already
reviewed on those pages. That is a real accuracy number on answers we
already own, and it is the reason not to argue about model choice in the
abstract — if flash-lite disagrees badly there, that measurement is the
argument for a bigger model. Not before.

WHAT A DISAGREEMENT MEANS, and why the number is not a simple accuracy. Our
page themes are editorial and sparse — a book about a sea voyage may simply
not have "the sea" among its four listed themes. So a model label our pages
lack is not necessarily wrong. The number worth watching is the reverse:
**themes our pages DO assert that the model misses**, which is a real
failure to read.

Output: data/akinator_traits.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from features import _compile, normalize            # noqa: E402
from site_books import load_site_books              # noqa: E402
from traits import (BATCH_SIZE, NEGATIVE_PREFIX, OUT_PATH,  # noqa: E402
                    TRAITS, allows_negatives, build_batch_prompt,
                    build_prompt, parse_batch_response, parse_response,
                    split_labels)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_PATH = os.path.join(REPO_ROOT, "data", "akinator_corpus.jsonl")
DESC_PATH = os.path.join(REPO_ROOT, "data", "akinator_descriptions.json")
CALIBRATION_PATH = os.path.join(REPO_ROOT, "data", "akinator_calibration.json")

# --source fandom. Same labeller, same vocabulary, same prompt — a
# different shelf of books entirely, and a SEPARATE output file because
# the two are keyed differently and must never merge by accident: Open
# Library rows are keyed "/works/OL…W" and these have no such key, only a
# title. Whether a Fandom label ends up beside an Open Library one is a
# question for whatever wires these into the corpus, not for the
# labeller.
FANDOM_TEXT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_text.json")
FANDOM_OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_traits.json")

# Cells the owner decided BY HAND, which no harvest may overwrite.
#
# Same file the drain refuses to vote on, read the same way build_matrix.py
# reads question_overrides.json — out of the sibling checkout, because it is
# a shipped artifact and this is a build-time script.
#
# WHAT THIS DOES AND DOES NOT PROTECT, stated because the honest version is
# narrower than it sounds. A locked cell also lives in `overrides.json`,
# which BOTH engines apply on top of the packed matrix at play time, so its
# value already wins no matter what the harvest writes. This guard is about
# the layer underneath: the base cell the audit compares a clamp against
# ("a clamp is redundant only when it equals what the engine would answer
# WITHOUT it"), and the value that would be left standing if the owner ever
# cleared the lock. A hand judgement should not quietly become a model guess
# the moment it is unlocked.
LOCKED_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator",
    "overrides_locked.json"))


def load_locked() -> dict[str, set[str]]:
    """work_key -> the trait keys the owner has decided by hand.

    Only TRAITS keys are kept: the file also locks author and genre cells,
    which no harvest touches, and carrying them would make the "skipped"
    count in the run summary describe protections that were never at risk.
    A missing or malformed file protects nothing, which is the pre-existing
    behaviour and loses a lock rather than an entire run.
    """
    try:
        with open(LOCKED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, set[str]] = {}
    for key, ids in data.items():
        if not isinstance(key, str) or not isinstance(ids, list):
            continue
        keep = {q for q in ids if isinstance(q, str) and q in TRAITS}
        if keep:
            out[key] = keep
    return out


def _trait_of(label: str) -> str:
    """The trait a stored label is about, positive or negative."""
    return label[len(NEGATIVE_PREFIX):] if label.startswith(NEGATIVE_PREFIX) else label

# Which of our editorial theme phrases assert which trait. Used ONLY by
# --calibrate, to check the model against a human judgment — never to
# produce labels, because it would inherit the sparsity that makes our
# themes unusable as features in the first place.
#
# MATCHED WHOLE-WORD, via features._compile — the same matcher, and the same
# reason, as the subject rules. The first version of this table substring-
# matched and the resulting "40% missed" was not a measurement of the model:
# "sea" matched *Search for meaning* and *Nature and the seasons*, "war"
# matched *Cowardice* (co-war-dice). The model declining those is correct,
# and it was scored as a failure to read. A trailing '*' means stem.
#
# NEEDLES REMOVED, each because the theme does not assert the trait AS
# DEFINED in traits.py — not because the model disagreed with them:
#   t:war       "conflict"  — *Father-son conflict*, *Moral conflict* are
#                             not wars; the definition says armed conflict.
#   t:child     "innocence" — *American innocence*, *Corruption of
#                             innocence* say nothing about the protagonist's
#                             age, which is what the trait claims.
#   t:survival  "isolation" — *Social isolation*, *Isolation and madness*
#                             are not a hostile environment to survive.
#   t:detective "crime"     — *Crime and punishment*, *Poverty and crime*
#                             assert no investigation.
#   t:funny     "absurd"    — *The absurdity of existence* is Camus, not a
#                             book written to be humorous.
# "warfare" is now listed explicitly: whole-word "war" no longer reaches it.
CALIBRATION_HINTS = {
    "t:sea": ["sea", "ocean", "voyage", "sail*", "maritime", "shipwreck"],
    "t:magic": ["magic*", "sorcer*", "witch*", "supernatural", "enchant*"],
    "t:war": ["war", "warfare", "battle", "military"],
    "t:romance": ["love", "romance", "romantic", "courtship"],
    "t:survival": ["survival", "surviv*", "endurance"],
    "t:family": ["family", "parent*", "sibling*", "marriage"],
    "t:child": ["childhood", "coming of age", "growing up"],
    "t:funny": ["satire", "satirical", "humor", "humour", "comic", "comedy"],
    "t:detective": ["mystery", "detective", "investigation"],
    "t:realevents": ["historical", "biography", "memoir", "true story"],
}

# Compiled once, same idiom as features._RULE_INDEX.
_HINT_INDEX = {key: [_compile(n) for n in needles]
               for key, needles in CALIBRATION_HINTS.items()}


def expected_traits(themes: list[str]) -> set[str]:
    """Which traits our human-reviewed themes assert."""
    normed = [normalize(t) for t in themes]
    return {key for key, pats in _HINT_INDEX.items()
            if any(p.search(t) for p in pats for t in normed)}


# How many whole batches may fail back-to-back before the run gives up.
# On a healthy run failures are isolated; a run of them means the provider
# is gone, not that these particular books are hard. Same idea, and the same
# number, as harvest_descriptions.py.
MAX_CONSECUTIVE_FAILURES = 6

# Ceiling on the backoff below. Past this a wait is no longer riding out a
# per-minute limit, it is waiting for a daily one, and stopping to resume
# later is the honest answer.
MAX_BACKOFF = 120.0


class _LastProviderError(logging.Handler):
    """Remembers the last thing gemini_client complained about.

    generate() logs each provider's real error and then raises a generic
    "all providers exhausted", so by the time this script sees a failure the
    reason is gone. Reading it back off the log is worth the small ugliness:
    it is the difference between "stopped after 6 failures" and "the free
    tier's 500 requests a day are spent", which decides whether the next run
    is in one minute or tomorrow.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        # Three buckets, kept apart on purpose. One call logs THREE lines —
        # the Gemini key's real error, then Groq's, then a generic "all
        # providers failed" — so "most recent" is the least informative of
        # the three. Keeping one field reported "all providers failed or
        # returned empty" while the line two above it named the 500-a-day
        # cap; keeping two still lost it, because Groq's own 429 carries no
        # daily-quota marker and overwrote Gemini's. What is wanted is the
        # most SPECIFIC line, not the last one.
        self.last = ""
        self.quota = ""      # any 429 / RESOURCE_EXHAUSTED
        self.daily = ""      # a 429 that names a per-day quota

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        self.last = text
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            self.quota = text
            # Gemini says "…PerDayPerProjectPerModel"; Groq says "tokens per
            # day (TPD)". Both are the same fact — come back tomorrow — and
            # neither is the per-minute limit, which just needs a slower pace.
            if ("PerDay" in text or "RequestsPerDay" in text
                    or "per day" in text.lower() or "TPD" in text):
                self.daily = text

    def diagnose(self) -> str:
        if self.daily:
            if "TPD" in self.daily or "per day" in self.daily.lower():
                return ("a provider's DAILY token budget is spent — Groq's "
                        "free tier is 100,000 tokens a day per model, and the "
                        "remaining corpus needs roughly 490,000. Resume "
                        "tomorrow, or use --groq-model to switch to a model "
                        "with its own separate daily bucket.")
            also = (" Groq is rate limited too, so there is no fallback left "
                    "today." if "groq" in self.quota.lower() else "")
            return ("the free tier's DAILY request cap is spent. It resets at "
                    "midnight Pacific — resume then, or add keys to "
                    "GEMINI_API_KEYS (each key has its own daily cap)." + also)
        if self.quota:
            return ("rate limited, with no per-day marker in the error. If it "
                    "is the per-minute cap, raise --delay and resume now.")
        return self.last[:200] if self.last else "no provider error was logged."


# Groq bills RESERVED output against its tokens-per-minute ceiling, not the
# tokens actually generated. The shared client asks for max_tokens=4096,
# which is right for a summary and ruinous here: a batch prompt is ~2,700
# tokens, so each call was charged ~6,800 of a 12,000/min budget and the run
# could not sustain two requests a minute at ANY --delay. That is the real
# reason behind "real prompts stop after four" in the earlier notes — the
# limit was never the prompt size.
#
# A reply here is one short JSON object: eight books, at most sixteen short
# labels each, well under 500 tokens. 800 leaves room and cuts the charge
# per call by roughly half. An over-long reply is truncated, fails to parse
# and is discarded whole by parse_batch_response — the same safe path as any
# other malformed reply, never a partial label set.
TRAIT_MAX_OUTPUT_TOKENS = 800


def _trait_config():
    """DEFAULT_CONFIG's temperature, this task's much smaller output cap.

    Built here rather than by editing gemini_client.DEFAULT_CONFIG, which is
    shared with every runtime tool in the API — /summary and the quiz
    generators genuinely need 4096.
    """
    from google.genai import types as genai_types

    from gemini_client import DEFAULT_CONFIG
    return genai_types.GenerateContentConfig(
        temperature=DEFAULT_CONFIG.temperature,
        max_output_tokens=TRAIT_MAX_OUTPUT_TOKENS,
    )


# A batch is bounded by CHARACTERS, not by a count of books.
#
# MEASURED 2026-08-21, and it is why --source fandom labelled nothing at
# all. The corpus path feeds short Open Library blurbs, so eight of them is
# a small prompt and a fixed count was fine. The Fandom path feeds harvested
# wiki prose capped at 4,000 characters each, and it processes LONGEST
# FIRST — so the very first batch is the eight biggest articles: 35,213
# characters, about 9,000 tokens, and Groq answers **HTTP 413 Payload Too
# Large**. Every batch failed, the run reported "0 books labelled", and the
# error surfaced as "Groq returned nothing (key set? quota left?)" — which
# reads as a quota problem and is not one.
#
# 12,000 characters is ~3,000 prompt tokens. 24,000 was tried first: it
# cleared the 413, and then hit the OTHER wall — Groq's free tier allows
# **12,000 tokens a minute**, so two 6,000-token calls exhaust it and every
# batch after the first came back empty. Half the size means four calls a
# minute fit the same budget, and the caller still has to pace itself
# (`--delay 20` for this source). `--batch` still caps the count; this caps
# the size, and a single book over the budget goes on its own rather than
# being dropped.
CHUNK_CHAR_BUDGET = 12000


def _pack_chunk(todo: list, start: int, max_books: int,
                descriptions: dict) -> list:
    """As many books as fit in one prompt, at least one."""
    chunk, total = [], 0
    for doc in todo[start:start + max_books]:
        size = len(descriptions.get(doc["key"]) or "")
        if chunk and total + size > CHUNK_CHAR_BUDGET:
            break
        chunk.append(doc)
        total += size
    return chunk


def _generate(prompt: str, provider: str = "auto") -> str:
    """One completion. `provider` picks who answers.

    "auto" is the shared client's normal chain: every Gemini key, then Groq.
    Correct in production, wasteful here — once Gemini's daily quota is
    spent, every single call burns a doomed Gemini attempt (and its
    backoff) before reaching the provider that can actually answer. Over
    5,000 books that is hours of failing on purpose.

    "groq" goes straight there. Gemini's free tier allows 15 requests a
    minute per key, which is 5.6 hours for the corpus on one key and is
    what pushed this to a switch rather than a preference.
    """
    config = _trait_config()
    if provider == "groq":
        text = _groq_call(prompt, config)
        if not text:
            raise RuntimeError("Groq returned nothing (key set? quota left?)")
        return text
    from gemini_client import generate
    return generate(prompt, config)


# Set by main() from --groq-model. Each Groq model has its OWN daily token
# bucket, so exhausting llama-3.3-70b (100,000 a day) does not touch the
# others — which is the only lever left once a day's budget is gone.
GROQ_MODEL_OVERRIDE: str | None = None


def _groq_call(prompt: str, config) -> str | None:
    """Groq, optionally against a model other than the shared default.

    gemini_client._groq_generate is hardcoded to GROQ_MODEL because the API's
    runtime fallback should not be reconfigurable per call. This is a build-
    time script with a different constraint — it needs whichever model still
    has daily budget — so it posts directly rather than widening the shared
    client's contract for a caller that is not in the request path.
    """
    import httpx

    from gemini_client import (GROQ_API_KEY, GROQ_CHAT_URL, GROQ_MODEL,
                               _groq_generate)
    if not GROQ_MODEL_OVERRIDE:
        return _groq_generate(prompt, config)
    if not GROQ_API_KEY:
        return None

    # `reasoning_effort: low`, and without it this whole path returns
    # nothing at batch size. MEASURED 2026-08-21: gpt-oss-120b is a
    # REASONING model, and `TRAIT_MAX_OUTPUT_TOKENS = 800` was sized for
    # gemini-3.1-flash-lite, which does not think before it answers. Asked
    # for eight books it spent **798 of its 800 output tokens on reasoning**
    # and returned `finish_reason: length` with an EMPTY string — so every
    # batch failed and the run reported "0 books labelled" while looking
    # like a quota problem.
    #
    # Raising the cap is not the fix and was tried: prompt is already 5,379
    # tokens and max_tokens=4000 answers **HTTP 413**. Lowering the effort
    # is: same batch of eight, reasoning drops to 459, `finish_reason:
    # stop`, 404 characters of correct JSON.
    #
    # Sent only on the override path, which is build-time scripts choosing
    # their own model. A model that rejects the parameter gets one retry
    # without it rather than a silent None — an unknown-parameter 400 must
    # not look like an empty answer, which is the exact confusion this
    # comment exists to end.
    body = {
        "model": GROQ_MODEL_OVERRIDE or GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
        "reasoning_effort": "low",
    }
    for attempt in (0, 1):
        try:
            resp = httpx.post(
                GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=body,
                timeout=90.0,
            )
            if resp.status_code == 400 and attempt == 0 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            if not text and choice.get("finish_reason") == "length":
                # Say which wall was hit. "Groq returned nothing" sent this
                # session hunting a quota that was never the problem.
                logging.getLogger("gemini_client").warning(
                    "Groq hit max_tokens before writing any content "
                    "(reasoning consumed the budget) — lower the batch size "
                    "or raise TRAIT_MAX_OUTPUT_TOKENS")
            return text or None
        except Exception as e:  # noqa: BLE001 — same fail-open shape as the client
            logging.getLogger("gemini_client").warning(f"Groq fallback failed: {e}")
            return None
    return None


def extract_one(title: str, author: str, text: str,
                provider: str = "auto",
                vocab: dict[str, tuple[str, str]] | None = None
                ) -> list[str] | None:
    """Labels for one book, or None when the call failed.

    None and [] are different and both are kept: a failed call should be
    retried on the next run, while a genuine empty answer should not.
    `vocab` — see `traits.build_prompt`; defaults to the full `TRAITS`,
    which is what `--calibrate` always wants (a global accuracy check, not
    a partial one) and what every caller got before `--keys` existed.
    """
    try:
        labels = parse_response(
            _generate(build_prompt(title, author, text, vocab), provider), vocab)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! {title[:40]}: {str(exc)[:70]}", file=sys.stderr)
        return None
    # HERE TOO, not only in extract_batch. The strip started life on the
    # batch path alone, which left the guarantee with a hole in exactly the
    # two places a short book is most likely to arrive by: --calibrate, which
    # never batches, and extract_batch's own retry-singly fallback, which is
    # what runs when a reply failed to align. A negative ships as `absent`
    # and argues against the correct book; the gate has to hold on every
    # path that can produce one.
    return _strip_ungrounded_negatives([labels], [(title, "", text)])[0]


def extract_batch(rows: list[tuple[str, str, str]], provider: str,
                  vocab: dict[str, tuple[str, str]] | None = None
                  ) -> list[list[str] | None]:
    """Label several books in one call, falling back to one at a time.

    A batch reply that does not align exactly — wrong count, duplicate or
    out-of-range index, malformed labels — is discarded WHOLE rather than
    salvaged. Partially-aligned labels would be attached to the wrong
    books, silently, which is worse than any number of failed calls.

    THE FALLBACK IS FOR MISALIGNMENT, NOT FOR FAILURE. A model that replied
    with seven entries for eight books may well answer correctly one book at
    a time. A provider that raised — quota, timeout, outage — will raise
    eight more times, and the run that hit the daily cap spent nine calls
    per batch discovering that, over and over. So a raised call returns
    None for the whole batch and lets the caller decide whether to go on.

    `vocab` — see `extract_one`.
    """
    try:
        raw = _generate(build_batch_prompt(rows, vocab), provider)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! batch failed ({str(exc)[:70]})", file=sys.stderr)
        return [None] * len(rows)

    parsed = parse_batch_response(raw, len(rows), vocab)
    if parsed is not None:
        return _strip_ungrounded_negatives(parsed, rows)
    print(f"    . batch of {len(rows)} did not align; retrying singly",
          file=sys.stderr)
    return [extract_one(t, a, x, provider, vocab) for t, a, x in rows]


def _strip_ungrounded_negatives(parsed: list[list[str] | None],
                                rows: list[tuple[str, str, str]]
                                ) -> list[list[str] | None]:
    """Drop "no" answers for books too short to support one.

    THE PROMPT ASKS AND THIS ENFORCES, and the split is the point. The batch
    prompt marks each short book TOO SHORT and rule 7 tells the model to
    return an empty "no" list for it — but a rule in a prompt is a request.
    A negative is an assertion that goes into the shipped matrix as
    `absent`, where it moves belief against the book; letting one through
    for a description nobody could judge would be exactly the confident
    wrong answer `NEGATIVE_MIN_WORDS` exists to prevent.

    Positives are untouched: they were always allowed at any length, and
    this changes nothing about how a short blurb is labelled today.
    """
    out = []
    for labels, (_t, _a, text) in zip(parsed, rows):
        if labels is None or allows_negatives(text):
            out.append(labels)
            continue
        kept = [l for l in labels if not l.startswith(NEGATIVE_PREFIX)]
        if len(kept) != len(labels):
            print(f"    . dropped {len(labels) - len(kept)} negative(s) for a "
                  f"{len(text.split())}-word description", file=sys.stderr)
        out.append(kept)
    return out


def _score(rows: list[dict]) -> None:
    """Print the comparison. `rows` is [{title, themes, labels}, ...].

    NEGATIVES ARE SCORED SEPARATELY AND THEY ARE THE POINT OF THIS RUN NOW.
    A theme our pages assert that the model merely FAILED TO FIND is
    recorded `unknown` — a gap, the same gap this file has always measured.
    A theme our pages assert that the model actively DENIED is recorded
    `absent`, which the engine scores at 0.15-0.45 and which therefore
    pushes the correct book DOWN when the player answers yes.
    One is a hole; the other is a wrong claim, and only the second can lose
    a game. They must never be added together.
    """
    hit = miss = extra = denied = 0
    misses: list[tuple[str, str]] = []
    denials: list[tuple[str, str]] = []
    for r in rows:
        yes, no = split_labels(r["labels"])
        expected = expected_traits(r["themes"])
        hit += len(expected & yes)
        # A theme the model denied is NOT counted as a miss as well — it is
        # its own, worse category, and double-counting it would hide the
        # improvement in one number behind a regression in the other.
        denied += len(expected & no)
        miss += len(expected - yes - no)
        extra += len(yes - expected)
        for key in sorted(expected - yes - no):
            misses.append((key, r["title"]))
        for key in sorted(expected & no):
            denials.append((key, r["title"]))

    neg_total = sum(len(split_labels(r["labels"])[1]) for r in rows)
    print()
    print("NEGATIVES")
    print(f"  'no' answers given                          : {neg_total}")
    print(f"  ...that CONTRADICT a theme our pages assert : {denied} "
          f"({denied * 100 // max(1, neg_total)}%)")
    if denials:
        print("  worst offenders:")
        for key, title in denials[:10]:
            print(f"    {key:<16} denied for {title[:44]}")
    print()
    print("  This is the number that gates the harvest. A denial ships as")
    print("  `absent` and scores 0.15-0.45, so it argues AGAINST the correct")
    print("  book when the player answers yes. A miss only ships as")
    print("  `unknown`, which moves nothing. Tune NEGATIVE_MIN_WORDS or the")
    print("  rules in traits._RULES against this figure, not against a")
    print("  handful of books read by eye.")

    total = hit + miss
    print()
    print("CALIBRATION")
    print(f"  themes our pages assert AND the model found : {hit}")
    print(f"  themes our pages assert and the model MISSED: {miss} "
          f"({miss * 100 // max(1, total)}%)")
    print(f"  labels only the model gave                  : {extra}")
    print()
    print("  The miss rate is the number that matters — those are human")
    print("  judgments the model failed to read. Model-only labels are")
    print("  often correct: our themes list four per book and are sparse")
    print("  by design, so 'the sea' can be true and simply unlisted.")
    if misses:
        by_trait: dict[str, list[str]] = {}
        for key, title in misses:
            by_trait.setdefault(key, []).append(title)
        print("\n  What was missed, so the number can be argued with:")
        for key, titles in sorted(by_trait.items(), key=lambda x: -len(x[1])):
            shown = ", ".join(t[:34] for t in titles[:4])
            more = f" +{len(titles) - 4}" if len(titles) > 4 else ""
            print(f"    {key:<14} {len(titles):>3}  {shown}{more}")


def calibrate(delay: float, provider: str = "auto",
              save_path: str = CALIBRATION_PATH) -> None:
    """Measure the model against themes a human already reviewed.

    The model's answers are SAVED. Scoring them is pure judgment — which
    theme phrase asserts which trait — and that judgment was wrong the first
    time round (see CALIBRATION_HINTS). Re-scoring a saved run costs
    nothing; re-running the model to change one's mind about a keyword costs
    129 calls of a 15-a-minute quota, which is how a measurement ends up
    unchallenged.
    """
    pages = [p for p in load_site_books() if p.get("themes") and p.get("prose")]
    print(f"Calibrating on {len(pages)} published pages with themes.\n")

    rows: list[dict] = []
    failed = 0
    for i, p in enumerate(pages, 1):
        labels = extract_one(p["title"], p["author"], p["prose"][:4000], provider)
        if labels is None:
            failed += 1          # a failed call is not an empty answer
            continue
        rows.append({"title": p["title"], "themes": p["themes"],
                     "labels": labels})
        if i % 10 == 0 or i == len(pages):
            print(f"  {i:>4}/{len(pages)}   {len(rows)} scored, {failed} failed")
        time.sleep(delay)

    with open(save_path, "w", encoding="utf-8") as fh:
        json.dump({"pages": rows}, fh, ensure_ascii=False, indent=1)
    print(f"\nsaved {len(rows)} model answers -> {save_path}")
    if failed:
        print(f"  {failed} call(s) failed and are excluded, not counted as "
              f"empty answers")
    _score(rows)


def rescore(path: str = CALIBRATION_PATH) -> None:
    """Re-score a saved calibration run against the current hints. No calls."""
    if not os.path.exists(path):
        print(f"No saved calibration at {path} — run --calibrate first.")
        return
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)["pages"]
    print(f"Re-scoring {len(rows)} saved model answers. No API calls.")
    _score(rows)


def _payload(out: dict, tested: dict, tracking_enabled: bool) -> dict:
    """What gets written to --out. `tested` is included only once tracking
    has been switched on for this file (see `tracking_enabled` in main()) —
    writing it earlier would be exactly the half-migrated state that flag
    exists to prevent: a `tested` map covering some books but not others,
    which the next run cannot tell apart from "these others were never
    tested for anything"."""
    payload = {"traits": out}
    if tracking_enabled:
        payload["tested"] = tested
    return payload


def migrate_tested(path: str) -> None:
    """One-time upgrade to per-key resume tracking. See --migrate-tested's
    own help text for the exact assumption this makes and why it is only
    correct right now, not in general.

    THE ONLY WAY a file gains a `tested` map, by design. An ordinary run
    never introduces one on its own — see `tracking_enabled` in main() —
    specifically so that the first `tested` a file ever gets is always
    COMPLETE (every book in `traits` stamped at once), never partial.

    Refuses to double-migrate: a file that already has a `tested` map has
    either been through this once or has been maintaining it incrementally
    ever since, and re-running this over already-accurate per-key data
    would blow that accuracy away — exactly the mistake this flag exists to
    prevent happening implicitly.
    """
    if not os.path.exists(path):
        print(f"No {path} yet — nothing to migrate.")
        return
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    out = saved.get("traits", saved)
    if "tested" in saved:
        print("Already migrated — 'tested' is already present. Nothing done.")
        return
    tested = {key: sorted(TRAITS) for key in out}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"traits": out, "tested": tested}, fh, ensure_ascii=False)
    print(f"Migrated {len(out)} book(s): each stamped as tested against all "
          f"{len(TRAITS)} current TRAITS keys. No API calls made.")
    print("From here on, only use --keys for a key added AFTER this moment "
          "— do not run --migrate-tested again.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score the last --calibrate run against the "
                         "current CALIBRATION_HINTS, without any API calls.")
    ap.add_argument("--limit", type=int, default=5000)
    # 4.2s, not 0.5. The free tier allows FIFTEEN requests per minute per
    # key for gemini-3.1-flash-lite, and the first calibration run at 0.25s
    # (240/min) had most of its calls rejected with 429 — it produced a
    # number that looked like a measurement and was mostly failures. A
    # default that silently exceeds the quota is worse than a slow one.
    #
    # Cost at this rate, single key: 9 minutes for the 129-page calibration,
    # about 5.6 hours for the full 5,000. Resumable, so it can be run in
    # pieces, and it is popularity-ordered so a partial run covers the books
    # players actually think of.
    ap.add_argument("--delay", type=float, default=4.2,
                    help="seconds between calls; 4.2 keeps one key inside "
                         "the 15/min free-tier quota. Lower it only if "
                         "GEMINI_API_KEYS holds several keys.")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE,
                    help="books per call. The trait vocabulary is 575 tokens "
                         "against 201 for a book, so sending one at a time "
                         "pays the vocabulary 3,590 times over — 2.79M "
                         "tokens for the corpus against 1.13M at five. Set 1 "
                         "to disable.")
    ap.add_argument("--provider", choices=["auto", "groq"], default="auto",
                    help="'groq' skips the Gemini attempts entirely. Use it "
                         "once Gemini's daily quota is spent — otherwise "
                         "every call pays for a doomed Gemini try first.")
    ap.add_argument("--groq-model", default=None,
                    help="Groq model to use with --provider groq. Each model "
                         "has its own daily token bucket, so this is the way "
                         "to keep going after one is spent. A different model "
                         "is a different labeller: run --calibrate on it "
                         "before trusting a corpus labelled with it.")
    ap.add_argument("--source", choices=["corpus", "fandom"], default="corpus",
                    help="'fandom' labels data/akinator_fandom_text.json — "
                         "the verified wiki prose harvested for books Open "
                         "Library has no description for — and writes to "
                         "akinator_fandom_traits.json instead.")
    ap.add_argument("--from-shipped", default=None,
                    help="with --source corpus: a books.json URL or local "
                         "path — read the book list from the SHIPPED "
                         "artifact instead of the local, gitignored corpus, "
                         "so a GitHub Actions runner with no corpus can "
                         "still run this incrementally. See shipped_docs.py.")
    ap.add_argument("--keys", default=None,
                    help="comma-separated TRAITS ids to backfill (e.g. "
                         "t:cooking) instead of the full vocabulary. Sends a "
                         "shorter prompt and skips any book already tested "
                         "against every requested key. Needs the output "
                         "file to already carry per-key 'tested' data — run "
                         "--migrate-tested once first if it doesn't; the "
                         "tool refuses rather than guessing.")
    ap.add_argument("--relabel", action="store_true",
                    help="re-judge books that already have labels, replacing "
                         "their answers for the requested keys. Needed to add "
                         "negatives to a corpus labelled before the extractor "
                         "could say 'no' — without it every book counts as "
                         "done and the run harvests nothing.")
    ap.add_argument("--ignore-locked", action="store_true",
                    help="also overwrite trait cells the owner set by hand "
                         "(overrides_locked.json). Off by default: a hand "
                         "judgement should not become a model guess.")
    ap.add_argument("--migrate-tested", action="store_true",
                    help="ONE-TIME: stamp every already-labelled book in "
                         "--out as tested against the full CURRENT TRAITS "
                         "vocabulary, enabling --keys. Makes no API calls. "
                         "Correct ONLY if run before adding any new TRAITS "
                         "key after upgrading to this version — it cannot "
                         "see history, so it assumes every label already on "
                         "disk came from the unfiltered vocabulary active "
                         "right now, which has always been true until this "
                         "flag exists. Do not re-run it after adding a new "
                         "key; that would falsely mark old books as already "
                         "tested against a key they never saw.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.out is None:
        args.out = FANDOM_OUT_PATH if args.source == "fandom" else OUT_PATH

    global GROQ_MODEL_OVERRIDE
    GROQ_MODEL_OVERRIDE = args.groq_model
    if args.groq_model:
        print(f"Groq model: {args.groq_model}")

    if args.rescore:
        rescore()
        return

    if args.calibrate:
        calibrate(args.delay, args.provider)
        return

    if args.migrate_tested:
        migrate_tested(args.out)
        return

    # `requested` is the vocabulary THIS run judges against — the full
    # TRAITS unless --keys narrows it. Resolved before any file I/O so the
    # old-format-file check below has it to compare against.
    if args.keys:
        wanted = [k.strip() for k in args.keys.split(",") if k.strip()]
        unknown = [k for k in wanted if k not in TRAITS]
        if unknown:
            print(f"! --keys names id(s) not in TRAITS: {unknown}. Add them "
                  f"to traits.py first.", file=sys.stderr)
            sys.exit(1)
        requested = {k: TRAITS[k] for k in wanted}
    else:
        requested = TRAITS
    requested_keys = set(requested)

    if args.source == "fandom":
        # Shaped into the same (docs, descriptions) pair the corpus path
        # produces, so everything downstream -- batching, the provider
        # fallback, resume, the failure stop -- runs unchanged. The key is
        # the title because that is the only identifier these books have;
        # see FANDOM_OUT_PATH on why they are kept in their own file.
        if not os.path.exists(FANDOM_TEXT_PATH):
            print("No Fandom text yet — run harvest_fandom_text.py first.")
            return
        with open(FANDOM_TEXT_PATH, encoding="utf-8") as fh:
            harvested = json.load(fh)
        descriptions = {t: r["text"] for t, r in harvested.items()
                        if r.get("text")}
        # No popularity number exists for these -- Open Library has no row
        # for most of them, which is the whole reason this source exists.
        # Longest verified prose first: it is the only ordering available
        # that correlates with how much there is to label.
        docs = sorted(({"key": t, "title": t, "author_name": [""]}
                       for t in descriptions),
                      key=lambda d: -len(descriptions[d["key"]]))
        docs = docs[:args.limit]
        print(f"Fandom source: {len(descriptions)} book(s) with verified "
              f"prose\n")
    else:
        if not os.path.exists(DESC_PATH):
            print("No descriptions yet — run harvest_descriptions.py first.")
            return
        with open(DESC_PATH, encoding="utf-8") as fh:
            descriptions = json.load(fh)

        if args.from_shipped:
            from shipped_docs import load_shipped_docs
            docs = load_shipped_docs(args.from_shipped)
        else:
            docs = []
            with open(CORPUS_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            docs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            docs.sort(key=lambda d: -(d.get("readinglog_count") or 0))
        docs = docs[:args.limit]

    # `tested` — which TRAITS keys each book has actually been judged
    # against, separate from `out` (which keys it was found to HAVE). A
    # book present in `out` used to mean "done, skip forever" regardless of
    # which keys produced that entry — so adding one new key to TRAITS and
    # re-running never backfilled it for any already-labelled book, only
    # for books labelled from that point on. `tested` is what makes "done"
    # a per-key question again.
    #
    # `tracking_enabled` — NOT `bool(tested)` — decides which predicate to
    # use, and the distinction is load-bearing. `tested` starts empty on a
    # freshly-upgraded file and would otherwise fill in gradually, one newly
    # -processed book at a time, on ordinary unfiltered runs — which makes
    # it TRUTHY long before it is COMPLETE. The first run after upgrading
    # would look identical to today (few new books, `tested` still empty,
    # old predicate). The SECOND run would see a non-empty-but-partial
    # `tested`, switch to the strict per-key predicate, and find every one
    # of the thousands of pre-existing books "not yet tested" for anything
    # — because they were labelled before this dict existed — and resend
    # every one of them. `tracking_enabled` is keyed on whether `"tested"`
    # was ever written by `--migrate-tested`, which stamps EVERY book in
    # `out` in one atomic pass specifically so this half-migrated state can
    # never occur: either the file has no tracking at all (old predicate,
    # exactly as before this feature existed) or it has COMPLETE tracking
    # (new predicate, safe by construction). Ordinary runs only ever ADD to
    # an already-complete `tested`, in lockstep with `out` — see the merge
    # loop and the write-back below, both gated on this same flag.
    out: dict[str, list[str]] = {}
    tested: dict[str, list[str]] = {}
    tracking_enabled = False
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        out = saved.get("traits", saved)
        tracking_enabled = "tested" in saved
        if tracking_enabled:
            tested = saved.get("tested") or {}
        elif args.keys:
            # Old-format file, predating this feature, and --keys was given.
            # This script cannot safely guess which historical keys a book
            # was actually tested against, so it refuses rather than risk
            # silently under-covering one.
            print("! This file predates per-key tracking and --keys "
                  "was given. Run --migrate-tested once first (no API "
                  "calls), then re-run with --keys.", file=sys.stderr)
            sys.exit(1)
        print(f"Resuming with {len(out)} books already labelled.")

    locked = load_locked() if not args.ignore_locked else {}
    skipped_locked = 0
    if locked:
        print(f"{len(locked)} book(s) have hand-set trait cells; "
              f"{sum(len(v) for v in locked.values())} cell(s) will be left "
              f"exactly as they are.")

    if args.relabel:
        # EVERY book with a description, whatever the file already says.
        # The ordinary predicates below both mean "has this book been
        # judged against these keys", and after --migrate-tested the answer
        # is yes for all 4,157 — so a run meant to ADD negatives to books
        # already labelled would find nothing to do and report a clean
        # finish. That is not a resume bug; the run is asking a different
        # question of the same books, and only this flag can say so.
        todo = [d for d in docs if d.get("key") in descriptions]
        print("--relabel: re-judging books that already have labels. "
              "Answers about the requested keys are REPLACED, not merged.")
    elif tracking_enabled:
        todo = [d for d in docs
                if d.get("key") in descriptions
                and not (requested_keys <= set(tested.get(d["key"], [])))]
    else:
        # No per-key tracking on this file yet (never migrated). Same
        # predicate this script has always used — --keys was already
        # refused above if this branch is reached with one.
        todo = [d for d in docs
                if d.get("key") in descriptions and d["key"] not in out]
    if args.keys:
        print(f"Backfilling {sorted(requested_keys)} only.")
    print(f"{len(todo)} books with a description and no labels yet.\n")

    # Watch the shared client's own log for the reason behind a failure.
    watcher = _LastProviderError()
    logging.getLogger("gemini_client").addHandler(watcher)
    logging.getLogger().addHandler(watcher)

    consecutive = 0
    stopped_early = False
    start = 0
    while start < len(todo):
        chunk = _pack_chunk(todo, start, args.batch, descriptions)
        rows = [(d.get("title") or "", (d.get("author_name") or [""])[0],
                 descriptions[d["key"]]) for d in chunk]
        results = (extract_batch(rows, args.provider, requested)
                   if args.batch > 1 else
                   [extract_one(*r, args.provider, requested) for r in rows])

        for doc, labels in zip(chunk, results):
            if labels is None:
                continue      # failed: leave it for the next run
            key = doc["key"]
            # WHAT THIS RUN IS ALLOWED TO CHANGE: the requested vocabulary,
            # minus anything the owner has decided by hand for this book.
            judged = requested_keys - locked.get(key, set())
            if locked.get(key):
                skipped_locked += len(requested_keys & locked[key])
            # Everything the file already knew about a key OUTSIDE that set
            # survives untouched. A book re-visited for a newly-added key
            # sends back labels for ONLY the requested subset, and
            # overwriting `out[key]` wholesale would erase every label it
            # had from an earlier, differently-scoped run.
            prev = {l for l in out.get(key, []) if _trait_of(l) not in judged}
            # REPLACE within `judged` rather than union, which a plain union
            # could not do once negatives existed: a book previously labelled
            # `t:war` and now answered `-t:war` would end up carrying BOTH,
            # and split_labels would hand apply_labels a key that is
            # simultaneously asserted and denied. Union was correct while
            # every label was a positive; it stopped being correct the moment
            # a label could contradict one.
            new = {l for l in labels if _trait_of(l) in judged}
            out[key] = sorted(prev | new)
            # Only once tracking is switched on for THIS file (see
            # `tracking_enabled` above) — writing a partial `tested` entry
            # for an unmigrated file is exactly the half-migrated state that
            # flag exists to prevent.
            if tracking_enabled:
                tested[key] = sorted(set(tested.get(key, [])) | requested_keys)

        # A batch where nothing came back is a provider problem, not a
        # property of these eight books. The first run to hit the daily cap
        # kept going for 1,500 more books, ~190 batches, every one of them
        # doomed, and reported a clean finish at the end.
        if all(r is None for r in results):
            consecutive += 1
        else:
            consecutive = 0

        # Advance by what was actually PACKED, not by args.batch: a
        # size-bounded chunk is often shorter than the count allows, and
        # striding past the difference would skip books silently.
        start += len(chunk)
        i = start
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(_payload(out, tested, tracking_enabled), fh, ensure_ascii=False)
        labelled = sum(1 for v in out.values() if v)
        print(f"  {i:>5}/{len(todo)}   {len(out)} done, "
              f"{labelled} with at least one label")

        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            print(f"\n! {consecutive} batches in a row returned nothing — "
                  f"stopping at {i}/{len(todo)}.", file=sys.stderr)
            print(f"  Why: {watcher.diagnose()}", file=sys.stderr)
            print(f"  {len(out)} books are saved. Re-run to resume from here; "
                  f"nothing is lost.", file=sys.stderr)
            stopped_early = True
            break

        # Back off after a failure instead of marching straight into the next
        # one. Groq's ceiling is 12,000 tokens a MINUTE and a batch of eight
        # costs ~2,700, so the limit is reached by going slightly too fast,
        # and it clears by waiting. Without this, a brief overrun spends all
        # six of the stop budget in under two minutes and ends a run that had
        # only needed to pause. Gemini's daily cap is unaffected: backing off
        # six times costs ~2 minutes before the same stop.
        pause = args.delay
        if consecutive:
            pause = min(args.delay * (2 ** consecutive), MAX_BACKOFF)
            print(f"    . backing off {pause:.0f}s "
                  f"({consecutive} failed in a row)", file=sys.stderr)
        time.sleep(pause)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(_payload(out, tested, tracking_enabled), fh, ensure_ascii=False)

    counts: dict[str, int] = {}
    neg_counts: dict[str, int] = {}
    for labels in out.values():
        for l in labels:
            if l.startswith(NEGATIVE_PREFIX):
                neg_counts[_trait_of(l)] = neg_counts.get(_trait_of(l), 0) + 1
            else:
                counts[l] = counts.get(l, 0) + 1
    print(f"\n{len(out)} books labelled -> {args.out}")
    total_neg = sum(neg_counts.values())
    if total_neg:
        # The number this whole change exists for: every one of these is a
        # cell moving from `unknown` (0.5, cancels in the normalisation and
        # moves nothing) to `absent` (0.15-0.45, which moves belief).
        print(f"  {total_neg:,} negative(s) recorded — cells that will pack "
              f"as ABSENT instead of unknown")
    if skipped_locked:
        print(f"  {skipped_locked} cell(s) left untouched because the owner "
              f"set them by hand")

    # Say what is still missing, in the same breath as what was done. The
    # count above is the number that looks like success; the one below is
    # the one that decides whether a trait can clear the 5% floor.
    if tracking_enabled:
        remaining = [d for d in docs
                     if d.get("key") in descriptions
                     and not (requested_keys <= set(tested.get(d["key"], [])))]
    else:
        remaining = [d for d in docs
                     if d.get("key") in descriptions and d["key"] not in out]
    if remaining:
        state = "STOPPED EARLY" if stopped_early else "not reached"
        print(f"  {len(remaining)} book(s) with a description are still "
              f"unlabelled ({state}). Re-run to continue.")
    else:
        print("  every book with a description is labelled.")
    no_text = sum(1 for d in docs if d.get("key") not in descriptions)
    print(f"  {no_text} book(s) have no description at all and cannot be "
          f"labelled from prose — they stay `unknown`, never absent.")

    # Percentages are of labelled books, not of the corpus. The floor is a
    # share of all 5,000, so a trait at 8% here is nearer 4% there while
    # coverage is partial — see the frequency column after a rebuild.
    #
    # Scoped to `requested` (the full TRAITS unless --keys narrowed it), not
    # always TRAITS: a --keys run's summary should report on what it just
    # backfilled, not print 0% for every untouched key that this run never
    # asked about.
    n = max(1, len(out))
    print(f"\n  share of the {n} LABELLED books (not of the corpus):")
    for key, (question, _defn) in requested.items():
        c = counts.get(key, 0)
        print(f"  {c * 100 // n:>3}%  {key:<14} {question}")


if __name__ == "__main__":
    main()
