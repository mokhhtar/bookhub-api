"""
scripts/akinator/display_names.py — what a player should SEE on the reveal.

WHY THIS IS NOT A CORRECTION. `corrections.py` fixes facts the pipeline
computes with: a wrong `first_publish_year` changes six matrix bits, so it
must go through a rebuild. A title or an author name changes nothing the
engine reasons about — it is the string printed at the moment the game
says "you were thinking of…". That moment is the whole payoff, and it
currently prints `人間失格` to a player who was thinking of "No Longer
Human", and `Фёдор Михайлович Достоевский` to one thinking of Dostoyevsky.

THE MEASUREMENT THAT PUT IT HERE rather than in `corrections.py`. An
`author_name` correction was the obvious first idea, and checking what it
would actually reach ruled it out:

    book_traits(doc["author_key"], …)   author facts join on the OL id
    book_counts[author_key]             "prolific" counts by OL id
    AuthorIndex.add(name, ol_key)       merges by OL id first

All fourteen Dostoyevsky rows already carry the same `author_key`
(`OL22242A`), under two spellings — so the engine had never split him; only
the printed name differed. An `author_name` correction would therefore have
fixed the display and nothing else, and would have made the owner wait for
a full rebuild to do it. A display overlay does the same job immediately.

Deliberately NOT here: anything the engine reads. This file cannot change a
year, a subject, an author id, or a matrix bit. It renames what is shown,
and a rename that could alter an answer would be a correction wearing a
costume.

Read in two places, same file, same reasoning as `excluded.json`: the
CLIENT applies it at load so a fix is live as soon as Pages redeploys, and
`build_matrix.py` applies it when writing `books.json` so the next full
rebuild does not silently undo it.

A `t` RENAME ALSO STASHES THE TITLE IT REPLACED under `o`. The give-up
screen's book search is a substring match against the printed title, so a
rename that makes "La sombra del viento" print as "The Shadow of the Wind"
would otherwise make the Spanish title unsearchable — trading one blind
spot for another. `o` mirrors what the client's own `applyDisplayNames`
does, so a live client apply and a full rebuild produce the same shape.
"""
from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DISPLAY_PATH = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator", "display_overrides.json"))


def load_display_overrides() -> dict[str, dict]:
    """work key -> {"t": title, "a": author}, either key optional."""
    if not os.path.exists(DISPLAY_PATH):
        return {}
    try:
        with open(DISPLAY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def apply_display(rows: list[dict], verbose: bool = False) -> int:
    """Rewrite the `t`/`a` of shipped books.json rows in place.

    Takes books.json-shaped rows (`k`/`t`/`a`), not corpus docs, because
    that is the only shape where these fields mean "what is printed". A
    blank or non-string override is ignored rather than allowed to erase a
    real title. A `t` rename writes the replaced title to `o` first — see
    the module docstring for why.
    """
    overrides = load_display_overrides()
    if not overrides:
        return 0
    applied = 0
    for row in rows:
        fix = overrides.get(row.get("k") or "")
        if not fix:
            continue
        title = fix.get("t")
        if isinstance(title, str) and title.strip() and row.get("t") != title:
            if verbose:
                print(f"    display {row.get('k')}: t {row.get('t')!r} -> {title!r}")
            row["o"] = row.get("t")
            row["t"] = title
            applied += 1
        author = fix.get("a")
        if isinstance(author, str) and author.strip() and row.get("a") != author:
            if verbose:
                print(f"    display {row.get('k')}: a {row.get('a')!r} -> {author!r}")
            row["a"] = author
            applied += 1
    return applied
