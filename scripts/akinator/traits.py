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
anything not in the vocabulary. Same discipline as
`_build_character_extraction_prompt` in fandom.py.

"NO" IS NOW A THIRD ANSWER, and this paragraph is the argument for a rule
this file used to state absolutely ("absence is `unknown`, never no").

The old rule collapsed two different situations into one. A description
that does not mention war might be two lines long — or it might be a full
paragraph about a cookery memoir. The first is genuinely "we cannot say";
the second is a book we KNOW has no war in it, recorded as though we had
never looked.

What that cost, measured on the shipped matrix: the eleven `t:` columns are
87-100% `unknown`, and an unknown cell is 0.5 for every book, so it cancels
in the normalisation and moves nothing. Together with `fact:namedchars`
those twelve questions carry **0.448 nats** of expected information —
against **0.236 for `fact:pre2000` alone**. Twelve questions are worth less
than two. Scoring the same corpus with those unknowns as absent instead
gives **1.421 nats, 3.2x**, and lifts each one from ~0.03 to ~0.11, which is
where `genre:fantasy` (0.0975) already sits.

The grounding rule is not weakened, it is made conditional on evidence:
`NEGATIVE_MIN_WORDS` gates the negative on there being enough description
to support one, the prompt tells the model plainly when it may not answer
"no", and `extract_traits` strips negatives from any book that did not
qualify rather than trusting the model to have obeyed. Everything below the
gate behaves exactly as it did before.

**CALIBRATED 2026-09-01, AND IT PASSED.** 194 published pages, one call
each, zero failures:

    "no" answers given                          1,617
    ...that contradict a theme our pages assert     4   (0.2%)

All four are defensible and at least two are the model being RIGHT where
our editorial theme is loose: it denied `t:magic` for The Hound of the
Baskervilles, where the whole plot is that the hound is not supernatural,
and for The Turn of the Screw, whose entire critical literature is the
argument about whether the ghosts are real. The other two are
`t:realevents` for a time-travel comedy (our "historical" theme phrase
trips the hint) and `t:romance` for The Little Prince.

Shape, which matters as much as the rate: mean 8.3 negatives a book, median
8, p90 of 13, max 16 of 17 — and **no book denied all seventeen, nor zero**.
That is the failure mode draft 2 had, and it is gone.

READ THE 0.2% FOR WHAT IT IS. It measures denials that contradict a human
judgement WE HOLD. Our page themes list about four per book and are sparse
by design, so a denial of something true that we never wrote down cannot be
counted. It is a lower bound on the error, measured where ground truth
exists — the strongest evidence available here, not a proof.

Projected effect: ~8.3 cells a book across 4,157 harvested descriptions is
about 34,600 cells moving from `unknown` (0.5, moves nothing) to `absent`
(0.15-0.45, moves belief).

How it got here — three drafts against real books and real calls, the first
two failing in opposite directions:

  draft 1  "no" only when the text SHOWS the label is false
           -> at most ONE negative per book; nothing at all for Sapiens,
              The Hobbit or Rich Dad Poor Dad. Prose never denies things
              it is not about, so the rule was unsatisfiable.
  draft 2  the "would have been mentioned" test, no brake
           -> SIXTEEN of seventeen labels denied for Rich Dad Poor Dad and
              for Sapiens. "No to everything I did not say yes to" is the
              second answer wearing a hat, and it ships as `absent`.
  draft 3  same test plus rules 4-6 (the brake)
           -> 6.8 negatives a book. The Hobbit and The Hound of the
              Baskervilles come back exactly right on both lists. Rich Dad
              Poor Dad STILL denies `t:child`, which is wrong — much of it
              is the author's boyhood — and non-fiction lost the positives
              draft 2 found.

Four books is not a measurement, which is the whole reason
`extract_traits.py --calibrate` exists and now scores negatives separately:
a theme our pages assert that the model MISSED ships as `unknown` and moves
nothing, while one it DENIED ships as `absent` and argues against the
correct book. Only the second can lose a game. Draft 3 was shipped on that
run's 0.2%, not on the four books above.

STILL INERT UNTIL A HARVEST RUNS. `akinator_traits.json` holds zero
negatives today, and on 400 of its books `apply_labels` returns
byte-identical sets to the logic it replaced. Nothing in the shipped game
moves until `extract_traits.py --limit 5000` has been re-run AND
`build_matrix.py` has repacked — at which point twelve questions change
what they are worth, so that rebuild wants `simulate.py` paired against the
current artifact before it ships.
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

# How much description it takes before "the text does not mention war" is
# allowed to become "there is no war in this book".
#
# MEASURED ON THE HARVEST, not chosen for roundness. 4,157 descriptions:
# p10 is 28 words, p25 is 54, the median is 104. At 60 words, 72% of books
# (3,009) may assert a negative and the shortest quarter may not — which is
# the quarter where a missing mention proves nothing at all. Raising it to
# 80 costs 400 books for very little extra safety; dropping it to 25 admits
# one-line blurbs, which is the whole failure this gate exists to stop.
#
# The negative is worth ~11 cells per qualifying book, so this gate is the
# difference between ~33,000 cells moving from `unknown` to `absent` and
# ~45,000 — and the 12,000 it declines are exactly the untrustworthy ones.
NEGATIVE_MIN_WORDS = 60

# How a negative is written down. A flat list keeps `load_traits`,
# `akinator_traits.json` and every existing consumer working unchanged: a
# file written before this feature simply has no "-" entries and behaves
# exactly as it always did. The alternative — a dict per book — would have
# been tidier and would have made every older file unreadable.
NEGATIVE_PREFIX = "-"


def allows_negatives(text: str) -> bool:
    """Is this description long enough for "no" to mean anything?

    Used in two places on purpose: `build_*_prompt` tells the model when it
    may not answer "no", and `extract_traits.extract_batch` strips negatives
    from books that did not qualify. The prompt is the request; the strip is
    the guarantee. A model quietly ignoring rule 4 must not be able to
    assert a fact about a book nobody could judge.
    """
    return len((text or "").split()) >= NEGATIVE_MIN_WORDS


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
    neg_ok = allows_negatives(text)
    return (
        "You are labelling a book for a guessing game, using ONLY the "
        "description supplied below.\n\n"
        f"BOOK: {title}\n"
        f"AUTHOR: {author}\n"
        f"DESCRIPTION:\n{text}\n\n"
        "For each label, decide whether it is TRUE of this book, FALSE of "
        "it, or impossible to tell from the description.\n\n"
        "LABELS:\n" + "\n".join(lines) + "\n\n"
        + _RULES(neg_ok) +
        'Reply with JSON only, in this exact shape:\n'
        '{"yes": ["t:sea"], "no": ["t:war", "t:funny"]}\n'
    )


# The three-way rules, shared by the single and batch prompts so the two
# cannot drift into asking for different judgements. `plural` switches the
# wording for a prompt carrying several books.
def _RULES(neg_ok: bool, plural: bool = False) -> str:
    it = "each description" if plural else "the description"
    rules = [
        f"1. Answer ONLY from {it}. Do not use anything you know about "
        f"{'these books' if plural else 'this book'} from elsewhere.",
        '2. "yes" means the description gives you real reason to say the '
        "label is true — not that it sounds like the kind of book that "
        "might have it. A wrong yes removes the correct book from the game.",
    ]
    if neg_ok:
        rules += [
            # THE "WOULD HAVE BEEN MENTIONED" TEST, and the first draft of
            # this rule is why it is worded this way. That draft asked for
            # "no" only when the description SHOWS the label does not apply
            # — which prose essentially never does, because a summary of a
            # history of humankind does not go on to deny it contains a
            # detective. Measured on six real books: the model returned at
            # most one negative each and none at all for Sapiens, The Hobbit
            # or Rich Dad Poor Dad, every one of which a reader would rule
            # out instantly. The licence has to be about what a description
            # of this length WOULD have said, not about what it did say.
            '3. "no" is for labels you can rule out. Use it when the '
            "description makes the book's subject and kind clear, AND the "
            "label — if it were true — would have been a central, "
            "unmissable part of such a book, so a description this long "
            "would have said so. A summary of a history of humankind would "
            'have mentioned a detective if there were one: answer "no". '
            "This is the most useful answer you can give.",
            # THE BRAKE, and it exists because the rule above without it
            # swung the model straight to the opposite failure: asked about
            # six real books it answered "no" to SIXTEEN of seventeen labels
            # for Rich Dad Poor Dad and Sapiens — including t:child for a
            # book largely about the author's boyhood. "No to everything I
            # did not say yes to" is not the third answer, it is the second
            # one wearing a hat, and it ships as `absent` where it argues
            # against the correct book.
            '4. But do NOT rule out something the book could quietly '
            "contain: a minor journey, one funny chapter, a background war, "
            "a childhood the summary skips. Those go in neither list.",
            '5. If you find yourself answering "no" to nearly every label, '
            "stop — you are judging the book's genre, not the book. Rule "
            "out the few a reader would call obviously impossible for THIS "
            "book and leave the rest undecided. Most books should get a "
            "handful of \"no\"s, not a dozen.",
            "6. If the description is a bare blurb — a tagline, a sales "
            "line, a list of praise — rule nothing out at all, however "
            "obvious it seems.",
            "7. Anything left over goes in NEITHER list. That is not a "
            "failure, it is the third answer.",
        ]
    else:
        rules += [
            '3. This description is too short to rule anything out. Return '
            'an EMPTY "no" list, whatever you may suspect.',
            "4. Anything you cannot confirm, leave out of both lists.",
        ]
    rules.append(f"{len(rules) + 1}. A label must never appear in both lists.")
    return "Rules, in order of importance:\n" + "\n".join(rules) + "\n\n"


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
    return _merge(data, vocab)


def _merge(row: dict, vocab: dict) -> list[str] | None:
    """{"yes": [...], "no": [...]} -> ["t:sea", "-t:war"], validated.

    Accepts the OLD single-list shape (`{"labels": [...]}`) as positives, so
    a model that reverts to the previous format still produces usable
    output instead of nothing. Cheap, and it means a half-rolled-out prompt
    change loses no data.

    A label in BOTH lists is dropped from both. The model has contradicted
    itself about that label and neither answer can be trusted — which is
    what `unknown` is for. Returns None when the shape is unusable at all,
    so the batch parser can reject a whole reply.
    """
    yes = row.get("yes")
    if yes is None:
        yes = row.get("labels")          # legacy single-list reply
    no = row.get("no") or []
    if not isinstance(yes, list) or not isinstance(no, list):
        return None
    y = {l for l in yes if isinstance(l, str) and l in vocab}
    n = {l for l in no if isinstance(l, str) and l in vocab}
    both = y & n
    y -= both
    n -= both
    return sorted(y) + sorted(NEGATIVE_PREFIX + l for l in n)


def split_labels(labels: list[str]) -> tuple[set[str], set[str]]:
    """A stored flat list -> (asserted, denied). The inverse of `_merge`."""
    yes, no = set(), set()
    for l in labels or ():
        (no if l.startswith(NEGATIVE_PREFIX) else yes).add(
            l[len(NEGATIVE_PREFIX):] if l.startswith(NEGATIVE_PREFIX) else l)
    return yes, no


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
    yes, no = split_labels(labels)
    book["present"] = sorted(set(book["present"]) | yes)
    # DENIED TRAITS GO INTO NEITHER SET, which is how both callers spell
    # "absent" — the state this function's docstring has always warned about
    # falling into by accident. Reaching it ON PURPOSE, from a model that
    # said "no" about a description long enough to support one, is the whole
    # point of NEGATIVE_MIN_WORDS: it turns 0.5 (moves nothing) into
    # absence_confidence(richness), 0.15-0.45, which does.
    book["unknown"] = sorted(set(book["unknown"]) | (set(TRAITS) - yes - no))


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
    any_neg = False
    for i, (title, author, text) in enumerate(books, 1):
        # PER BOOK, because a batch mixes a 300-word synopsis with a
        # one-line blurb and the gate is a property of the description, not
        # of the call. Saying it beside the text it applies to is what keeps
        # the model from carrying one book's licence over to the next.
        neg = allows_negatives(text)
        any_neg = any_neg or neg
        note = ("may use \"no\"" if neg else
                "TOO SHORT — leave this book's \"no\" list empty")
        blocks.append(f"### BOOK {i}  [{note}]\nTITLE: {title}\n"
                      f"AUTHOR: {author}\nDESCRIPTION: {text}")
    return (
        "You are labelling books for a guessing game, using ONLY each book's "
        "own description below.\n\n"
        "For each label, decide whether it is TRUE of that book, FALSE of "
        "it, or impossible to tell from its description.\n\n"
        "LABELS:\n" + "\n".join(lines) + "\n\n"
        + "\n\n".join(blocks) + "\n\n"
        + _RULES(any_neg, plural=True).rstrip("\n")
        + "\n6. Judge each book ONLY from its own description. Never let one "
        "book's description influence another's labels.\n"
        "7. A book marked TOO SHORT above gets an empty \"no\" list, however "
        "obvious an answer may seem.\n"
        f"8. Return exactly {len(books)} entries, numbered 1 to {len(books)}, "
        "in the same order as the books above.\n\n"
        'Reply with JSON only, in this exact shape:\n'
        '{"books": [{"n": 1, "yes": ["t:sea"], "no": ["t:war"]}, '
        '{"n": 2, "yes": [], "no": []}]}\n'
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
        merged = _merge(row, vocab)
        if merged is None:
            return None
        out[n - 1] = merged
    if any(v is None for v in out):
        return None
    return out  # type: ignore[return-value]
