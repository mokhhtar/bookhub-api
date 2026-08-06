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
_TRAILING_QUALIFIERS = re.compile(
    r",?\s*(fiction|general|etc\.?|juvenile|nonfiction|"
    r"history and criticism|criticism and interpretation)\s*$",
    re.I,
)

_PUNCT = re.compile(r"[\"'“”‘’\(\)\[\]{}.:;!?]+")
_WS = re.compile(r"\s+")


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
    # Applied repeatedly: "adventure and adventurers, fiction, general".
    for _ in range(3):
        new = _TRAILING_QUALIFIERS.sub("", s).strip(" ,-")
        if new == s:
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
    # --- genre: fiction ---
    ("genre:fantasy", "Is it a fantasy story?",
     ["fantasy", "wizard", "dragon", "elves", "sword and sorcery", "mythical"]),
    ("genre:scifi", "Is it science fiction?",
     ["science fiction", "sciencefiction", "dystopi", "space opera", "time travel",
      "cyberpunk", "steampunk", "extraterrestrial"]),
    ("genre:mystery", "Is it a mystery or detective story?",
     ["mystery", "detective", "whodunit", "private investigator", "sleuth"]),
    ("genre:thriller", "Is it a thriller or suspense story?",
     ["thriller", "suspense", "spies", "espionage", "conspiracy"]),
    ("genre:horror", "Is it a horror story?",
     ["horror", "ghost", "vampire", "haunted", "zombie", "monsters", "occult"]),
    ("genre:romance", "Is it a romance?",
     ["romance", "love stor", "romantic", "courtship", "enemies to lovers",
      "forbidden love", "first love"]),
    ("genre:historicalfic", "Is it historical fiction?",
     ["historical fiction", "fiction historical", "historical romance"]),
    ("genre:adventure", "Is it an adventure story?",
     ["adventure", "quest", "treasure", "exploration", "survival"]),
    ("genre:war", "Does it involve war?",
     ["war", "world war", "military", "soldiers", "battle", "holocaust"]),
    ("genre:crime", "Does it involve a crime?",
     ["crime", "murder", "criminals", "homicide", "serial killer", "theft"]),
    ("genre:poetry", "Is it poetry?", ["poetry", "poems", "verse", "sonnets"]),
    ("genre:drama", "Is it a play or drama?",
     ["drama", "plays", "theater", "theatre", "tragedies", "comedies"]),
    ("genre:comics", "Is it a comic or graphic novel?",
     ["comic", "graphic novel", "manga", "cartoons"]),
    ("genre:shortstories", "Is it a collection of short stories?",
     ["short stories", "short storie", "anthology", "collections"]),
    ("genre:humor", "Is it humorous?",
     ["humor", "humour", "satire", "comedy", "wit and humor"]),

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
     ["politic", "government", "social science", "sociology", "democracy",
      "race relations", "feminism", "human rights", "law"]),
    ("genre:religion", "Is it a religious or spiritual book?",
     ["religion", "christian", "bible", "god", "spiritual", "islam", "buddhis",
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
     ["young adult", "teenage", "teen", "adolescen", "high school stories"]),

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
    ("place:europe_other", "Does it take place elsewhere in Europe?",
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
     ["magic", "witch", "sorcer", "spells", "supernatural", "enchant"]),
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
    ("form:series", "Is it part of a series?",
     ["series", "sequel", "trilogy", "saga"]),
]

# Fast lookup: which rules could a normalized string trigger?
_RULE_INDEX = [(key, kws) for key, _q, kws in SUBJECT_RULES]

QUESTION_TEXT: dict[str, str] = {key: q for key, q, _ in SUBJECT_RULES}


def map_subject(normalized: str) -> list[str]:
    """Map one normalized subject to zero or more canonical features.

    Zero is a legitimate and common outcome — most of OL's 8,835 raw
    strings say nothing we ask about. Unmapped is not "no".
    """
    if not normalized or is_stop_subject(normalized):
        return []
    hits = []
    for key, keywords in _RULE_INDEX:
        if any(kw in normalized for kw in keywords):
            hits.append(key)
    return hits


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
STRUCTURAL_QUESTIONS = {
    "fact:veryold": "Was it written before 1900?",
    "fact:old": "Was it written before 1950?",
    "fact:pre1970": "Was it written before 1970?",
    "fact:pre2000": "Was it written before 2000?",
    "fact:recent": "Was it published in the last 25 years?",
    "fact:verrecent": "Was it published in the last 10 years?",
    "fact:short": "Is it a short book (under 200 pages)?",
    "fact:midshort": "Is it under 300 pages?",
    "fact:long": "Is it a long book (over 400 pages)?",
    "fact:verylong": "Is it over 600 pages?",
    "fact:famous": "Is it very widely read?",
    "fact:freeebook": "Is it freely available to read online?",
    "fact:namedchars": "Does it have well-known named characters?",
    "fact:highlyrated": "Is it very highly rated by readers?",
    "fact:wellrated": "Do readers generally rate it well?",
    "fact:translated": "Has it been translated into many languages?",
    "fact:manyeditions": "Has it been printed in many different editions?",
}


def structural_features(doc: dict, popularity_rank: int, corpus_size: int) -> dict[str, bool | None]:
    """Facts we can state about a book, with `None` for genuinely unknown.

    `None` matters: a book with no `first_publish_year` must answer
    "was it written before 1950?" with *unknown*, not with *no*.
    """
    year = doc.get("first_publish_year")
    pages = doc.get("number_of_pages_median")
    ratings_n = doc.get("ratings_count") or 0
    ratings_avg = doc.get("ratings_average")
    languages = doc.get("language") or []
    editions = doc.get("edition_count") or 0

    feats: dict[str, bool | None] = {}
    feats["fact:veryold"] = (year < 1900) if year else None
    feats["fact:old"] = (year < 1950) if year else None
    feats["fact:pre1970"] = (year < 1970) if year else None
    feats["fact:pre2000"] = (year < 2000) if year else None
    feats["fact:recent"] = (year >= 2001) if year else None
    feats["fact:verrecent"] = (year >= 2016) if year else None

    feats["fact:short"] = (pages < 200) if pages else None
    feats["fact:midshort"] = (pages < 300) if pages else None
    feats["fact:long"] = (pages > 400) if pages else None
    feats["fact:verylong"] = (pages > 600) if pages else None

    # Top decile of a popularity-sorted corpus.
    feats["fact:famous"] = popularity_rank < max(1, corpus_size // 10)
    feats["fact:freeebook"] = (doc.get("ebook_access") in ("public", "borrowable"))
    feats["fact:namedchars"] = bool(doc.get("person"))

    feats["fact:highlyrated"] = (ratings_avg >= 4.2) if (ratings_avg and ratings_n >= 20) else None
    feats["fact:wellrated"] = (ratings_avg >= 3.9) if (ratings_avg and ratings_n >= 20) else None

    # "English?" is useless — 96% of the corpus is in English. How WIDELY
    # translated it is, though, splits near the middle and is a question a
    # player can actually answer about a book they know.
    feats["fact:translated"] = len(languages) >= 5 if languages else None
    feats["fact:manyeditions"] = editions >= 50 if editions else None
    return feats


# ---------------------------------------------------------------------------
# Per-book feature extraction
# ---------------------------------------------------------------------------

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

    present: set[str] = set()
    for n in content_subjects:
        present.update(map_subject(n))

    # `place` and `time` feed the same setting rules — OL files "Devon
    # (England)" under place, not subject, and it means the same thing.
    for extra in (doc.get("place") or []) + (doc.get("time") or []):
        present.update(map_subject(normalize(extra)))

    unknown: set[str] = set()
    for key, value in structural_features(doc, popularity_rank, corpus_size).items():
        if value is None:
            unknown.add(key)
        elif value:
            present.add(key)

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
        "richness": len(content_subjects),
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
