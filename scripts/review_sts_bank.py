"""
scripts/review_sts_bank.py — dump a Spot the Slop bank for human review.

    python scripts/review_sts_bank.py --json out.json
    python scripts/review_sts_bank.py            # readable in a terminal

The generator prints a review block as it goes, but truncated to 150
characters a passage — enough to confirm a round was built, nowhere near
enough to answer the only question that matters at review:

    is the fake convincing at the RIGHT level?

Obvious pastiche is a boring round. A fake that reads better than the original
argues the opposite of what the site exists to say. Both judgements need the
whole passage, so this prints the whole passage.

Reads the shipped files rather than any generation-time state, so it can be run
on a bank built weeks ago, and so what is reviewed is exactly what is served.
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

from make_gtb_puzzles import DEFAULT_SITE, decode_reveal  # noqa: E402
from make_sts_puzzles import DATA_SUBPATH, mojibake_hits  # noqa: E402


def read_bank(data_dir: str) -> list[dict]:
    days = []
    for name in sorted(os.listdir(data_dir)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
            continue
        with open(os.path.join(data_dir, name), encoding="utf-8") as f:
            puzzle = json.load(f)
        reveal = decode_reveal(puzzle["reveal_enc"], puzzle["date"])
        pairs = []
        for rnd, ans in zip(puzzle["rounds"], reveal):
            real_i = ans["real_index"]
            real = rnd["passages"][real_i]
            fake = rnd["passages"][1 - real_i]
            pairs.append({
                "i": rnd["i"],
                "author": rnd["author"],
                "title": ans["title"],
                "url": ans["url"],
                "gutenberg": ans.get("gutenberg"),
                "real": real,
                "fake": fake,
                # Which side the PLAYER sees first. A bank where the real one is
                # usually on top would be guessable without reading either.
                "real_index": real_i,
                "echo_flagged": bool(ans.get("echo_flagged")),
                # Length is the one giveaway the generator cannot fully police:
                # it enforces +/-30%, and a pair at the edge of that still reads
                # as lopsided on screen.
                "real_chars": len(real),
                "fake_chars": len(fake),
                "mojibake": mojibake_hits(real) + mojibake_hits(fake),
            })
        days.append({"date": puzzle["date"], "n": puzzle["n"], "pairs": pairs})
    return days


def summarise(days: list[dict]) -> dict:
    pairs = [p for d in days for p in d["pairs"]]
    firsts = sum(1 for p in pairs if p["real_index"] == 0)
    authors: dict[str, int] = {}
    titles: dict[str, int] = {}
    for p in pairs:
        authors[p["author"]] = authors.get(p["author"], 0) + 1
        titles[p["title"]] = titles.get(p["title"], 0) + 1
    return {
        "days": len(days),
        "pairs": len(pairs),
        "real_first": firsts,
        "real_second": len(pairs) - firsts,
        "echo_flagged": sum(1 for p in pairs if p["echo_flagged"]),
        "mojibake": sum(1 for p in pairs if p["mojibake"]),
        "authors": sorted(authors.items(), key=lambda kv: (-kv[1], kv[0])),
        "repeat_titles": sorted(((t, n) for t, n in titles.items() if n > 1),
                                key=lambda kv: (-kv[1], kv[0])),
        "widest_length_gap": max(
            (abs(p["real_chars"] - p["fake_chars"]) / max(p["real_chars"], 1), p["title"])
            for p in pairs) if pairs else (0, None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--data-dir")
    ap.add_argument("--json")
    args = ap.parse_args()

    data_dir = (os.path.abspath(args.data_dir) if args.data_dir
                else os.path.join(os.path.abspath(args.site), DATA_SUBPATH))
    if not os.path.isdir(data_dir):
        print(f"no bank at {data_dir}")
        return 2

    days = read_bank(data_dir)
    if not days:
        print(f"no puzzle files in {data_dir}")
        return 2
    stats = summarise(days)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"),
                       "data_dir": data_dir, "stats": stats, "days": days},
                      f, ensure_ascii=False, indent=1)
        print(f"{stats['pairs']} pairs across {stats['days']} days -> {args.json}")
        return 0

    for day in days:
        print(f"\n=== {day['date']}  #{day['n']} " + "=" * 40)
        for p in day["pairs"]:
            flag = "   [REVIEW: may echo the author's own published words]" if p["echo_flagged"] else ""
            print(f"\n  {p['i']}. {p['title']} — {p['author']}{flag}")
            print(f"     REAL ({p['real_chars']}):\n       {p['real']}")
            print(f"     FAKE ({p['fake_chars']}):\n       {p['fake']}")

    print("\n" + "=" * 60)
    print(f"{stats['pairs']} pairs, {stats['days']} days")
    print(f"real passage shown first in {stats['real_first']}, second in {stats['real_second']}")
    print(f"{stats['echo_flagged']} flagged as possibly the author's own words")
    if stats["mojibake"]:
        print(f"** {stats['mojibake']} pair(s) contain mis-decoded text — should be zero **")
    if stats["repeat_titles"]:
        print("books used more than once: " +
              ", ".join(f"{t} x{n}" for t, n in stats["repeat_titles"]))
    print("\nJudge every pair on ONE question: is the fake convincing at the RIGHT")
    print("level? Obvious pastiche is a boring round. A fake that reads better than")
    print("the original argues the opposite of what this site exists to say.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
