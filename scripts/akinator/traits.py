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
    "t:magic": (
        "Is there magic in it?",
        "Magic, sorcery, spellcasting or supernatural powers are real "
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
}

TRAIT_QUESTIONS = {k: v[0] for k, v in TRAITS.items()}


def build_prompt(title: str, author: str, text: str) -> str:
    """One book, one prompt. Grounded on the supplied text only."""
    lines = [f'- "{k}": {defn}' for k, (_q, defn) in TRAITS.items()]
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


def parse_response(raw: str) -> list[str]:
    """Model output -> validated label list.

    Anything outside the vocabulary is dropped rather than trusted: a model
    that invents `t:pirates` has stopped classifying and started writing,
    and an unknown key would sail straight through the feature pipeline as
    a question nobody authored.
    """
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
    labels = data.get("labels")
    if not isinstance(labels, list):
        return []
    return sorted({l for l in labels if isinstance(l, str) and l in TRAITS})


def load_traits(path: str = OUT_PATH) -> dict[str, list[str]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    return saved.get("traits", saved)
