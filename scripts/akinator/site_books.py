"""
scripts/akinator/site_books.py — our own published pages as a corpus source.

The owner asked whether the free classics already published on the site
could feed the game. They can, and they are the best source available for
that half of the corpus, because unlike everything else here the data is
already fetched, already grounded and already human-reviewed.

MEASURED ON THE 128 PUBLISHED PAGES:

    themes            128/128   (100%)
    free_ebook         79/128   (61%)
    characters         60/128   (46%)
    quotes             47/128   (36%)
    summary prose     median 713 words per book
    already in the shipped 5,000       65
    absent from it entirely            ~44

So the pages do two different jobs at once:

  * **Additions.** Roughly forty published classics never make the top
    5,000 by Open Library reading log, which is exactly the gap the owner
    noticed. They are books we publish, so a player finding one and being
    guessed correctly lands on a page we own.
  * **Enrichment.** For the sixty-five already present, our themes are
    editorial judgments that Open Library's subject strings are not.

THEMES NEED THE SAME TREATMENT AS OL SUBJECTS, and for the same reason.
395 distinct themes across 128 books; only 62 appear on two or more books
and 8 on five or more. Phase 0 found this at 69 books and the shape has not
changed with 128 — a theme unique to one book cannot split anything. But
the shared head is exactly the kind of question the catalogue cannot
supply:

    coming of age 15 · social class 10 · redemption 8 · social injustice 7
    power and corruption 6 · good versus evil 5 · moral ambiguity 5
    survival 5

Those are mapped through a curated vocabulary like every other feature
family here, never used raw.

THE SUMMARY PROSE IS THE OTHER PRIZE and is not used yet. 713 words a book
of grounded description is the natural input for the evocative questions
the owner asked for after playing the real Akinator — "is there a secret
organisation in it?" — under the same extraction discipline as
`fandom.py`. Tracked separately; this module only exposes the text.
"""
from __future__ import annotations

import json
import os
import re

from authors import canonical_author, surname_token
from features import normalize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SITE_BOOKS = os.path.abspath(os.path.join(REPO_ROOT, "..", "bookhub", "_books"))

_FRONT = re.compile(r"^---\n(.*?)\n---", re.S)


def _scalar(head: str, key: str) -> str:
    m = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', head, re.M)
    return (m.group(1) or "").strip() if m else ""


def _json_field(head: str, key: str):
    """A front-matter value that is inline JSON (`themes: [...]`)."""
    m = re.search(rf"^{key}:\s*([\[{{].*?[\]}}])\s*$", head, re.M | re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def load_site_books(path: str = SITE_BOOKS) -> list[dict]:
    """Every published book page, parsed.

    Returns the fields the game can use plus the summary body, which is
    the raw text an extraction pass would read.
    """
    if not os.path.isdir(path):
        return []

    out = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".md"):
            continue
        raw = open(os.path.join(path, name), encoding="utf-8").read()
        m = _FRONT.match(raw)
        if not m:
            continue
        head = m.group(1)
        title = _scalar(head, "title")
        if not title:
            continue

        body = raw[m.end():]
        # Tags out: the body is published HTML, and an extraction prompt
        # wants prose, not markup.
        prose = re.sub(r"<[^>]+>", " ", body)
        prose = re.sub(r"\s+", " ", prose).strip()

        out.append({
            "title": title,
            "author": _scalar(head, "author"),
            "slug": _scalar(head, "slug"),
            "year": _scalar(head, "published_year"),
            "cover_url": _scalar(head, "cover_url"),
            "google_id": _scalar(head, "google_id"),
            "openlibrary_id": _scalar(head, "openlibrary_id"),
            "categories": _json_field(head, "categories") or [],
            "themes": _json_field(head, "themes") or [],
            "characters": _json_field(head, "characters") or [],
            "free_ebook": bool(_json_field(head, "free_ebook")),
            # Goodreads count — used only to ORDER the additions, never
            # converted onto the readinglog scale. See supplement().
            "ratings": _json_field(head, "ratings"),
            "page_count": _scalar(head, "page_count"),
            "prose": prose,
        })
    return out


def index_by_identity(pages: list[dict]) -> dict[tuple[str, str], dict]:
    """(normalized title, normalized author) -> page, for matching."""
    return {(normalize(p["title"]), normalize(p["author"])): p for p in pages}


# ---------------------------------------------------------------------------
# Themes -> canonical features
# ---------------------------------------------------------------------------
# Only the shared head is worth asking about; the long tail of one-book
# themes cannot split a candidate set. Wording is a DRAFT and belongs in the
# owner's review pass, like every other question in this project.

THEME_QUESTIONS = {
    "site:comingofage": "Is it a coming-of-age story?",
    "site:socialclass": "Is social class important in it?",
    "site:redemption": "Is it a story about redemption?",
    "site:injustice": "Is it about injustice?",
    "site:power": "Is it about power and corruption?",
    "site:goodevil": "Is it a struggle between good and evil?",
    "site:survival": "Is it a story of survival?",
    "site:satire": "Is it satirical?",
}

THEME_RULES: list[tuple[str, list[str]]] = [
    ("site:comingofage", ["coming of age", "adolescen", "growing up", "childhood",
                          "innocence", "maturity"]),
    ("site:socialclass", ["social class", "class", "aristocra", "poverty",
                          "wealth and", "status"]),
    ("site:redemption", ["redemption", "forgiveness", "atonement", "second chance"]),
    ("site:injustice", ["injustice", "oppression", "racism", "prejudice",
                        "discrimination", "civil rights", "slavery"]),
    ("site:power", ["power and corruption", "corruption", "tyranny",
                    "totalitarian", "abuse of power", "ambition"]),
    ("site:goodevil", ["good versus evil", "good and evil", "morality",
                       "moral ambiguity", "temptation"]),
    ("site:survival", ["survival", "endurance", "resilience", "isolation"]),
    ("site:satire", ["satire", "social satire", "irony", "absurdity"]),
]


def theme_features(themes: list[str]) -> set[str]:
    """Canonical features for one book's theme list.

    Substring matching is safe here in a way it was not for places: these
    are our own editorial phrases, not a library's 8,835-string vocabulary,
    and they were read before the rules were written.
    """
    out: set[str] = set()
    normed = [normalize(t) for t in themes or []]
    for key, needles in THEME_RULES:
        if any(any(n_ in t for n_ in needles) for t in normed):
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# Corpus supplement
# ---------------------------------------------------------------------------

def _close(a: str, b: str, budget: int = 1) -> bool:
    """Are two surnames the same name spelled differently?

    Exists for one recurring, documented case: our pages carry both
    "Fyodor Dostoevsky" and "Fyodor Dostoyevsky" for the same novel, and
    Open Library has its own spellings again. Those are not typos — they
    are competing transliterations, and this project has been bitten by
    them since the Guess the Book pool ("Dostoyevsky" vs "Dostoevsky",
    "graf Leo Tolstoy", `hg-wells`).

    Budget of one edit, which covers every real case measured —
    Dostoevsky/Dostoyevsky, Tolstoy/Tolstoi, Carroll/Carrol are all a
    single insertion or substitution.
    
    IT IS NOT A SAFE TEST ON ITS OWN: "austen" and "auster" are also one
    edit apart, and Jane Austen is not Paul Auster. What makes it safe here
    is that it is only ever consulted when the MAIN TITLE already matches
    exactly, and those two authors share no titles. Do not lift this
    function into a context without that guard.
    """
    if a == b:
        return True
    if not a or not b or min(len(a), len(b)) < 6:
        return False
    if abs(len(a) - len(b)) > budget:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= budget


def _goodreads_count(head_json) -> int:
    if isinstance(head_json, dict):
        try:
            return int(head_json.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def supplement(docs: list[dict], pages: list[dict] | None = None,
               band: tuple[int, int] = (2000, 4800),
               verbose: bool = True) -> list[dict]:
    """Pin every published book into the corpus, adding the missing ones.

    Three groups, measured on the 128 published pages against the 20k
    corpus: 65 are already inside the shipped 5,000, 19 exist in the corpus
    but rank deeper than it, and 44 are absent entirely — Anna Karenina,
    A Christmas Carol, Around the World in Eighty Days among them. A player
    thinking of a book WE PUBLISH should never be told the game does not
    know it, and a correct guess lands them on a page we own.

    POPULARITY FOR THE SYNTHETIC ENTRIES is assigned by RANK, not by
    converting Goodreads counts. The conversion was measured and does not
    hold: among books present in both sources the readinglog/Goodreads
    ratio spans roughly fifteen-fold — one title with 4.95M Goodreads
    ratings has 328 Open Library readers while another with 4.60M has
    5,053. Treating the two scales as proportional would inject that noise
    straight into the prior, which is the single most load-bearing number
    in the engine. So Goodreads only ORDERS these books against each other,
    and they are then slotted into a conservative band of the existing
    distribution: present and findable, never outranking books whose
    popularity was measured on the same scale as everyone else's.
    """
    pages = load_site_books() if pages is None else pages
    if not pages:
        return docs

    # MATCH ON TITLE + AUTHOR SURNAME, not the full author string. Our
    # pages and Open Library disagree on author names constantly, and
    # matching strictly would re-create the duplication corpus_filter.py
    # just removed. Measured: 8 of 52 unmatched pages were the same book
    # under a different author spelling —
    #
    #   Ivanhoe            ours "Walter Scott"         OL "Sir Walter Scott"
    #   The Great Gatsby   ours "Francis Scott Key…"   OL "F. Scott Fitzgerald"
    #   War and Peace      ours "graf Leo Tolstoy"     OL "Лев Толстой"
    #   Through the Looking-Glass  ours "Lewis Lewis Carroll"  OL "Lewis Carroll"
    #
    # The same family of variants that produced `hg-wells` in the Guess the
    # Book pool. Surname is the part both sources agree on — except across
    # scripts, which is why a bare title match backs it up.
    def ident(title: str, author: str) -> tuple[str, str]:
        # Subtitles differ between sources for the same book — our page is
        # "A Christmas Carol : a 1843 novella" where Open Library has "A
        # Christmas Carol", and we hold two Crime and Punishment pages whose
        # titles differ only after the colon. Comparing the main title with
        # the author's surname is what makes those meet.
        main = re.split(r"[:;]| - ", title, 1)[0]
        return normalize(main), surname_token(canonical_author(author))

    have = {ident(d.get("title") or "", (d.get("author_name") or [""])[0])
            for d in docs}
    have_titles = {normalize(d.get("title") or "") for d in docs}
    missing = [p for p in pages
               if ident(p["title"], p["author"]) not in have
               and normalize(p["title"]) not in have_titles]
    if not missing:
        return docs

    # Our own pages duplicate too — two published pages for Crime and
    # Punishment, three spellings of Fitzgerald across two Gatsby pages.
    # Deduplicate the additions against each other before adding them, or
    # the supplement re-creates inside the game exactly the split it was
    # written to close. Most-reviewed page wins.
    ordered = sorted(missing, key=lambda p: -_goodreads_count(p.get("ratings")))
    seen: list[tuple[str, str]] = []
    deduped = []
    for p in ordered:
        title_key, surname = ident(p["title"], p["author"])
        if any(t == title_key and _close(sn, surname) for t, sn in seen):
            continue
        seen.append((title_key, surname))
        deduped.append(p)
    if verbose and len(deduped) != len(ordered):
        print(f"Site pages: {len(ordered) - len(deduped)} duplicate pages collapsed")
    ordered = deduped
    pop_scale = sorted((d.get("readinglog_count") or 0) for d in docs)
    lo, hi = band
    lo = min(lo, max(0, len(pop_scale) - 1))
    hi = min(hi, max(0, len(pop_scale) - 1))
    # pop_scale is ascending, corpus rank is descending — flip the band.
    top_val = pop_scale[max(0, len(pop_scale) - 1 - lo)]
    bot_val = pop_scale[max(0, len(pop_scale) - 1 - hi)]

    added = []
    n = max(1, len(ordered) - 1)
    for i, p in enumerate(ordered):
        frac = i / n
        added.append({
            "key": "/site/" + (p["slug"] or normalize(p["title"]).replace(" ", "-")),
            "title": p["title"],
            "author_name": [p["author"]] if p["author"] else [],
            "author_key": [],
            "first_publish_year": int(p["year"]) if str(p["year"]).isdigit() else None,
            # Our themes and categories ARE this book's subjects. They are
            # editorial judgments rather than catalogue strings, which is
            # why they are trusted here without the OL stop-list.
            "subject": list(p.get("themes") or []) + list(p.get("categories") or []),
            # Our pages store characters as {name, slug, role} objects
            # while the rest of the pipeline speaks Open Library's plain
            # `person` strings. Flattened to names here so one shape
            # reaches extract_characters(). The site lists the protagonist
            # first too, which is the ordering harvest_protagonist.py
            # relies on.
            "person": [c.get("name") for c in (p.get("characters") or [])
                       if isinstance(c, dict) and c.get("name")]
                      or [c for c in (p.get("characters") or [])
                          if isinstance(c, str)],
            "language": ["eng"],
            "readinglog_count": int(top_val - frac * (top_val - bot_val)),
            "ebook_access": "public" if p.get("free_ebook") else "",
            "_site_page": p["slug"],
        })

    out = docs + added
    out.sort(key=lambda d: -(d.get("readinglog_count") or 0))
    if verbose:
        print(f"Site pages: +{len(added)} books we publish that the corpus lacked")
    return out
