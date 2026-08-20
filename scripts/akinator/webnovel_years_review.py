"""
scripts/akinator/webnovel_years_review.py — the 39 web novels, for a human.

    python scripts/akinator/webnovel_years_review.py

WHY A SPREADSHEET AND NOT ANOTHER EXTRACTOR. Measured this session: of the
33 web novels shipping without a first-publish year, 18 have no harvested
prose at all, 7 more have prose with no year in it, and a keyword rule
recovers 3 of the remaining 8 — correctly, but 3. The ceiling for anything
automatic over the text we hold is 8 of 33, so the rest is a judgement
call, and 33 books is small enough for a person to settle in one sitting.

The year matters because six questions read it (`fact:veryold` ..
`fact:verrecent`) and `features.py` makes a missing one answer `unknown` to
all six. A wrong year is therefore much worse than none: it flips five of
the six into confident wrong answers. That is why this file asks for
confirmation instead of writing anything itself.

WHAT EACH ROW IS. One candidate year found in the wiki prose, with the
sentence it came from and a guess at what that sentence is dating —
serialization, a print edition, a translation, an adaptation, a single
volume. A work with no candidates still gets one row, so nothing is
invisible, and it carries the wiki URL to open.

FILL IN `your_year`. Leave it blank to skip; write `none` when the wiki
genuinely does not say. Then the years can be applied through the admin
page's correction endpoint, which lands them at the next full rebuild.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.abspath(os.path.join(REPO_ROOT, "..", "bookhub",
                                   "games", "data", "akinator"))
TEXT_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom_text.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_webnovel_years_review.csv")

YEAR = re.compile(r"\b(1[89]\d\d|20[0-2]\d)\b")
# Split on sentence enders only. NOT on the comma: every full date in this
# corpus reads "February 14, 2014", so splitting there puts the keyword in
# one fragment and the year in the next, and they never meet again.
SENTENCE = re.compile(r"(?<=[.!?])\s+")

# What a sentence carrying a year seems to be dating. Ordered: the first
# match wins, so the specific patterns come before the vague ones.
KINDS: list[tuple[str, str]] = [
    # A SEQUEL IS A DIFFERENT WORK, and its start date reads exactly like
    # this one's. "Solo Leveling: Ragnarok began serialization in 2023" was
    # being offered as Solo Leveling's own date.
    ("SEQUEL, not this book", r"\bsequel\b|\bspin[- ]?off\b|\bsecond series\b"),
    ("translation", r"translat|english version|localiz|licen[sc]ed"),
    # `animat` as well as `anime`: "animated by Studio Bind began on January
    # 10, 2021" was landing under serialization, because \banime\b does not
    # match "animated".
    ("adaptation", r"\b(anime|animat\w*|manga|manhwa|manhua|webtoon|donghua|drama|"
                   r"film|movie|television|tv series|game|adapted)\b"),
    ("one volume", r"\bvolume\s*\d|\bvol\.?\s*\d|\bchapter\s*\d|\bbook\s*\d"),
    ("serialization start", r"first seriali|began seriali|started seriali|"
                            r"serializ\w*\s+(?:on|from|in)|first posted|"
                            r"originally posted|web[- ]based novel|"
                            # Worm's wiki dates its start by naming the first
                            # chapter — "The story began with Gestation 1.1 on
                            # June 11 2011" — which no publication verb covers.
                            r"(?:story|novel|series|it)\s+(?:began|started)|"
                            r"(?:began|started)\s+(?:with|on|in)\b"),
    ("print edition", r"published (?:in full|by)|print(?:ed)? (?:edition|version)|"
                      r"physical (?:edition|release)|light novel (?:volume|edition)"),
    ("first publication", r"first published|originally published|debuted|first release"),
    ("ended", r"\b(ended|concluded|completed|finished)\b"),
]


# CLASSIFY WHAT SURROUNDS THE YEAR, NOT THE SENTENCE. One sentence routinely
# dates two different events — "The story began with Gestation 1.1 on June 11
# 2011, and ended with Interlude … 2013" — and labelling both years from the
# whole sentence called them both "ended", which is exactly the confusion
# this file exists to remove. The window is asymmetric because English puts
# the verb before the date: "began serialization on … 2012".
BEFORE, AFTER = 90, 25


def classify(sentence: str, at: int) -> tuple[str, str]:
    """(what this year seems to date, the words it was read from)."""
    before = sentence[max(0, at - BEFORE):at]
    after = sentence[at:at + AFTER]
    # Trim only what precedes the year, and only at a clause boundary before
    # it. Trimming the whole window instead read "…June 11 2011, and ended
    # with Inte" and kept the tail, so Worm's START date was labelled from
    # the clause about its END.
    head, sep, tail = before.rpartition(" and ")
    if sep and len(tail) > 8:
        before = tail
    window = (before + after).strip(" ,;")
    for name, pattern in KINDS:
        if re.search(pattern, window.lower()):
            return name, " ".join(window.split())
    return "unclear", " ".join(window.split())


def load_web_novels() -> list[dict]:
    meta = json.load(open(os.path.join(ART, "meta.json"), encoding="utf-8"))
    qs = json.load(open(os.path.join(ART, "questions.json"), encoding="utf-8"))
    books = json.load(open(os.path.join(ART, "books.json"), encoding="utf-8"))
    raw = open(os.path.join(ART, "matrix.bin"), "rb").read()
    nq, bpr = meta["questions"], meta["bytes_per_row"]
    k = [q["id"] for q in qs].index("form:webnovel")
    out = []
    for i, b in enumerate(books):
        state = (raw[i * bpr + (k >> 2)] >> ((k & 3) * 2)) & 3
        if state == 1:
            out.append({**b, "rank": i + 1})
    return out


def main() -> None:
    texts = json.load(open(TEXT_PATH, encoding="utf-8"))
    novels = load_web_novels()
    rows = []
    for b in sorted(novels, key=lambda x: x["rank"]):
        rec = texts.get(b.get("t") or "") or {}
        prose = rec.get("text") or ""
        sub = rec.get("subdomain") or ""
        page = rec.get("page") or ""
        url = f"https://{sub}.fandom.com/wiki/{page.replace(' ', '_')}" if sub and page \
            else (f"https://{sub}.fandom.com" if sub else "")
        base = {
            "work_key": b.get("k"), "title": b.get("t"), "rank": b["rank"],
            "richness_r": b.get("r"), "current_year": b.get("y") or "",
            "has_prose": "yes" if prose else "NO",
            "wiki_page": page, "wiki_url": url,
        }
        seen: set[int] = set()
        found = False
        for sentence in SENTENCE.split(prose):
            for m in YEAR.finditer(sentence):
                year = int(m.group(0))
                if year in seen:
                    continue
                seen.add(year)
                found = True
                kind, context = classify(sentence, m.start())
                rows.append({**base, "year_candidate": year,
                             "seems_to_date": kind, "context": context,
                             "sentence": " ".join(sentence.split())[:220],
                             "your_year": ""})
        if not found:
            rows.append({**base, "year_candidate": "",
                         "seems_to_date": "no year in our text"
                                          if prose else "no prose harvested",
                         "context": "", "sentence": "", "your_year": ""})

    fields = ["work_key", "title", "rank", "richness_r", "current_year",
              "has_prose", "year_candidate", "seems_to_date", "context",
              "your_year", "sentence", "wiki_page", "wiki_url"]
    # utf-8-sig: Excel on Windows reads a plain UTF-8 CSV as the ANSI
    # codepage and mangles every CJK title in this list.
    with open(OUT_PATH, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    works = len({r["title"] for r in rows})
    need = len({r["title"] for r in rows if not r["current_year"]})
    cands = sum(1 for r in rows if r["year_candidate"] != "")
    noprose = len({r["title"] for r in rows if r["has_prose"] == "NO"})
    print(f"{works} web novels, {need} without a year in the shipped game")
    print(f"{cands} candidate year(s) to judge; {noprose} have no prose at all")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
