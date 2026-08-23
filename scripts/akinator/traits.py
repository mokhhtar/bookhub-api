"""
scripts/akinator/traits.py — the evocative questions, extracted by model.

WHY A MODEL HERE, when pronoun counting beat Gemini for character gender.
Because gender is countable and this is not. The owner played the real
Akinator, was asked *"is this character associated with a tarot card?"*, and
noticed our questions are all catalogue facts — genre, era, length,
nationality. Those separate books. They do not delight.

The gap is not hypothetical. The game already HAS "does it take place at
sea?" and **never asks it**: Open Library's subjects flag it on 1.3% of the
corpus, under the 5% floor, so feature selection drops it. The signal is
real — Moby Dick, Treasure Island, Robinson Crusoe and The Sea-Wolf all
carry it — and the coverage is not. Twenty Thousand Leagues Under the Sea
is missed; Little Women is flagged.

Prose is where these facts live. `harvest_descriptions.py` fetches it (80%
of the corpus, median ~127 words) and this classifies it.

THE VOCABULARY IS AUTHORED, NOT DISCOVERED — the rule that has governed
every feature family in this project. We define the list; the model maps
into it and may return nothing. Letting a model invent its own labels would
reproduce Open Library's 8,835-string problem with worse provenance.

CHOSEN FOR TWO AXES, both of which phase 3 established have to hold:

  * **Does it split?** A trait on 1% of books eliminates nothing. So these
    are CATEGORIES of the fantastic, not one book's furniture — "is there
    organised magic?" survives where "is it associated with a tarot card?"
    is wonderful for Lord of Mysteries and meaningless for the other 4,999.
  * **Can a player answer it?** Every one of these is something a reader
    knows about a book they are picturing, without looking anything up.

GROUNDING. An extracted trait is a CLAIM about the book, and a wrong claim
eliminates the right book with confidence. So the prompt demands the text
support it, offers `unknown` as a first-class answer, and the parser drops
anything not in the vocabulary. Absence is `unknown`, never "no". Same
discipline as `_build_character_extraction_prompt` in fandom.py.
"""
from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_traits.json")

# key -> (player-facing question, what the model is told to look for)
#
# Wording is a DRAFT and belongs in the owner's review pass, like every
# other question here. The definitions matter more than the wording at this
# stage: they are what the model is actually judging against.
TRAITS: dict[str, tuple[str, str]] = {
    "t:sea": (
        "Does it take place at sea?",
        "A substantial part of the story happens on a ship, a boat, an "
        "ocean voyage, an island reached by sea, or among sailors."),
    # WORDED IDENTICALLY TO features.py's `theme:magic` ON PURPOSE, and it
    # has to be kept that way. `_drop_duplicate_wordings` in build_matrix.py
    # only fires on byte-identical player-facing text, and it is what stops
    # a player being asked about magic twice in one game (found by a player,
    # 2026-08-18). Reword one side alone and the guard silently stops
    # firing: this question comes back as a 50th column that answers
    # "present" for 602 books and `unknown` for 4,398 — a trait can never
    # assert "no" — which is exactly the near-useless question the dedup
    # was written to suppress. Change both or neither.
    "t:magic": (
        "Does it have supernatural elements?",
        "Magic, sorcery, spellcasting, or supernatural beings and powers — "
        "vampires, ghosts, demons, dragons, witches, monsters — are real "
        "within the story's world."),
    "t:secretorg": (
        "Is there a secret organisation in it?",
        "A secret society, hidden order, conspiracy, cult or clandestine "
        "agency plays a part in the plot."),
    "t:otherworld": (
        "Does it take place in an invented world?",
        "The setting is a world other than the real Earth — a secondary "
        "world, another planet, or a fantasy realm."),
    "t:detective": (
        "Is someone investigating a mystery?",
        "A character is investigating a crime, a disappearance or an "
        "unexplained event as a central thread."),
    "t:war": (
        "Does a war happen in it?",
        "A war, battle or armed conflict is part of the story, not merely "
        "mentioned as background."),
    "t:romance": (
        "Is there a central love story?",
        "A romantic relationship is one of the main threads, not a "
        "background detail."),
    "t:animals": (
        "Are animals important characters?",
        "Animals are significant characters, whether they speak or not."),
    "t:child": (
        "Is the main character a child or teenager?",
        "The protagonist is under about eighteen for most of the story."),
    "t:realevents": (
        "Is it based on real events or real people?",
        "The story depicts actual historical events or real people, "
        "including memoir and biography."),
    "t:future": (
        "Is it set in the future?",
        "The story takes place later than the time it was written."),
    "t:travel": (
        "Is it a journey across many places?",
        "The story follows a journey, voyage or quest across several "
        "distinct places."),
    "t:family": (
        "Is it about a family?",
        "The relationships within one family are a central subject."),
    "t:school": (
        "Does it take place at a school?",
        "A school, academy or university is a main setting."),
    "t:survival": (
        "Is it about surviving danger?",
        "Characters must survive a hostile environment, disaster or "
        "sustained threat."),
    "t:funny": (
        "Is it funny?",
        "The book is written to be humorous, comic or satirical."),
    # LAYER 2, 2026-08-21. The owner asked for a "power system" question and
    # the subject data cannot answer it: that vocabulary was searched for in
    # BOTH catalogues and is absent — Open Library gives 33 apparent hits on
    # the shipped 5,000 of which nearly all are `nervous system`, `limbic
    # system`, `system analysis` and `esperanto`, and the Fandom SUBJECT
    # harvest gives three books, two of them false. It is not in the
    # metadata anywhere.
    #
    # It IS in the prose. So it is asked of the labeller against the wiki
    # text instead, which is what this vocabulary is for.
    #
    # DELIBERATELY NARROWER THAN `theme:magic`. That question now asks
    # "Does it have supernatural elements?" and Harry Potter, Dracula and
    # LOTR all answer yes to it. This one asks about a CODIFIED, ranked
    # progression the story states rules for — the thing a cultivation or
    # LitRPG reader recognises instantly and a Dracula reader does not.
    # Two questions that both mean "is it fantasy" would be worth nothing;
    # the wording keeps them apart, and the dedup guard checks they are not
    # byte-identical.
    "t:powersystem": (
        "Does it have a ranked system of powers with stated rules?",
        "The story sets out an explicit, ranked way of gaining or measuring "
        "power that characters advance through — cultivation stages, levels, "
        "ranks, classes, quirks, chakra, mana, a literal 'system' — with "
        "rules the text states. Ordinary magic, a gift, or an unexplained "
        "supernatural ability is NOT this: the ranking and its rules must be "
        "part of how the story works."),
}

TRAIT_QUESTIONS = {k: v[0] for k, v in TRAITS.items()}


def build_prompt(title: str, author: str, text: str,
                 vocab: dict[str, tuple[str, str]] | None = None) -> str:
    """One book, one prompt. Grounded on the supplied text only.

    `vocab` is which labels to ask about — defaults to the full `TRAITS`,
    byte-for-byte what every caller got before this parameter existed.
    Narrower on purpose for `propose_questions.py`'s sample measurement and
    for `extract_traits.py --keys`'s single-dimension backfill: a shorter
    vocabulary means a shorter prompt, not a different judgment on the
    labels that ARE asked about.
    """
    vocab = TRAITS if vocab is None else vocab
    lines = [f'- "{k}": {defn}' for k, (_q, defn) in vocab.items()]
    return (
        "You are labelling a book for a guessing game, using ONLY the "
        "description supplied below.\n\n"
        f"BOOK: {title}\n"
        f"AUTHOR: {author}\n"
        f"DESCRIPTION:\n{text}\n\n"
        "For each label, decide whether the description supports it.\n\n"
        "LABELS:\n" + "\n".join(lines) + "\n\n"
        "Rules, in order of importance:\n"
        "1. Answer ONLY from the description above. Do not use anything you "
        "know about this book from elsewhere.\n"
        "2. If the description does not make a label clear, LEAVE IT OUT. "
        "Omitting a label is always better than guessing it. A wrong label "
        "removes the correct book from the game.\n"
        "3. Include a label only when the description gives you real reason "
        "to, not because it sounds like the kind of book that might have it.\n"
        "4. If the description is too short or vague to judge anything, "
        "return an empty list.\n\n"
        'Reply with JSON only, in this exact shape:\n'
        '{"labels": ["t:sea", "t:survival"]}\n'
    )


def parse_response(raw: str,
                   vocab: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """Model output -> validated label list.

    Anything outside `vocab` is dropped rather than trusted: a model that
    invents `t:pirates` has stopped classifying and started writing, and an
    unknown key would sail straight through the feature pipeline as a
    question nobody authored. `vocab` must be the SAME dict `build_prompt`
    was called with — validating against the full `TRAITS` when only a
    subset was asked about would let a hallucinated label for an
    unrequested key pass as real, which is exactly the false-confidence
    this check exists to prevent.
    """
    vocab = TRAITS if vocab is None else vocab
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
    result = data.get("labels")
    if not isinstance(result, list):
        return []
    return sorted({l for l in result if isinstance(l, str) and l in vocab})


def load_traits(path: str = OUT_PATH) -> dict[str, list[str]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    return saved.get("traits", saved)


def apply_labels(book: dict, work_key: str,
                 extracted: dict[str, list[str]]) -> None:
    """Merge extracted labels into one book's `present` / `unknown` sets.

    ONE implementation, imported by both `build_matrix.py`, which packs the
    artifact the game ships, and `simulate.py`, which measures it. They had
    a copy each, both of which omitted the unlabelled case below, and fixing
    only one would have been worse than fixing neither: the simulator would
    have been measuring a different game from the one being shipped, and
    "the offline measurements describe the live game" is the whole reason
    the simulator is trusted.

    Three states, and all three are load-bearing:

      asserted     -> present, P(yes) 0.90
      not asserted -> unknown, because the model is told to omit whatever
                      the description does not support. Silence is "the text
                      did not say", never "no".
      no entry     -> unknown as well. A book whose description was never
                      harvested (1,352 of 5,000) has not been examined at
                      all, which is the same absence of evidence, arrived at
                      one step earlier.

    The last case must be written out. Both callers pack "in neither set" as
    ABSENT, so leaving it implicit asserts *no magic, not at sea, no
    romance* about every unexamined book, on all sixteen trait columns.
    """
    labels = extracted.get(work_key or "")
    if labels is None:
        book["unknown"] = sorted(set(book["unknown"]) | set(TRAITS))
        return
    book["present"] = sorted(set(book["present"]) | set(labels))
    book["unknown"] = sorted(set(book["unknown"]) | (set(TRAITS) - set(labels)))


# ---------------------------------------------------------------------------
# Batched prompting
# ---------------------------------------------------------------------------
# The vocabulary is 575 tokens and the book is 201. Sending one book per call
# pays the vocabulary 3,590 times over — 2.79M tokens for the corpus, which
# on a free tier is measured in days rather than hours. Stating it once and
# labelling several books against it costs ~316 tokens a book at five, ~258
# at ten: a 2.5-3x reduction for no loss of information.
#
# Eight is the size used. The marginal saving past that is small while the
# blast radius of a malformed reply keeps growing, and a model asked to hold
# more than a handful of books in one answer starts blurring them together.
BATCH_SIZE = 8


def build_batch_prompt(books: list[tuple[str, str, str]],
                       vocab: dict[str, tuple[str, str]] | None = None) -> str:
    """One prompt, several books. `books` is [(title, author, text), ...].

    `vocab` — see `build_prompt`; defaults to the full `TRAITS`.
    """
    vocab = TRAITS if vocab is None else vocab
    lines = [f'- "{k}": {defn}' for k, (_q, defn) in vocab.items()]
    blocks = []
    for i, (title, author, text) in enumerate(books, 1):
        blocks.append(f"### BOOK {i}\nTITLE: {title}\nAUTHOR: {author}\n"
                      f"DESCRIPTION: {text}")
    return (
        "You are labelling books for a guessing game, using ONLY each book's "
        "own description below.\n\n"
        "LABELS:\n" + "\n".join(lines) + "\n\n"
        + "\n\n".join(blocks) + "\n\n"
        "Rules, in order of importance:\n"
        "1. Judge each book ONLY from its own description. Never let one "
        "book's description influence another's labels.\n"
        "2. Answer only from the descriptions. Do not use anything you know "
        "about these books from elsewhere.\n"
        "3. If a description does not make a label clear, LEAVE IT OUT. "
        "Omitting is always better than guessing — a wrong label removes the "
        "correct book from the game.\n"
        "4. A book whose description is too short or vague gets an empty "
        "list. That is a valid answer.\n"
        f"5. Return exactly {len(books)} entries, numbered 1 to {len(books)}, "
        "in the same order as the books above.\n\n"
        'Reply with JSON only, in this exact shape:\n'
        '{"books": [{"n": 1, "labels": ["t:sea"]}, {"n": 2, "labels": []}]}\n'
    )


def parse_batch_response(raw: str, expected: int,
                         vocab: dict[str, tuple[str, str]] | None = None
                         ) -> list[list[str]] | None:
    """Batch reply -> one validated label list per book, or None.

    None means "do not trust any of this" and the caller should retry the
    batch one book at a time. Returning a partially-aligned list would be
    the worst outcome available: labels silently attached to the wrong
    books, which is exactly the failure the whole grounding discipline
    exists to avoid. `vocab` — see `parse_response`; must match what
    `build_batch_prompt` was called with.
    """
    vocab = TRAITS if vocab is None else vocab
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    rows = data.get("books")
    if not isinstance(rows, list) or len(rows) != expected:
        return None

    out: list[list[str] | None] = [None] * expected
    for row in rows:
        if not isinstance(row, dict):
            return None
        n = row.get("n")
        if not isinstance(n, int) or not (1 <= n <= expected):
            return None
        if out[n - 1] is not None:
            return None          # duplicate index: alignment is not provable
        labels = row.get("labels")
        if not isinstance(labels, list):
            return None
        out[n - 1] = sorted({l for l in labels
                             if isinstance(l, str) and l in vocab})
    if any(v is None for v in out):
        return None
    return out  # type: ignore[return-value]
