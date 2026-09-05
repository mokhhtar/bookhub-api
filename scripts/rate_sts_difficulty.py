"""
scripts/rate_sts_difficulty.py — measure how HARD each Spot the Slop pair is.

    python scripts/rate_sts_difficulty.py --out ratings.json
    python scripts/rate_sts_difficulty.py --out r.json --runs 5 --limit 6

Human review before launch asks "is the fake convincing at the right level?".
That is a judgement about English prose, and the owner's English is not strong
enough to make it 70 times. This narrows the job: it sorts the bank so only the
extremes need reading, and it says why in Arabic.

WHAT IT MEASURES, AND WHY IT IS NOT "IS THIS AI?"

Models are bad at detecting machine text and it is the wrong question anyway.
This measures DISCRIMINABILITY: shown the pair blind, how often does a
competent reader pick the real one?

    picks the real one every time   the pair is OBVIOUS      — a boring round
    picks at chance                 the pair is HARD         — or the fake won
    picks the FAKE                  read this one            — the fake may be
                                                               the better prose

That last case is the one that matters most. The plan's whole risk is a fake
that reads better than the original, which argues the opposite of what the site
exists to say.

THE CONTAMINATION PROBLEM, WHICH IS THE REASON THE NAIVE VERSION IS USELESS

Every book in the pool is public domain, which means every one of them is in
the model's training data. Shown a real paragraph of Huckleberry Finn, a model
may identify it BY RECALL rather than by prose quality — and then every pair
looks "obvious" and a perfectly good bank gets regenerated for nothing.

So each pair is asked two things: whether it actually recognises the words as a
specific published book, and, separately, which it picks. A pair the model
recognises has its discrimination score marked CONTAMINATED and excluded from
the calibration read, because that score is measuring memory, not difficulty.

WHY GROQ AND NOT GEMINI. Gemini wrote the fakes. Asking it to judge its own
output is the circularity this project already refuses elsewhere (see
play-bot.js on why its player is not told the answer). An independent model is
the minimum honesty here — and it is still only a proxy, see below.

WHAT THIS IS NOT. It is not the calibration measurement. The real one is the
distribution of player scores the games Worker already records (0-5), and the
70-80% target is an empirical number, not an aesthetic one. A model's
discriminability is not a human's: it may catch statistical tells people miss,
and miss things people catch. Treat the output as a reading order, not a
verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from google.genai import types as genai_types  # noqa: E402

from make_gtb_puzzles import DEFAULT_SITE  # noqa: E402  (loads .env at import)
from make_sts_puzzles import DATA_SUBPATH  # noqa: E402
from review_sts_bank import read_bank  # noqa: E402

# The Groq caller from the trait extractor rather than gemini_client's shared
# one, and not a copy of either. gpt-oss-120b is a REASONING model: the shared
# path sends no `reasoning_effort`, so it can spend its whole output budget
# thinking and return an empty string with finish_reason "length". That was
# measured once already and cost a run that looked like a quota problem. This
# module has the fix and the comment explaining it.
sys.path.insert(0, os.path.join(HERE, "akinator"))
import extract_traits  # noqa: E402

DEFAULT_MODEL = "openai/gpt-oss-120b"
# 1.2s was about 50 requests a minute and Groq's free tier answers far fewer,
# so most of a run was spent generating 429s rather than answers.
PACE_SECONDS = 3.0

# A 429 IS NOT AN ANSWER, AND IT USED TO BE TREATED AS ONE. extract_traits'
# _groq_call catches every exception, logs "Groq fallback failed" and returns
# None -- a deliberate fail-open shape there, where a missing trait is simply
# a trait not learned. Here it silently destroyed a RUN: a pair asked three
# times could come back with one usable answer, and one run is a coin toss.
# Two consecutive batches lost most of their measurements this way.
#
# Not fixed in _groq_call, which the trait pipeline shares and whose fail-open
# behaviour is correct for its own caller. Retried here instead, where waiting
# is free and the alternative is a number nobody should trust.
RETRY_WAITS = (6, 18, 45)


def build_prompt(first: str, second: str, author: str) -> str:
    """The rater is told the author, because THE PLAYER IS TOLD THE AUTHOR.

    The first version of this withheld it, and that was not a neutral choice —
    it was a harder game than the one being shipped, and it scored 36% over the
    whole bank. Worse than chance is not "these pairs are hard": it is a rater
    with a consistent preference pointing the wrong way. The reasons said so
    outright — real Huckleberry Finn rejected for "irregular grammar", real
    Tolstoy beaten by a fake with "clear, balanced" syntax.

    A model asked which passage a human wrote, with no era to anchor on,
    answers which passage is better MODERN prose — and smooth, balanced,
    cliche-free modern prose is exactly what a language model produces. The
    page says "one of these is really Herman Melville" for the same reason this
    now does: without the anchor the question is about competence, and with it
    the question is about voice.
    """
    return f"""Two passages of English prose. One of them is really by {author}.
The other was written by a language model imitating {author}.

Judge them as {author}'s reader would: the question is which passage has that
author's VOICE, in their period and register — not which is better written by
today's standards. Period prose is often long-winded, ornate, comma-spliced or
in dialect, and none of those are faults here. Smooth, balanced, evenly-paced
prose is what the imitation tends to produce.

You are NOT told which is which, and the order means nothing.

PASSAGE A:
{first}

PASSAGE B:
{second}

Answer three separate things.

1. RECOGNITION — do you actually recognise either passage as the real words of
   a specific published book? Answer only if you recognise the ACTUAL WORDS.
   Recognising the style, the period or the subject is NOT recognition; answer
   "none" for that. Naming a book you are not sure about makes this measurement
   worse, so "none" is the right answer whenever you are guessing.

2. PICK — which passage is really by {author}? You must choose A or B even if
   you are unsure. If you answered "none" above, pick on voice alone.

3. WHY — one sentence naming the strongest CONCRETE signal you used: sentence
   rhythm, specific detail versus abstraction, cliche, syntax, register,
   punctuation. Do not write "it feels more authentic" or "it reads better" —
   name the thing on the page.

Return ONLY JSON, no other text:
{{"recognised": "A" or "B" or "none",
  "book": "title if recognised, else null",
  "pick": "A" or "B",
  "confidence": "low" or "medium" or "high",
  "why": "one sentence in English",
  "why_ar": "the same sentence in Arabic"}}"""


def ask(first: str, second: str, author: str, model: str) -> dict | None:
    extract_traits.GROQ_MODEL_OVERRIDE = model
    config = genai_types.GenerateContentConfig(
        # Not zero: several runs per pair only tell us something if the model
        # is allowed to land differently on the ones that are genuinely close.
        temperature=0.6,
        max_output_tokens=1400,
        response_mime_type="application/json",
    )
    prompt = build_prompt(first, second, author)
    # An empty return is retried as readily as a rate-limited one: _groq_call
    # flattens both to None, and neither is a reason to throw a run away.
    for wait in (0,) + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        try:
            raw = extract_traits._groq_call(prompt, config)
        except Exception as e:
            print(f"      call failed: {type(e).__name__}")
            raw = None
        if not raw:
            continue
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)      # fenced or prefaced output
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        # Unparseable is a bad answer rather than no answer, and asking the
        # same question again is the cheapest way to find out which.
    return None


def rate_pair(pair: dict, runs: int, model: str, rng: random.Random) -> dict:
    """One pair, `runs` times, with the sides swapped between runs.

    THE SWAP IS NOT DECORATION. A model with a position bias that always picks
    the first passage would score 100% on every pair whose real side happens to
    be first, and 0% on the rest — which would look like a difficulty signal and
    be nothing of the kind. Alternating the order and recording it makes the
    bias visible instead of letting it masquerade as a measurement.
    """
    results = []
    for i in range(runs):
        real_first = (i % 2 == 0) if runs > 1 else rng.random() < 0.5
        first, second = ((pair["real"], pair["fake"]) if real_first
                         else (pair["fake"], pair["real"]))
        time.sleep(PACE_SECONDS)
        answer = ask(first, second, pair["author"], model)
        if not answer:
            continue
        real_letter = "A" if real_first else "B"
        pick = str(answer.get("pick", "")).strip().upper()[:1]
        rec = str(answer.get("recognised", "none")).strip().upper()[:1]
        results.append({
            "real_letter": real_letter,
            "pick": pick,
            "correct": pick == real_letter,
            # Recognition only counts when aimed at the REAL passage. A model
            # claiming to recognise the fabricated one has recognised nothing —
            # that is a hallucination, and treating it as contamination would
            # throw away a usable pair.
            "recognised_real": rec == real_letter,
            "book": answer.get("book"),
            "confidence": str(answer.get("confidence", "")).lower(),
            "why": (answer.get("why") or "").strip(),
            "why_ar": (answer.get("why_ar") or "").strip(),
            "picked_first": pick == "A",
        })

    n = len(results)
    if not n:
        return {"runs": 0, "verdict": "no answer"}

    correct = sum(r["correct"] for r in results)
    # TWO RUNS IS THE FLOOR, and it is not a nicety. Groq's daily budget ran out
    # mid-bank once and 429s left pairs measured on a SINGLE run — and a single
    # run is a coin toss, which this would then have dressed up as "fake may
    # win" in red next to a passage. Same rule as everywhere else here: no
    # measurement beats a wrong one. Rerun those pairs when the budget resets.
    if n < 2 and runs >= 2:
        return {"runs": n, "correct": correct, "verdict": "not measured",
                "why": "", "why_ar": "",
                "books_named": sorted({r["book"] for r in results if r["book"]}),
                "picked_first": sum(r["picked_first"] for r in results)}

    recognised = sum(r["recognised_real"] for r in results)
    high = sum(r["confidence"] == "high" for r in results)
    rate = correct / n

    if recognised >= max(2, (n + 1) // 2):
        verdict = "contaminated"
    elif rate <= 1 / 3:
        verdict = "fake may win"
    elif rate == 1.0 and high == n:
        verdict = "too obvious"
    elif rate == 1.0:
        verdict = "clear"
    else:
        verdict = "hard"

    # The reason shown to a reader should come from a run that got it RIGHT:
    # the reasoning behind a wrong pick describes the fake, not the tell.
    speaking = next((r for r in results if r["correct"]), results[0])
    return {
        "runs": n,
        "correct": correct,
        "correct_rate": round(rate, 3),
        "recognised_real": recognised,
        "books_named": sorted({r["book"] for r in results if r["book"]}),
        "picked_first": sum(r["picked_first"] for r in results),
        "high_confidence": high,
        "verdict": verdict,
        "why": speaking["why"],
        "why_ar": speaking["why_ar"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--data-dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, help="rate only the first N pairs (a trial)")
    # A bank burns down a day at a time whether or not the game is live, and a
    # day that has passed can never be played. Rating it spends the daily budget
    # on the only pairs whose quality can no longer matter -- and spends it
    # FIRST, since the batch runs in date order.
    ap.add_argument("--from", dest="start", help="skip days before this date")
    args = ap.parse_args()

    from gemini_client import GROQ_API_KEY
    if not GROQ_API_KEY:
        print("GROQ_API_KEY missing — aborting.")
        return 2

    data_dir = (os.path.abspath(args.data_dir) if args.data_dir
                else os.path.join(os.path.abspath(args.site), DATA_SUBPATH))
    days = read_bank(data_dir)
    flat = [(d["date"], d["n"], p) for d in days for p in d["pairs"]]
    if args.start:
        before = len(flat)
        flat = [f for f in flat if f[0] >= args.start]
        skipped = before - len(flat)
        if skipped:
            print("skipping " + str(skipped) + " pair(s) on days before "
                  + args.start + " -- those days have passed and cannot be played\n")
    if args.limit:
        flat = flat[:args.limit]

    print(f"Rating {len(flat)} pair(s) x {args.runs} run(s) with {args.model}\n")
    rng = random.Random(20260902)
    rated = []
    for i, (date, no, p) in enumerate(flat, 1):
        print(f"[{i}/{len(flat)}] {date} pair {p['i'] + 1} — {p['title']}")
        r = rate_pair(p, args.runs, args.model, rng)
        rated.append({"date": date, "n": no, "i": p["i"], "title": p["title"],
                      "author": p["author"], **r})
        print(f"      {r['verdict']}"
              + (f"  ({r.get('correct', 0)}/{r['runs']} correct)" if r["runs"] else "")
              + (f"  recognised as: {', '.join(r['books_named'])}"
                 if r.get("books_named") else ""))

    counts: dict[str, int] = {}
    for r in rated:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    usable = [r for r in rated if r["verdict"] not in
              ("contaminated", "no answer", "not measured")]
    first_picks = sum(r.get("picked_first", 0) for r in rated)
    total_runs = sum(r["runs"] for r in rated)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "runs": args.runs, "data_dir": data_dir,
                   "counts": counts, "pairs": rated}, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 58)
    for v in ("too obvious", "clear", "hard", "fake may win", "contaminated",
              "not measured", "no answer"):
        if counts.get(v):
            print(f"  {v:<14} {counts[v]}")
    if usable:
        mean = sum(r["correct_rate"] for r in usable) / len(usable)
        print(f"\n  discriminability over {len(usable)} uncontaminated pairs: {mean:.0%}")
        print("  (a model's number, not a player's — the real one is the live "
              "score distribution)")
    if total_runs:
        bias = first_picks / total_runs
        print(f"  picked the FIRST passage in {bias:.0%} of runs"
              + ("  ** position bias — treat every score with suspicion **"
                 if not 0.35 <= bias <= 0.65 else "  (no position bias)"))
    short = [r for r in rated if r["verdict"] == "not measured"]
    if short:
        print(f"\n  {len(short)} pair(s) fell below two usable runs — rate-limited, not")
        print("  measured. Rerun once the daily budget resets; do not read the blanks")
        print("  as a pass.")
        for r in short[:12]:
            print(f"    {r['date']} pair {r['i'] + 1} — {r['title']}")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
