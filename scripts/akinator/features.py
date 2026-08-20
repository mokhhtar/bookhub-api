"""
scripts/akinator/features.py — turns raw Open Library metadata into game features.

WHY THIS IS THE HARD PART. Measured on the Phase 0 sample of 500 books:
8,835 distinct raw subject strings, of which **6,625 appear exactly once**.
A feature no two books share cannot split a candidate set, so three quarters
of the raw vocabulary is dead weight before we even start. The rest arrives
in families that mean one thing and spell it many ways:

    Love / love / Love, fiction / Love stories / Love & Romance
    Self-help / Self-Help / SELF-HELP / Self-help techniques
    Fiction / Ficcion / Ficción / Novela / Romans, nouvelles

Plus a category that is not about the book at all: "New York Times
bestseller", "Large type books", "Open Library Staff Picks", "Reading
Level-Grade 11", "open_syllabus_project". Those are library and collection
artifacts. They would make perfectly stable features and tell a player
nothing about the book they are thinking of.

THE APPROACH, and why not raw clustering. Two stages:

  1. `normalize()` — mechanical collapse (case, diacritics, punctuation,
     the ", fiction" / ", general" / ", etc." suffixes). This alone merges
     most spelling families without any human judgment.
  2. `SUBJECT_RULES` — a **curated keyword -> canonical feature** table.
     This is the deliberate design decision: we do NOT treat the source's
     vocabulary as our vocabulary. We define the feature list we want to
     ask questions about, and map raw strings into it. ~100 rules covers
     what 8,835 raw strings were trying to say.

     The alternative — auto-clustering raw strings — was rejected because
     the clusters still need naming and checking by hand, so it adds a
     step without removing the judgment, and it makes the mapping unstable
     across monthly refreshes.

GROUNDING. A raw subject matching no rule maps to nothing. It does NOT get
guessed into the nearest bucket, and its absence is never read as a denial —
see `absence_confidence()` for how "this book lacks the sea-stories subject"
is scored as weak evidence rather than proof. Per [[Grounding Rule]],
treating a gap in OL's metadata as a "no" would confidently eliminate the
correct book, which is the worst failure this game can have.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Stage 1 — mechanical normalization
# ---------------------------------------------------------------------------

# Suffixes OL appends to express "this subject, as it applies to fiction".
# They carry no information the `fiction` feature doesn't already carry.
# `\b` is load-bearing: without it this strips the qualifier off the END OF
# A WORD, not just off the end of a string. "sciencefiction" became
# "science" — which is what defeated the compound collapse below on the
# first attempt, and would defeat any future one the same way.
_TRAILING_QUALIFIERS = re.compile(
    r",?\s*\b(fiction|general|etc\.?|juvenile|nonfiction|"
    r"history and criticism|criticism and interpretation)\s*$",
    re.I,
)

_PUNCT = re.compile(r"[\"'“”‘’\(\)\[\]{}.:;!?]+")
_WS = re.compile(r"\s+")

# Compound genre names whose second word the trailing-qualifier strip would
# otherwise eat, changing what the phrase means. Collapsed to one token so
# the phrase survives as itself — `genre:scifi` and `form:fiction` both
# already match `sciencefiction`. See the note in normalize().
_SCIFI_COMPOUND = re.compile(r"\bscience[\s-]+fiction\b", re.I)


def normalize(raw: str) -> str:
    """Collapse a raw subject/name string to a comparable form.

    Lowercase, strip diacritics, drop punctuation, remove trailing
    qualifiers. `Ficción` and `Fiction` and `"Fiction,"` all land on
    `fiction`. Applied identically at build time and at lookup time — one
    normalizer, so two spellings can never resolve differently.
    """
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = _PUNCT.sub(" ", s)
    # "Science fiction" is the NAME OF A GENRE, not the word "science" with
    # a trailing qualifier, and the strip below cannot tell the difference.
    # It turned the most common science-fiction subject in the corpus into
    # the bare word "science", which is wrong twice over:
    #
    #   "Science fiction"  ->  "science"  ->  genre:science YES (false)
    #                                         genre:scifi   no  (missing)
    #
    # So 900 books answered YES to "Is it about science?" — 505 of them
    # novels, including Harry Potter, A Game of Thrones, The Hobbit,
    # Animal Farm and Kafka's Metamorphosis — while the books actually
    # tagged "Science fiction" were not marked as science fiction at all.
    # `genre:scifi` has carried "sciencefiction" as a keyword all along,
    # waiting for a collapse that never happened; this is it.
    #
    # Found by the owner playing: they answered "no" to "Is it about
    # science?" about The Road, correctly, and the table scored it a FIRM
    # clash — the heaviest penalty in the belief update — against the very
    # book they had in mind. Fifth time play has beaten simulation to a bug.
    #
    # Done HERE rather than as a rule exclusion because the evidence is
    # destroyed here: by the time any rule runs, the word "fiction" is
    # already gone and nothing downstream can tell the two cases apart.
    s = _SCIFI_COMPOUND.sub("sciencefiction", s)
    # Applied repeatedly: "adventure and adventurers, fiction, general".
    for _ in range(3):
        new = _TRAILING_QUALIFIERS.sub("", s).strip(" ,-")
        if new == s:
            break
        if not new:
            # The qualifier WAS the whole subject. Stripping it here erased
            # the most common subject in the entire dataset — "Fiction" was
            # the #1 raw string in the phase 0 sample (298 of 500 books) and
            # normalized to the empty string, so the most fundamental
            # question a player could be asked never existed. Keep it.
            break
        s = new
    return _WS.sub(" ", s).strip()


# Subjects that describe the *edition, collection or library record* rather
# than the book. Stable, common, and useless as questions.
STOP_SUBJECT_PATTERNS = [
    "new york times", "open library", "staff picks", "large type",
    "reading level", "open_syllabus", "long now", "accessible book",
    "protected daisy", "in library", "overdrive", "lending library",
    "language materials", "translations into", "collectible",
    "bestseller", "book club", "award", "internet archive",
]


def is_stop_subject(normalized: str) -> bool:
    return any(p in normalized for p in STOP_SUBJECT_PATTERNS)


# Words that describe a character's ROLE rather than name them. An entry
# made only of these is not a name.
_GENERIC_CAST = {
    "the", "a", "an", "and", "or", "of", "his", "her", "their", "its",
    "man", "men", "woman", "women", "boy", "boys", "girl", "girls",
    "child", "children", "kid", "kids", "baby", "person", "people",
    "narrator", "protagonist", "hero", "heroine", "villain", "author",
    "main", "character", "characters", "unnamed", "nameless", "reader",
    "young", "old", "little", "fictitious",
}


def has_named_characters(persons: list[str]) -> bool:
    """Does this book have characters a reader could NAME?

    THE CASE THIS EXISTS FOR. The owner played *The Road* and answered "no"
    to "does it have well-known named characters?" — correctly: McCarthy's
    two characters are "the man" and "the boy", and their namelessness is
    the point of the book. Open Library files those strings under `person`,
    the feature was `bool(doc.get("person"))`, so the game asserted they
    were named, scored a firm and correct answer as a contradiction, and
    pushed a rank-363 book down for it.

    WHY NOT REUSE `usable_token`, which was the obvious first fix and is
    wrong. That filter answers a different question — "is this token worth
    ASKING about" — and requires four characters, so it rejects **Ove**,
    **Pi** and **Cat in the Hat**. Measured over the corpus it flipped 33
    books and several of the flips were plainly wrong. `extract_characters`
    is no better here: it discards `"Lucy Pevensie and Edmund Pevensie"`
    entirely, returning no names for a book that obviously has them.

    So this is deliberately conservative in the other direction: a book
    loses the feature only when EVERY entry is made purely of role words.
    A false "yes" costs a little belief; a false "no" contradicts a player
    who is right, which is the failure that started this.
    """
    for entry in persons:
        tokens = [t for t in normalize(entry).split() if t]
        if not tokens:
            continue
        if any(t not in _GENERIC_CAST for t in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Stage 2 — the curated vocabulary
# ---------------------------------------------------------------------------
# (feature_key, question wording, [substrings that imply it])
#
# Wording here is a DRAFT. Phase 3 of the plan is the owner reviewing every
# question by hand — "Sea stories" -> "Does it take place at sea?" is a
# human judgment and is explicitly not auto-generated.
#
# Order matters: the first matching rule wins, so put specific genres above
# the broad "fiction"/"literature" catch-alls.

SUBJECT_RULES: list[tuple[str, str, list[str]]] = [
    # --- the most basic split of all ---
    # Added 2026-08-07 after the owner asked whether the matrix could answer
    # "is the book fiction?" and the honest answer was no. It had been
    # invisible rather than considered: `normalize()` treated a trailing
    # "fiction" as a qualifier to strip, so the single most common subject
    # in the corpus collapsed to an empty string before any rule saw it.
    #
    # `, fiction` as a SUFFIX ("Friendship, fiction") is still stripped —
    # that is a real qualifier — but it now also registers the fiction
    # signal here rather than discarding it.
    ("form:fiction", "Is it fiction (a made-up story)?",
     # "sciencefiction" is the collapsed compound from normalize(); without
     # it a book whose only subject is "Science fiction" would not be
     # marked as fiction at all — which was already true before the
     # collapse existed, since that subject normalized to "science".
     ["fiction", "sciencefiction", "novel*", "stories", "tales", "roman*",
      "ficcion", "novela"]),
    ("form:nonfiction", "Is it a factual, non-fiction book?",
     ["nonfiction", "non fiction", "true stor*", "essays"]),

    # --- genre: fiction ---
    ("genre:fantasy", "Is it a fantasy story?",
     ["fantasy", "wizard*", "dragon*", "elves", "sword and sorcery", "mythical"]),
    ("genre:scifi", "Is it science fiction?",
     ["science fiction", "sciencefiction", "dystopi*", "space opera", "time travel",
      "cyberpunk", "steampunk", "extraterrestrial"]),
    ("genre:mystery", "Is it a mystery or detective story?",
     ["mystery", "detective*", "whodunit", "private investigator", "sleuth"]),
    ("genre:thriller", "Is it a thriller or suspense story?",
     ["thriller", "suspense", "spies*", "espionage", "conspiracy"]),
    ("genre:horror", "Is it a horror story?",
     ["horror", "ghost*", "vampire*", "haunted", "zombie", "monsters", "occult"]),
    ("genre:romance", "Is it a romance?",
     ["romance", "love stor*", "romantic", "courtship", "enemies to lovers",
      "forbidden love", "first love"]),
    ("genre:historicalfic", "Is it historical fiction?",
     ["historical fiction", "fiction historical", "historical romance"]),
    ("genre:adventure", "Is it an adventure story?",
     ["adventure", "quest", "treasure", "exploration", "survival"]),
    ("genre:war", "Does it involve war?",
     ["war", "world war", "military", "soldiers", "battle", "holocaust"]),
    ("genre:crime", "Does it involve a crime?",
     ["crime", "murder*", "criminals", "homicide", "serial killer", "theft"]),
    ("genre:poetry", "Is it poetry?", ["poetry", "poems", "verse", "sonnets"]),
    ("genre:drama", "Is it a play or drama?",
     ["drama", "plays", "theater", "theatre", "tragedies", "comedies"]),
    ("genre:comics", "Is it a comic or graphic novel?",
     ["comic*", "graphic novel", "manga", "cartoons"]),
    ("genre:shortstories", "Is it a collection of short stories?",
     ["short stories", "short storie*", "anthology", "collections"]),
    ("genre:humor", "Is it humorous?",
     ["humor*", "humour*", "satire", "comedy", "wit and humor"]),

    # --- genre: non-fiction ---
    ("genre:biography", "Is it a biography or memoir?",
     ["biography", "autobiography", "memoir", "personal narrative", "diaries",
      "correspondence"]),
    ("genre:selfhelp", "Is it a self-help book?",
     ["self-help", "self help", "selfhelp", "self-improvement", "self improvement",
      "self-actualization", "self-realization", "motivation", "success",
      "conduct of life", "personal growth", "habit"]),
    ("genre:business", "Is it about business or money?",
     ["business", "economics", "finance", "management", "entrepreneur",
      "marketing", "investment", "money", "leadership", "wealth"]),
    ("genre:psychology", "Is it about psychology or the mind?",
     ["psychology", "mental health", "consciousness", "emotions", "behavior",
      "cognitive", "psychiatry", "therapy"]),
    ("genre:science", "Is it about science?",
     ["science", "physics", "biology", "chemistry", "mathematics", "astronomy",
      "evolution", "genetics", "medicine", "technology", "computers"]),
    ("genre:history", "Is it a history book?",
     ["history", "historia", "civilization", "ancient", "medieval", "empire",
      "archaeology"]),
    ("genre:politics", "Is it about politics or society?",
     ["politic*", "government", "social science", "sociology", "democracy",
      "race relations", "feminism", "human rights", "law"]),
    ("genre:religion", "Is it a religious or spiritual book?",
     ["religion", "christian", "bible", "god", "spiritual", "islam", "buddhis*",
      "faith", "prayer", "church", "theology", "mysticism"]),
    ("genre:philosophy", "Is it about philosophy?",
     ["philosophy", "ethics", "metaphysics", "existential", "stoic", "logic"]),
    ("genre:health", "Is it about health, fitness or food?",
     ["health", "diet", "nutrition", "fitness", "exercise", "cooking", "recipes",
      "weight loss", "yoga"]),
    ("genre:travel", "Is it about travel or a particular place?",
     ["travel", "voyages", "description and travel", "guidebook"]),
    ("genre:art", "Is it about art, music or design?",
     ["art", "music", "painting", "photography", "design", "architecture",
      "film", "drawing"]),
    ("genre:education", "Is it an educational or teaching book?",
     ["education", "teaching", "study and teaching", "textbook", "学习",
      "학습", "students", "school textbook", "examinations", "readers"]),
    ("genre:nature", "Is it about nature or animals?",
     ["nature", "animals", "environment", "ecology", "gardening", "birds",
      "wildlife", "climate"]),
    ("genre:sports", "Is it about sport or games?",
     ["sports", "football", "baseball", "basketball", "chess", "games"]),

    # --- audience ---
    ("audience:children", "Is it a children's book?",
     ["children", "juvenile", "picture book", "kids", "infantil", "jeunesse",
      "nursery", "toddler", "board book"]),
    ("audience:ya", "Is it aimed at teenagers?",
     ["young adult", "teenage", "teen*", "adolescen*", "high school stories"]),

    # --- setting ---
    ("setting:school", "Does it take place at a school?",
     ["school stories", "boarding school", "college", "university", "campus",
      "students life", "school life"]),
    ("setting:sea", "Does it take place at sea?",
     ["sea stories", "ocean", "sailing", "ships", "pirates", "seafaring",
      "shipwreck", "whaling", "naval"]),
    ("setting:space", "Does it take place in space or on another planet?",
     ["space", "planets", "interstellar", "spacecraft", "mars", "galaxy",
      "astronaut"]),
    ("setting:smalltown", "Does it take place in a small town or the countryside?",
     ["country life", "farm life", "village", "rural", "small town"]),
    ("setting:city", "Does it take place in a big city?",
     ["city and town life", "urban", "new york n y", "london england",
      "paris france", "metropolitan"]),

    # Geography, from the `place` field. It is present on 44% of works and
    # holds 2,071 distinct values, of which a short head does real work:
    # United States, England, London, New York, France, Great Britain.
    # The first version folded all of this into two vague rules and left the
    # rest unused — these are among the sharpest questions available, because
    # "where does it happen" is something a reader always knows.
    ("place:usa", "Does it take place in the United States?",
     ["united states", "america", "new york", "california", "chicago",
      "texas", "boston", "washington", "maine", "florida"]),
    ("place:britain", "Does it take place in Britain?",
     ["england", "london", "great britain", "britain", "scotland", "wales",
      "yorkshire", "cornwall", "oxford", "cambridge"]),
    ("place:france", "Does it take place in France?", ["france", "paris"]),
    ("place:europe_other", "Does it take place in Europe, outside Britain?",
     ["germany", "italy", "spain", "russia", "greece", "ireland", "norway",
      "sweden", "poland", "berlin", "rome", "moscow", "vienna"]),
    ("place:asia", "Does it take place in Asia?",
     ["japan", "china", "india", "korea", "vietnam", "tokyo", "afghanistan",
      "iran", "middle east", "arabia"]),
    ("place:africa", "Does it take place in Africa?",
     ["africa", "nigeria", "egypt", "kenya", "south africa", "congo"]),
    ("place:latam", "Does it take place in Latin America?",
     ["mexico", "brazil", "colombia", "argentina", "chile", "peru",
      "caribbean", "cuba"]),
    ("place:imaginary", "Does it take place somewhere imaginary?",
     ["imaginary places", "fictitious place", "imaginary wars",
      "middle earth", "narnia", "wonderland", "oz"]),

    # --- themes ---
    ("theme:magic", "Is there magic in it?",
     ["magic*", "witch*", "sorcer*", "spells", "supernatural", "enchant*"]),
    ("theme:family", "Is family central to the story?",
     ["family", "mothers", "fathers", "brothers", "sisters", "siblings",
      "parent", "marriage", "married people", "domestic fiction"]),
    ("theme:friendship", "Is friendship central to the story?",
     ["friendship", "friends"]),
    ("theme:comingofage", "Is it a coming-of-age story?",
     ["coming of age", "bildungsroman", "growing up", "adolescence"]),
    ("theme:death", "Does it deal with death or grief?",
     ["death", "grief", "bereavement", "loss", "mourning", "suicide"]),
    ("theme:animals", "Are animals important characters in it?",
     ["dogs", "cats", "horses", "talking animals", "animal stories", "bears",
      "rabbits", "wolves"]),
    ("theme:identity", "Is it about identity or belonging?",
     ["identity", "belonging", "self-perception", "race", "immigrants",
      "cultural conflict"]),
    ("theme:justice", "Is it about justice or injustice?",
     ["justice", "prejudice", "discrimination", "slavery", "oppression",
      "civil rights", "revolution"]),

    # --- form / period markers ---
    ("form:classic", "Is it considered a classic?",
     ["classic", "literature english", "english literature", "american literature",
      "world literature", "canon"]),
    # THE WORDING IS THE WHOLE FIX HERE, and the old one was worse than a
    # missing question. "Is it part of a series?" cannot be answered by the
    # player this game is built for.
    #
    # guessTarget() in book-mind-reader.html says it outright: a series is
    # named when the group holds the belief but no volume does, because
    # "Harry Potter is right for someone thinking of the series AND for
    # someone thinking of any volume". So the intended player may well be
    # holding the whole work in mind — and for them, "is it part of a
    # series?" is honestly answered NO. Harry Potter is not part of a
    # series; it is one.
    #
    # That answer is close to fatal. Every volume answers `present` (0.90),
    # so a "no" multiplies each of them by 0.10 while a rich standalone is
    # multiplied by 0.85 — an 8.5x relative penalty applied to the correct
    # answer, in one question, for answering truthfully. The owner hit it
    # in play and reported the game felt broken by it.
    #
    # The new wording is true for BOTH readings and needs no change to what
    # the matrix stores: a volume and its series both have several books
    # about the same characters, and a standalone has not. Same fact, no
    # ambiguity about which level of the work is being asked about.
    ("form:series", "Are there several books about the same characters or world?",
     ["series", "sequel", "trilogy", "saga"]),
    # Owner's suggestion, from a game where "Is it famous around the
    # world?" was asked of a Chinese web serial. That question is a
    # catalogue artefact — it reads translation and edition counts, which
    # is not something a reader holds in their head — and this is the
    # opposite: anyone who reads web novels knows instantly, and anyone
    # who does not knows just as instantly that theirs is not one.
    #
    # Rare in the corpus by construction, so it lives in FORCE_KEEP.
    #
    # THE CLAIM THAT USED TO BE HERE WAS MEASURED AND IS FALSE. It read: "a
    # yes narrows to a few dozen books at once, which is exactly the shape
    # of question the engine's information gain rewards." Information gain
    # rewards a question that splits the CURRENT BELIEF near half, and it
    # discounts a decisive-but-unlikely answer by exactly how unlikely it
    # is. Scored against the shipped prior this question ranks **42nd of
    # 49** (gain 0.039, splits 25.1%), so the chooser reaches it only after
    # the useful questions are spent.
    #
    # Simulated over all 39 books that answer yes, with the player
    # answering truthfully: it is asked in 22 of 39 games and never before
    # **turn 14**, mostly at turn 19-20 — at the very end of a 20-25
    # question game, far too late to have narrowed anything. The other 17
    # games never reach it. So the question is not unreachable, which was
    # the easy assumption; it is late, which is worse because it looks like
    # it works.
    #
    # Fixing it means changing WHEN it can be asked, not how it is worded —
    # the same unlock-on-condition treatment the character questions get in
    # the endgame. Tracked, not done here.
    ("form:webnovel", "Is it a web novel or light novel?",
     ["web novel", "web serial", "light novel", "webnovel", "webtoon",
      "manhwa", "manhua", "xianxia", "wuxia", "litrpg", "isekai",
      "cultivation novel", "progression fantasy"]),
]

# Compound place names that a keyword match gets WRONG, checked before any
# rule fires. Every one of these was found in the live corpus, not imagined:
#
#   'New England'            matched britain     — it is in Massachusetts
#   'Indiana'                matched asia        — it contains "india"
#   'New Mexico'             matched latam       — it is in the USA
#   'Latin/South America'    matched usa         — "america" is in both
#   'Cambridge Massachusetts' matched britain    — the other Cambridge
#   'Warsaw'                 matched genre:war   — "war" is inside it
#
# Word-boundary matching (below) fixes Indiana and Warsaw. It cannot fix
# 'Latin America' or 'New England', where the misleading token really is a
# whole word, so those need naming. Order matters: first match wins.
_PLACE_OVERRIDES: list[tuple[str, list[str]]] = [
    ("place:latam", ["latin america", "south america", "central america"]),
    ("place:usa", ["new england", "new mexico", "cambridge massachusetts",
                   "massachusetts", "new york", "new orleans", "new jersey",
                   "new hampshire"]),
    ("", ["new south wales", "new zealand", "american samoa"]),  # neither
]


def _compile(keyword: str) -> re.Pattern:
    """Whole word by default; a trailing '*' means 'this is a stem'.

    Naive substring matching is what let "war" match Warsaw and "india"
    match Indiana. Whole-word is the right default, but several rules
    genuinely want a stem — 'politic*' for political/politics, 'sorcer*'
    for sorcery/sorcerer — so the table says which is which instead of the
    matcher guessing.
    """
    if keyword.endswith("*"):
        return re.compile(r"\b" + re.escape(keyword[:-1]), re.I)
    return re.compile(r"\b" + re.escape(keyword) + r"\b", re.I)


# Fast lookup: which rules could a normalized string trigger?
_RULE_INDEX = [(key, [_compile(k) for k in kws]) for key, _q, kws in SUBJECT_RULES]

QUESTION_TEXT: dict[str, str] = {key: q for key, q, _ in SUBJECT_RULES}


def map_subject(normalized: str) -> list[str]:
    """Map one normalized subject to zero or more canonical features.

    Zero is a legitimate and common outcome — most of OL's 8,835 raw
    strings say nothing we ask about. Unmapped is not "no".
    """
    if not normalized or is_stop_subject(normalized):
        return []

    for key, phrases in _PLACE_OVERRIDES:
        if any(p in normalized for p in phrases):
            return [key] if key else []

    return [key for key, patterns in _RULE_INDEX
            if any(p.search(normalized) for p in patterns)]


# ---------------------------------------------------------------------------
# Structural features — facts, not subjects
# ---------------------------------------------------------------------------

# Graded rather than three coarse buckets. Measured on the real corpus:
# `first_publish_year` is present on 99% of works and `number_of_pages_median`
# on 94%, but the first version asked only "before 1900 / before 1950 / last
# 25 years" and "over 400 / under 200" — leaving the best-covered fields in
# the dataset barely used while the game ran short of early questions.
#
# Thresholds sit on the corpus quartiles (years 1911/1969/1999/2014, pages
# 142/224/320/448) so each one splits near the middle of whatever is left.
# They are deliberately correlated: information gain drops a redundant
# question automatically once its neighbour has been answered, so the cost
# of offering both "before 1970" and "before 2000" is nothing, and the
# benefit is a sharp split available at several different points.
#
# WORDING IS OWNER-REVIEWED (2026-08-07). Do not edit these strings to suit
# the data — the review pass exists because a question can have excellent
# coverage and still be unanswerable, and four questions were cut for
# exactly that reason:
#
#   "Has it been printed in many different editions?"  (22% coverage)
#   "Is it freely available to read online?"            (46%)
#   "Do readers generally rate it well?"                (29%)
#   "Did the author die more than a century ago?"       (6%)
#
# All four had good numbers. Nobody picturing a book knows its edition
# count, and its copyright status is a fact about the publisher rather than
# the story. Coverage made them good features; it never made them good
# questions.
STRUCTURAL_QUESTIONS = {
    "fact:veryold": "Was it written before 1900?",
    "fact:old": "Was it written before 1950?",
    "fact:pre1970": "Was it written before 1970?",
    "fact:pre2000": "Was it written before 2000?",
    "fact:recent": "Was it published in the last 25 years?",
    "fact:verrecent": "Was it published in the last 10 years?",
    # PAGE QUESTIONS REMOVED, 2026-08-18, on the owner's argument and the
    # project's own rule. `published_year` in a published page is the
    # EDITION year and the build plan already forbids it becoming a clue.
    # A page count is the same kind of fact and was never held to it: the
    # owner read Lord of the Mysteries as an Arabic PDF running past 2,000
    # pages against 208 for the English volume, so the "length" of a work
    # is a property of the translation somebody happened to hold.
    #
    # It is worse for a series. Asking how many pages Lord of the
    # Mysteries has is asking about an object that does not exist.
    #
    # Measured before removing, and the measurement is why the earlier
    # numbers were misleading. With the simulator's own player, who reads
    # ground truth and therefore always knows a page count, dropping three
    # of these cost 60.0% -> 56.1% (p=0.018, "significant"). Forcing that
    # player to answer `unknown` instead — what a real player does — gave
    # 55.0%, WORSE than removing them. A question the player cannot answer
    # is not neutral: it spends one turn of thirty and multiplies every
    # book by 0.5.
    "fact:famous": "Is it very widely read?",
    "fact:namedchars": "Does it have well-known named characters?",
    # REMOVED with the page questions and for the same reason: it asks
    # about an AGGREGATE the player has no access to. A reader knows
    # whether THEY liked a book; "is it rated 4.2+ by twenty or more
    # people" is a database row. The owner's analogy for the whole family
    # was exact -- it is Akinator asking whether the character appeared on
    # page 54.
    # Was "Has it been translated into many languages?" — the underlying
    # signal is language count, but what a reader can actually answer is
    # whether the book is famous internationally.
    "fact:translated": "Is it famous around the world?",
}


def structural_features(doc: dict, popularity_rank: int, corpus_size: int,
                        webnovel: bool = False) -> dict[str, bool | None]:
    """Facts we can state about a book, with `None` for genuinely unknown.

    `None` matters: a book with no `first_publish_year` must answer
    "was it written before 1950?" with *unknown*, not with *no*.
    """
    year = doc.get("first_publish_year")
    ratings_n = doc.get("ratings_count") or 0
    languages = doc.get("language") or []

    feats: dict[str, bool | None] = {}
    feats["fact:veryold"] = (year < 1900) if year else None
    feats["fact:old"] = (year < 1950) if year else None
    feats["fact:pre1970"] = (year < 1970) if year else None
    feats["fact:pre2000"] = (year < 2000) if year else None
    feats["fact:recent"] = (year >= 2001) if year else None
    feats["fact:verrecent"] = (year >= 2016) if year else None

    # A WEB NOVEL DATES ITSELF, mostly, without anyone finding its year.
    #
    # 33 of the 39 web novels ship with no `first_publish_year` — the wikis
    # record chapters and characters, not publication dates, and a harvest
    # plus a model plus a keyword rule between them recovered 4. So all six
    # of the questions above answered `unknown`, which is not merely a gap:
    # a reader answering "no, not before 2000" gives those books 0.5 where a
    # properly dated book gets 0.85, so being undated actively pushed them
    # DOWN against books that carry a date.
    #
    # Five of the six need no date at all. The form did not exist before the
    # web: the earliest of these with a known year is 2008, and a serialised
    # web novel from before 1900, 1950, 1970 or 2000 is not a thing that can
    # be. Stating that is not a guess — it is the same kind of claim as
    # "a play has no chapters".
    #
    # THE SIXTH IS DIFFERENT AND IS LEFT ALONE unless something dates it,
    # because "in the last 10 years" is exactly what varies across this
    # group. `wiki_created` is the honest source: a wiki is made after the
    # book it is about, so a wiki that predates 2016 proves the book does
    # too. The converse proves nothing — Solo Leveling's wiki is from 2018
    # and the novel from 2014 — so a later wiki leaves the question unknown
    # rather than answering it wrongly. Verified against the three of these
    # whose year we do know: the wiki was never older than the book.
    if webnovel and not year:
        for q in ("fact:veryold", "fact:old", "fact:pre1970", "fact:pre2000"):
            feats[q] = False
        feats["fact:recent"] = True
        wiki = doc.get("wiki_created")
        if isinstance(wiki, int) and wiki < 2016:
            feats["fact:verrecent"] = False

    # See STRUCTURAL_QUESTIONS: page count is an edition fact, not a fact
    # about the work, and is no longer asked.

    # Top decile of a popularity-sorted corpus.
    feats["fact:famous"] = popularity_rank < max(1, corpus_size // 10)
    # "Well-known NAMED characters" — and a name the character questions
    # would refuse to ask about is not one.
    #
    # This was `bool(doc.get("person"))`, which counted any entry at all.
    # The owner played *The Road* and answered "no", correctly: McCarthy's
    # two characters are "the man" and "the boy" and that is the point of
    # the book. Open Library files them under `person`, so the game asserted
    # they were named, scored a firm and correct "no" as a contradiction,
    # and pushed a rank-363 book down for it.
    #
    # The inconsistency was internal, which is why it survived: the endgame
    # already runs those same strings through `usable_token`, which rejects
    # "the", "man" and "boy" — so one half of the engine knew there were no
    # usable names while the other half claimed there were. Same filter now
    # decides both.
    feats["fact:namedchars"] = has_named_characters(doc.get("person") or [])

    # See STRUCTURAL_QUESTIONS: an aggregate rating is not something a
    # player can answer about the book in their head.

    # "English?" is useless — 96% of the corpus is in English. The number of
    # languages a book exists in is a decent proxy for international fame,
    # and fame is the half of it a reader can actually answer. Edition count
    # crosses the same threshold in the data but not in a reader's head, so
    # `ebook_access` and `edition_count` are both read here no longer.
    feats["fact:translated"] = len(languages) >= 5 if languages else None
    return feats


# ---------------------------------------------------------------------------
# Per-book feature extraction
# ---------------------------------------------------------------------------

# A GENRE claim needs more than one stray subject behind it. Moby Dick
# carries 97 raw OL subjects; exactly one of them — the literal string
# "History" — was enough to assert `genre:history: YES`, and exactly one —
# "Science Fiction & Fantasy", a bookstore shelf category, not a
# description of the book — asserted `genre:scifi: YES`. Measured over the
# shipped 5,000: `genre:history` and `genre:romance` are single-subject
# supported in 53% of the books that have them present, `genre:crime` 47%,
# `genre:scifi` 38%, `genre:fantasy` 33%, `genre:horror` 32%. Found by the
# owner playing, the same way the science/scifi and ladder bugs were.
#
# Scoped to `genre:` keys only. `form:fiction`'s single strong signal is
# the subject "Fiction" — that is the correctly-designed case, not the
# bug, and raising the bar there would cost unmeasured coverage for a
# problem that was never shown to exist outside genre classification.
#
# A subject that fails this bar is NOT recorded as absent — that would
# repeat the exact present/absent conflation this project keeps finding.
# It simply does not enter `present`, which is identical to what a book
# with ZERO supporting subjects already gets: the existing
# richness-scaled `absence_confidence()` soft prior, unchanged.
GENRE_MIN_SUPPORT = 2


def extract(doc: dict, popularity_rank: int, corpus_size: int) -> dict:
    """Build one book's feature record.

    Returns `present` (features the metadata supports), `unknown`
    (structural facts we could not determine) and `richness` — how well
    documented the record is, which decides how much weight an *absent*
    feature carries. See `absence_confidence()`.
    """
    raw_subjects = doc.get("subject") or []
    normalized = [normalize(s) for s in raw_subjects]
    content_subjects = [n for n in normalized if n and not is_stop_subject(n)]

    # One independent SOURCE STRING, not one raw subject: two identical
    # strings (OL sometimes lists a subject twice) must not count as two
    # signals, or GENRE_MIN_SUPPORT would be satisfied by a data-entry
    # duplicate instead of genuine corroboration. Deduplicated across BOTH
    # feeds below — subject and place/time — since what matters is how
    # many independent things assert a feature, not which OL field they
    # came from.
    signals: set[str] = set(content_subjects)
    for extra in (doc.get("place") or []) + (doc.get("time") or []):
        # `place`/`time` feed the same setting rules — OL files "Devon
        # (England)" under place, not subject, and it means the same thing.
        n = normalize(extra)
        if n:
            signals.add(n)

    support: dict[str, int] = {}
    for n in signals:
        for f in map_subject(n):
            support[f] = support.get(f, 0) + 1

    present: set[str] = {
        f for f, n in support.items()
        if not f.startswith("genre:") or n >= GENRE_MIN_SUPPORT
    }

    unknown: set[str] = set()
    known_false: set[str] = set()
    # `present` is complete for subject-derived features by this line, which
    # is why the web-novel era rule can be told whether this is one.
    for key, value in structural_features(doc, popularity_rank, corpus_size,
                                          webnovel="form:webnovel" in present).items():
        if value is None:
            unknown.add(key)
        elif value:
            present.add(key)
        else:
            # A computed False, not a missing subject — see
            # KNOWN_FALSE_CONFIDENCE. A book with a publication year is
            # definitively not "before 1900" if that year is 1998.
            known_false.add(key)

    return {
        "key": doc.get("key"),
        "title": doc.get("title") or "",
        "author": (doc.get("author_name") or [""])[0],
        "year": doc.get("first_publish_year"),
        "popularity": doc.get("readinglog_count") or 0,
        "wikidata": (doc.get("id_wikidata") or [None])[0],
        "persons": doc.get("person") or [],
        "present": sorted(present),
        "unknown": sorted(unknown),
        "known_false": sorted(known_false),
        "richness": len(content_subjects),
    }


# Questions that cannot both be true. Answering "yes" to one suppresses its
# siblings outright.
#
# The probability work (known-false at 0.03, per-question base rates for
# unknowns) already pushes a contradicted sibling from 0.45 down to about
# 0.07, and the engine picks whichever question sits closest to half, so it
# would not normally choose one. "Would not normally" is not the standard
# here: the owner hit exactly this playing the game — "is the author
# American?" answered yes, then "is the author from Asia, Africa or Latin
# America?" — and a player who sees that once stops believing the game is
# reading their mind.
#
# So the arithmetic is the fix and this is the guarantee. Cheap, explicit,
# and it cannot drift with the data the way a threshold can.
EXCLUSIVE_GROUPS: list[list[str]] = [
    ["author:american", "author:british", "author:european", "author:nonwestern"],
    ["place:usa", "place:britain", "place:france", "place:europe_other",
     "place:asia", "place:africa", "place:latam", "place:imaginary"],
    ["form:fiction", "form:nonfiction"],
    ["audience:children", "audience:ya"],
]

# question -> the siblings it rules out when answered yes.
EXCLUDES: dict[str, set[str]] = {}
for _group in EXCLUSIVE_GROUPS:
    for _q in _group:
        EXCLUDES[_q] = {x for x in _group if x != _q}


# ---------------------------------------------------------------------------
# Ordered ladders: several questions that are really one number
# ---------------------------------------------------------------------------
# EXCLUSIVE_GROUPS handles alternatives — American OR British. It cannot
# express IMPLICATION, and two families of questions here are pure
# implication because they are thresholds on a single underlying value:
#
#     year  : <1900, <1950, <1970, <2000, >=2001, >=2016
#     pages : <200, <300, >400, >600
#
# The owner played the game and was asked, in one session:
#
#     "Was it published in the last 25 years?"  yes      (so year >= 2001)
#     "Was it published in the last 10 years?"  no       (so year <= 2015)
#     "Was it written before 2000?"                      <- already answered
#
# The third question's answer was determined two turns earlier. The engine
# could not know that: to the matrix these are three unrelated columns, and
# because the belief update is deliberately SOFT (a wrong answer weakens a
# book, never eliminates it) the pre-2000 books still held enough mass
# afterwards that the question kept a positive information gain. Nothing was
# broken; the engine was reasoning correctly from a model that was missing a
# fact.
#
# Declaring the ladders lets the engine hold an interval per dimension and
# skip any question whose answer is already implied by it. Not a heuristic —
# the skipped questions have exactly zero information left to give.
#
# The same table is duplicated in the JavaScript engine. That duplication is
# a known hazard here (it has bitten this project twice), so it is covered by
# the golden-trace parity test: change one side and `node
# games/parity-check.js` fails on the turn where the sequences part.
LADDERS: dict[str, list[tuple[str, str, float]]] = {
    "year": [
        ("fact:veryold", "<", 1900),
        ("fact:old", "<", 1950),
        ("fact:pre1970", "<", 1970),
        ("fact:pre2000", "<", 2000),
        ("fact:recent", ">=", 2001),
        ("fact:verrecent", ">=", 2016),
    ],
    # "pages" ladder removed with the questions it ordered.
}

# question -> (dimension, op, threshold)
LADDER_OF: dict[str, tuple[str, str, float]] = {
    q: (dim, op, thr)
    for dim, rungs in LADDERS.items()
    for q, op, thr in rungs
}


def ladder_narrow(lo: float, hi: float, op: str, thr: float,
                  answer_is_yes: bool) -> tuple[float, float]:
    """Narrow an inclusive [lo, hi] interval by one answered threshold."""
    if op == "<":
        # "is it < thr?"  yes -> value <= thr-1 ; no -> value >= thr
        return (lo, min(hi, thr - 1)) if answer_is_yes else (max(lo, thr), hi)
    if op == ">":
        return (max(lo, thr + 1), hi) if answer_is_yes else (lo, min(hi, thr))
    # ">="
    return (max(lo, thr), hi) if answer_is_yes else (lo, min(hi, thr - 1))


# A rung whose minority side is smaller than this share of the surviving
# interval is treated as settled even though it is not strictly determined.
# 0.0 disables it — the strict behaviour.
#
# WHY A THRESHOLD AND NOT JUST THE STRICT TEST. The owner played a game
# that went: "before 2000?" no, "in the last 10 years?" no — leaving the
# interval [2000, 2015] — and was then asked "in the last 25 years?"
# (>= 2001). Strictly that is undetermined, because the single year 2000
# still answers it "no". Practically it can separate 98 books out of the
# 1,344 in that range, 7.3%, and it cost one of the thirty questions a
# game gets.
#
# The strict test cannot see this: 2001 lies inside [2000, 2015], so the
# rung survives, and the deliberately SOFT belief update then leaves it a
# positive information gain from the pre-2000 mass that never fully died.
# That is the same failure the ladder was built to fix, and this is its
# residue — the year ladder happens to carry two thresholds one year
# apart (`< 2000` and `>= 2001`), so answering either nearly answers both.
#
# Measured from the interval alone, never from the corpus, so both engines
# can compute it identically with nothing extra shipped.
LADDER_DEMINIMIS = 0.0

# `engine.py` seeds unbounded dimensions with ±1e9 rather than infinities.
_UNBOUNDED = 1e8


def _rung_split(op: str, thr: float, lo: float, hi: float) -> tuple[float, float]:
    """Widths of the (yes, no) sides this rung would cut [lo, hi] into."""
    if op == "<":
        yes, no = (lo, thr - 1), (thr, hi)
    elif op == ">":
        yes, no = (thr + 1, hi), (lo, thr)
    else:                                                        # ">="
        yes, no = (thr, hi), (lo, thr - 1)
    return (max(0.0, min(hi, yes[1]) - max(lo, yes[0]) + 1),
            max(0.0, min(hi, no[1]) - max(lo, no[0]) + 1))


def ladder_determined(op: str, thr: float, lo: float, hi: float) -> bool:
    """True when every value in [lo, hi] answers this rung the same way.

    Or near enough — see LADDER_DEMINIMIS, which is a no-op at 0.0.
    """
    if lo > hi:
        return True                      # contradictory answers; stop asking
    if op == "<":
        strict = hi < thr or lo >= thr
    elif op == ">":
        strict = lo > thr or hi <= thr
    else:
        strict = lo >= thr or hi < thr   # ">="
    if strict or not LADDER_DEMINIMIS:
        return strict

    # Only meaningful once BOTH ends are pinned: an open side is not small,
    # and calling it small on a ±1e9 sentinel would retire a rung that still
    # splits most of the corpus.
    if lo <= -_UNBOUNDED or hi >= _UNBOUNDED:
        return False
    w_yes, w_no = _rung_split(op, thr, lo, hi)
    total = w_yes + w_no
    return bool(total) and min(w_yes, w_no) / total < LADDER_DEMINIMIS


# Questions kept regardless of the 5% frequency floor (owner, 2026-08-07).
#
# The floor measures how many books a question applies to. It cannot see how
# CLEANLY a reader can answer it, and these three are unusually clean — a
# reader always knows whether the story happens at a school, whether they
# are holding a play, and whether the book is part of a series.
#
# `form:series` is the important one, and it is a data failure rather than a
# real one: a large share of the corpus genuinely is in a series and Open
# Library simply does not record it. It reads as 4.2% because the metadata
# is missing, not because series books are rare. Fixing the source is
# tracked separately; keeping the question meanwhile costs nothing, because
# a book with no series data answers `unknown` rather than "no".
FORCE_KEEP = {
    "form:series",
    "setting:school",
    "genre:drama",
    # Below the floor by construction — the corpus is a general catalogue
    # and web novels are a sliver of it. Kept because the floor measures
    # how MANY books a question applies to and cannot see how cleanly a
    # reader answers it, which is the same reason the three above are here.
    "form:webnovel",
}


def absence_confidence(richness: int) -> float:
    """P(yes | book, feature) when the book does NOT carry the feature.

    NOT zero, and this is the single most important number in the file.
    Open Library metadata is incomplete, so "no `sea stories` subject" is
    evidence against a sea story, not proof of its absence. Reading it as
    proof would eliminate the correct book with confidence — the failure
    this game cannot afford.

    How strong the evidence is scales with how well documented the record
    is: a book with 60 subjects that still lacks `sea stories` probably
    isn't one; a book with 2 subjects tells us almost nothing, so its
    answer sits near the uninformative 0.5.
    """
    if richness <= 2:
        return 0.45          # essentially uninformative
    if richness <= 6:
        return 0.35
    if richness <= 15:
        return 0.25
    return 0.15              # richly documented: absence really does mean something


PRESENCE_CONFIDENCE = 0.90   # a subject IS stated: strong but never certain
UNKNOWN_CONFIDENCE = 0.50    # we genuinely cannot say

# `known_false` — a fact we positively computed as FALSE, which is not the
# same thing as a subject the record happens not to mention. The set is
# still built (it is real information and `extract()` records it), but
# **the engine deliberately scores it as ordinary absence**, so there is no
# separate confidence constant here.
#
# That is a measured decision, not an oversight. It was introduced to fix
# the owner's report of "Is the author American? yes" being followed by "Is
# the author from Asia, Africa, or Latin America?" — the reasoning being
# that Wikidata saying American means certainly-not-African, while
# `absence_confidence()` is calibrated for subjects, where a gap is weak
# evidence. Sound reasoning; scoring it at 0.03 measured **41.7% against a
# 63.3% baseline** at 5,000 books, because at that value a single
# disagreeing answer very nearly eliminates the correct book, and players
# disagree with library metadata about a quarter of the time.
#
# What actually fixed the contradiction was EXCLUSIVE_GROUPS above. Left
# computed rather than deleted because a future use may want it — but if
# you reintroduce a probability for it, measure first. Full account in
# [[2026-08-07 — Akinator phase 4, and four measurement traps]].
