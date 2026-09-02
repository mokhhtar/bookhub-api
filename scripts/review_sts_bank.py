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


# ── the review sheet ───────────────────────────────────────────────────────
# Set as a proof sheet, because that is the job: two passages side by side in
# the same face, at the same size, with the label small. The reviewer is NOT
# being asked which one is real — they are told — so shouting the answer would
# emphasise the wrong thing. What has to be easy is reading both as prose.
_HTML_HEAD = """<title>Spot the Slop — review sheet</title>
<style>
  :root {
    --paper: #FBFBF9; --ink: #191C1F; --muted: #5C6570; --faint: #8A939C;
    --rule: #E2E4E0; --panel: #FFFFFF; --slate: #3B4A5A;
    --real: #2F5D50; --real-bg: #F1F6F3;
    --fake: #8A3A2E; --fake-bg: #FBF2F0;
    --warn: #7A5A1E; --warn-bg: #FBF6EA;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    --sans: ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #14161A; --ink: #E7E9EC; --muted: #9AA3AD; --faint: #6F7883;
      --rule: #262A30; --panel: #1A1D22; --slate: #9DB2C9;
      --real: #7FBFA6; --real-bg: #17231F;
      --fake: #D89184; --fake-bg: #241A18;
      --warn: #D9B366; --warn-bg: #221D12;
    }
  }
  :root[data-theme="dark"] {
    --paper: #14161A; --ink: #E7E9EC; --muted: #9AA3AD; --faint: #6F7883;
    --rule: #262A30; --panel: #1A1D22; --slate: #9DB2C9;
    --real: #7FBFA6; --real-bg: #17231F;
    --fake: #D89184; --fake-bg: #241A18;
    --warn: #D9B366; --warn-bg: #221D12;
  }
  :root[data-theme="light"] {
    --paper: #FBFBF9; --ink: #191C1F; --muted: #5C6570; --faint: #8A939C;
    --rule: #E2E4E0; --panel: #FFFFFF; --slate: #3B4A5A;
    --real: #2F5D50; --real-bg: #F1F6F3;
    --fake: #8A3A2E; --fake-bg: #FBF2F0;
    --warn: #7A5A1E; --warn-bg: #FBF6EA;
  }

  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink);
         font-family: var(--sans); line-height: 1.5; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 40px 22px 90px; }

  .eyebrow { font-size: 12px; letter-spacing: .12em; text-transform: uppercase;
             color: var(--faint); font-weight: 600; }
  h1 { font-family: var(--serif); font-size: clamp(30px, 5vw, 42px); line-height: 1.12;
       margin: 10px 0 0; font-weight: 600; text-wrap: balance; }
  .standfirst { font-size: 16.5px; color: var(--muted); max-width: 64ch; margin: 14px 0 0; }
  .standfirst b { color: var(--ink); }

  .ask { margin: 26px 0 0; padding: 18px 20px; border: 1px solid var(--rule);
         border-left: 3px solid var(--slate); border-radius: 3px; background: var(--panel); }
  .ask p { margin: 0; font-size: 15.5px; color: var(--muted); max-width: 70ch; }
  .ask p + p { margin-top: 9px; }
  .ask b { color: var(--ink); }

  .checks { display: grid; gap: 1px; background: var(--rule); border: 1px solid var(--rule);
            border-radius: 3px; margin-top: 26px;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); overflow: hidden; }
  .check { background: var(--panel); padding: 14px 16px; }
  .check dt { font-size: 11.5px; letter-spacing: .08em; text-transform: uppercase;
              color: var(--faint); font-weight: 600; }
  .check dd { margin: 5px 0 0; font-size: 21px; font-variant-numeric: tabular-nums;
              font-family: var(--serif); }
  .check dd small { font-size: 13px; color: var(--muted); font-family: var(--sans); }
  .check.is-good dd { color: var(--real); }
  .check.is-watch dd { color: var(--warn); }

  .toolbar { position: sticky; top: 0; z-index: 5; margin-top: 30px; padding: 12px 0;
             background: var(--paper); border-bottom: 1px solid var(--rule);
             display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  .tally { font-size: 13.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .tally b { color: var(--ink); }
  button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 3px;
           border: 1px solid var(--rule); background: var(--panel); color: var(--muted);
           cursor: pointer; }
  button:hover { border-color: var(--faint); color: var(--ink); }
  button:focus-visible { outline: 2px solid var(--slate); outline-offset: 2px; }

  .day { margin-top: 44px; }
  .day-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
              padding-bottom: 9px; border-bottom: 1px solid var(--rule); }
  .day-no { font-family: var(--serif); font-size: 25px; }
  .day-date { font-size: 13px; color: var(--faint); font-variant-numeric: tabular-nums;
              letter-spacing: .04em; }

  .pair { margin-top: 26px; padding-top: 22px; border-top: 1px solid var(--rule); }
  .day .pair:first-of-type { border-top: 0; padding-top: 4px; }
  .pair-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
               margin-bottom: 14px; }
  .pair-book { font-family: var(--serif); font-size: 18px; }
  .pair-author { font-size: 14px; color: var(--muted); }
  .pair-n { font-size: 11.5px; color: var(--faint); font-variant-numeric: tabular-nums;
            letter-spacing: .08em; text-transform: uppercase; font-weight: 600; }

  .flag { display: inline-block; font-size: 11.5px; font-weight: 600; letter-spacing: .06em;
          text-transform: uppercase; color: var(--warn); background: var(--warn-bg);
          border: 1px solid var(--warn); border-radius: 2px; padding: 2px 7px; }
  .flag-why { font-size: 13.5px; color: var(--warn); margin: 0 0 14px; max-width: 70ch; }

  .texts { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .text { border: 1px solid var(--rule); border-radius: 3px; padding: 18px 20px 14px;
          background: var(--panel); display: flex; flex-direction: column; gap: 12px; }
  .text.real { border-left: 3px solid var(--real); background: var(--real-bg); }
  .text.fake { border-left: 3px solid var(--fake); background: var(--fake-bg); }
  .text p { font-family: var(--serif); font-size: 17px; line-height: 1.62; margin: 0;
            color: var(--ink); }
  .mark { font-size: 11.5px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
          margin-top: auto; display: flex; gap: 8px; align-items: baseline; }
  .text.real .mark { color: var(--real); }
  .text.fake .mark { color: var(--fake); }
  .mark span { font-weight: 500; letter-spacing: .04em; text-transform: none;
               color: var(--faint); font-variant-numeric: tabular-nums; }

  .verdict { display: flex; gap: 7px; margin-top: 14px; flex-wrap: wrap; align-items: center; }
  .verdict-label { font-size: 12px; color: var(--faint); letter-spacing: .06em;
                   text-transform: uppercase; font-weight: 600; margin-right: 3px; }
  .verdict button[aria-pressed="true"] { background: var(--slate); border-color: var(--slate);
                                         color: var(--paper); font-weight: 600; }

  .rated { display: inline-flex; align-items: baseline; gap: 6px; font-size: 11.5px;
           font-weight: 600; letter-spacing: .04em; padding: 2px 8px; border-radius: 2px;
           border: 1px solid var(--rule); color: var(--muted); }
  .rated i { font-style: normal; font-variant-numeric: tabular-nums; color: var(--faint); }
  .rated.bad { color: var(--fake); border-color: var(--fake); background: var(--fake-bg); }
  .rated.warn { color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }
  .rated.good { color: var(--real); border-color: var(--real); background: var(--real-bg); }

  .rating-note { margin: 0 0 14px; padding: 11px 14px; border-radius: 3px;
                 border: 1px solid var(--rule); background: var(--panel); }
  .rating-note.bad { border-left: 3px solid var(--fake); }
  .rating-note.warn { border-left: 3px solid var(--warn); }
  .rating-note.good { border-left: 3px solid var(--real); }
  .rating-note p { margin: 0; max-width: 74ch; }
  .rating-note .ar { font-size: 16px; line-height: 1.75; color: var(--ink); }
  .rating-note .en { font-size: 13.5px; color: var(--muted); margin-top: 5px; }
  .rating-note .mean { font-size: 12.5px; color: var(--faint); margin-top: 7px;
                       font-style: italic; }

  .caveat { margin-top: 18px; padding: 16px 18px; border: 1px solid var(--warn);
            border-radius: 3px; background: var(--warn-bg); }
  .caveat p { margin: 0; font-size: 14.5px; color: var(--ink); max-width: 72ch; }
  .caveat p + p { margin-top: 8px; }
  .caveat b { color: var(--warn); }
  body.only-needed .pair[data-needs="0"] { display: none; }
  body.only-needed .day:not(:has(.pair[data-needs="1"])) { display: none; }

  .foot { margin-top: 60px; padding-top: 20px; border-top: 1px solid var(--rule);
          font-size: 13.5px; color: var(--faint); max-width: 70ch; }
  @media print { .toolbar, .verdict { display: none; } .day { break-inside: avoid; } }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
"""


# Verdicts from rate_sts_difficulty.py, with what each means for the reader.
VERDICTS = {
    "fake may win": ("المزيف قد يتفوّق", "bad",
                     "The rater chose the fabricated passage. Read this one — but see "
                     "the note on dialect: vernacular authors are penalised by a "
                     "machine reader for the very thing that makes them real."),
    "too obvious": ("واضح جداً", "warn",
                    "Picked correctly every time, with high confidence. Probably a "
                    "boring round."),
    "hard": ("صعب", "good", "Picked correctly some of the time. This is the level to aim at."),
    "clear": ("واضح", "", "Picked correctly every time, but not confidently."),
    "contaminated": ("ملوّث", "warn",
                     "The rater recognised the real passage from memory, so its score "
                     "measures recall, not difficulty. Says nothing either way."),
    "no answer": ("لا جواب", "warn", "The rater returned nothing usable."),
}


def render_html(days: list[dict], stats: dict, data_dir: str,
                ratings: dict | None = None) -> str:
    from html import escape as e

    ratings = ratings or {}

    real_pct = round(stats["real_first"] / max(stats["pairs"], 1) * 100)
    gap_pct = round(stats["widest_length_gap"][0] * 100)
    checks = [
        ("Pairs to read", f"{stats['pairs']}", f"across {stats['days']} days", ""),
        ("Real passage first", f"{real_pct}%",
         f"{stats['real_first']} of {stats['pairs']}",
         "is-good" if 40 <= real_pct <= 60 else "is-watch"),
        ("Mis-decoded text", f"{stats['mojibake']}", "should be zero",
         "is-good" if not stats["mojibake"] else "is-watch"),
        ("Widest length gap", f"{gap_pct}%", "limit is 30",
         "is-good" if gap_pct <= 30 else "is-watch"),
        ("Flagged fakes", f"{stats['echo_flagged']}", "read these first",
         "is-watch" if stats["echo_flagged"] else "is-good"),
        ("Authors", f"{len(stats['authors'])}", "never twice in a day", ""),
    ]
    head = "".join(
        f'<div class="check {cls}"><dt>{e(label)}</dt>'
        f'<dd>{e(value)} <small>{e(note)}</small></dd></div>'
        for label, value, note, cls in checks)

    # THE CAVEAT IS NOT BOILERPLATE. It names the one failure this rater is
    # known to have, measured on the first three pairs it ever saw: it rejected
    # a real Huckleberry Finn passage as "riddled with grammatical errors" —
    # which is Twain's vernacular, the very thing that makes it real. A reader
    # who trusts the badge without this would regenerate good rounds.
    needed = sum(1 for r in ratings.values()
                 if VERDICTS.get(r["verdict"], ("", "", ""))[1] in ("bad", "warn"))
    caveat = filter_btn = ""
    if ratings:
        caveat = f"""<div class="caveat">
    <p><b>The rating is a reading order, not a verdict.</b> A model judged each
      pair blind and its accuracy is used as a difficulty proxy — nothing here
      decides whether a pair ships. {needed} of {len(ratings)} pairs are worth
      your eyes; the rest scored in the range the game is aiming at.</p>
    <p><b>Its known failure: dialect.</b> It rejected a real Mark Twain passage
      as &ldquo;riddled with grammatical errors&rdquo; — which is exactly Twain's
      vernacular voice. Expect &ldquo;fake may win&rdquo; on Twain, and on any author
      who writes in dialect, to be the machine's mistake rather than a bad pair.</p>
    <p>Pairs marked <b>contaminated</b> mean the rater recognised the real
      passage from memory. Every book here is public domain and therefore in its
      training data, so that score measures recall, not difficulty, and says
      nothing either way.</p>
  </div>"""
        filter_btn = ('<button id="filter" aria-pressed="false">'
                      f'Show only what needs reading ({needed})</button>')

    body = []
    for day in days:
        body.append(f'<section class="day"><div class="day-head">'
                    f'<h2 class="day-no">No.&nbsp;{day["n"]}</h2>'
                    f'<span class="day-date">{e(day["date"])}</span></div>')
        for p in day["pairs"]:
            flag = ('<span class="flag">flagged</span>' if p["echo_flagged"] else "")
            why = ('<p class="flag-why">The fake may be this author\'s own published words '
                   'from another book. The inverse check only proves it is not in '
                   '<i>this</i> one — read it before it ships, or a real sentence goes '
                   'out labelled as machine slop.</p>' if p["echo_flagged"] else "")
            cards = []
            for kind in ("real", "fake"):
                mark = (f'Really {e(p["author"])}' if kind == "real" else "Written by an AI")
                cards.append(
                    f'<div class="text {kind}"><p>{e(p[kind])}</p>'
                    f'<div class="mark">{mark}<span>{p[kind + "_chars"]} chars</span></div></div>')
            # Shown real-first always, regardless of which side the player sees:
            # the reviewer is comparing, not guessing, and a consistent column
            # is faster to read down.
            key = f'{day["date"]}-{p["i"]}'
            # A rating is a READING ORDER, never a verdict. It decides whether a
            # pair is worth the owner's time, and nothing else — so it renders
            # as a badge and a sentence, and it never hides a passage.
            rated = ratings.get(key)
            badge, rating_note, needs = "", "", "0"
            if rated:
                label_ar, tone, meaning = VERDICTS.get(
                    rated["verdict"], (rated["verdict"], "", ""))
                score = (f'{rated.get("correct", 0)}/{rated["runs"]}'
                         if rated.get("runs") else "—")
                badge = (f'<span class="rated {tone}" dir="auto">{e(label_ar)}'
                         f'<i>{e(score)}</i></span>')
                needs = "1" if tone in ("bad", "warn") else "0"
                ar = rated.get("why_ar") or ""
                en = rated.get("why") or ""
                named = ", ".join(rated.get("books_named") or [])
                rating_note = (
                    f'<div class="rating-note {tone}">'
                    f'<p class="ar" dir="rtl" lang="ar">{e(ar)}</p>'
                    f'<p class="en">{e(en)}</p>'
                    f'<p class="mean">{e(meaning)}'
                    + (f' Recognised as: {e(named)}.' if named else "")
                    + "</p></div>")
            body.append(
                f'<article class="pair" data-key="{e(key)}" data-needs="{needs}">'
                f'<div class="pair-head"><span class="pair-n">Pair {p["i"] + 1}</span>'
                f'<span class="pair-book">{e(p["title"])}</span>'
                f'<span class="pair-author">{e(p["author"])}</span>{flag}{badge}</div>'
                f'{why}{rating_note}<div class="texts">{"".join(cards)}</div>'
                f'<div class="verdict"><span class="verdict-label">Verdict</span>'
                f'<button data-v="ok">Right level</button>'
                f'<button data-v="obvious">Too obvious</button>'
                f'<button data-v="better">Fake reads better</button>'
                f'<button data-v="cut">Cut it</button></div></article>')
        body.append("</section>")

    total = stats["pairs"]
    return _HTML_HEAD + f"""
<div class="wrap">
  <p class="eyebrow">Litheca &middot; not launched</p>
  <h1>Spot the Slop &mdash; the first bank, before it goes live</h1>
  <p class="standfirst">
    {stats['pairs']} pairs over {stats['days']} days. In each one, the passage on
    the left is really from the book and the one on the right was
    <b>written by a machine imitating that author</b>. Nothing here is published:
    the page is on main carrying <code>noindex</code>, off the games hub and off
    the account dashboard until these have been read.
  </p>

  <div class="ask">
    <p><b>One question per pair: is the fake convincing at the right level?</b>
      Not &ldquo;did it fool me?&rdquo; &mdash; you already know the answer, and that is
      the wrong test.</p>
    <p>The target is a 70&ndash;80% success rate. A game people usually lose says
      AI is indistinguishable from great literature, which is the opposite of
      what the site argues. A fake that is obvious pastiche is a boring round;
      a fake that reads <i>better</i> than the original argues the machine won.</p>
  </div>

  <dl class="checks">{head}</dl>
  {caveat}

  <div class="toolbar">
    <span class="tally" id="tally">No verdicts yet</span>
    {filter_btn}
    <button id="copy">Copy the rejects</button>
    <button id="clear">Clear all verdicts</button>
  </div>

  {''.join(body)}

  <p class="foot">
    Generated from the shipped puzzle files in <code>{e(data_dir)}</code>, so this
    is exactly what would be served. Verdicts are kept in this browser only.
  </p>
</div>
<script>
  var KEY = "sts-review-verdicts";
  var marks = {{}};
  try {{ marks = JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch (e) {{ marks = {{}}; }}

  var pairs = Array.prototype.slice.call(document.querySelectorAll(".pair"));

  function save() {{
    try {{ localStorage.setItem(KEY, JSON.stringify(marks)); }} catch (e) {{}}
  }}
  function paint() {{
    pairs.forEach(function (el) {{
      var v = marks[el.dataset.key];
      el.querySelectorAll(".verdict button").forEach(function (b) {{
        b.setAttribute("aria-pressed", b.dataset.v === v ? "true" : "false");
      }});
    }});
    var done = Object.keys(marks).length;
    var bad = Object.keys(marks).filter(function (k) {{ return marks[k] !== "ok"; }}).length;
    document.getElementById("tally").innerHTML = done
      ? "<b>" + done + "</b> of " + pairs.length + " judged &middot; <b>" + bad + "</b> to fix"
      : "No verdicts yet";
  }}
  pairs.forEach(function (el) {{
    el.querySelectorAll(".verdict button").forEach(function (b) {{
      b.addEventListener("click", function () {{
        var k = el.dataset.key;
        if (marks[k] === b.dataset.v) {{ delete marks[k]; }} else {{ marks[k] = b.dataset.v; }}
        save(); paint();
      }});
    }});
  }});
  document.getElementById("copy").addEventListener("click", function () {{
    var lines = pairs.filter(function (el) {{
      var v = marks[el.dataset.key];
      return v && v !== "ok";
    }}).map(function (el) {{
      var h = el.querySelector(".pair-head");
      return marks[el.dataset.key].toUpperCase() + "  " + el.dataset.key + "  " +
             h.querySelector(".pair-book").textContent;
    }});
    var text = lines.length ? lines.join("\\n") : "nothing rejected";
    navigator.clipboard.writeText(text).then(function () {{
      document.getElementById("copy").textContent = "Copied " + lines.length;
      setTimeout(function () {{
        document.getElementById("copy").textContent = "Copy the rejects";
      }}, 1600);
    }});
  }});
  document.getElementById("clear").addEventListener("click", function () {{
    marks = {{}}; save(); paint();
  }});

  var filter = document.getElementById("filter");
  if (filter) {{
    filter.addEventListener("click", function () {{
      var on = document.body.classList.toggle("only-needed");
      filter.setAttribute("aria-pressed", on ? "true" : "false");
      filter.textContent = on ? "Show all {total}" : "Show only what needs reading ({needed})";
    }});
  }}
  paint();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--data-dir")
    ap.add_argument("--json")
    ap.add_argument("--html")
    # Output of rate_sts_difficulty.py. Optional on purpose: the sheet has to
    # stand on its own, because a rating is a reading order and never a reason
    # not to have the sheet.
    ap.add_argument("--ratings")
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

    ratings = {}
    if args.ratings:
        with open(args.ratings, encoding="utf-8") as f:
            loaded = json.load(f)
        ratings = {f'{r["date"]}-{r["i"]}': r for r in loaded.get("pairs", [])}
        print(f"{len(ratings)} rating(s) from {loaded.get('model', '?')}")

    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(days, stats, data_dir, ratings))
        print(f"{stats['pairs']} pairs across {stats['days']} days -> {args.html}")
        return 0

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
