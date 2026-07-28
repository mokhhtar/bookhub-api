"""
tools/gtb_pool.py — the hand-verified catalog behind "Guess the Book",
Litheca's daily knowledge game.

Same role (and the same trust model) as tools/fandom_catalog.py: a curated
config module, not a resolver. Nothing here is fetched, inferred or
generated at runtime — every field is either checked mechanically against
the real Project Gutenberg text by scripts/verify_gtb_pool.py, or written
by hand and reviewed by eye before commit.

WHY A HAND-VERIFIED POOL AT ALL (the trap that made it necessary):
`published_year` in the site's own _books/*.md front matter is the EDITION
year, not the work's. `_books/moby-dick.md` says "2009". A clue reading
"First published: 2009" for Moby-Dick is exactly the wrong data the
grounding rule forbids, so publication years — and every other factual
clue — come from `first_published` here, never from book_data.

Which fields are verified how:
  gutenberg_id / gutenberg_title / author   — asserted against the "Title:"
      and "Author:" lines of the fetched Gutenberg header. A silent
      renumbering upstream fails the check loudly instead of quietly
      grounding a puzzle in the wrong book.
  characters / setting_anchor               — asserted to appear VERBATIM in
      the stripped text (case-insensitive). A character clue is therefore
      grounded in the book itself, not in the curator's memory.
  first_published / language / nationality /
  author_years / setting                    — hand-written, eye-reviewed.
      The verifier cross-checks first_published against Open Library's
      first_publish_year and REPORTS disagreement; Open Library is noisy
      (it indexes reprints as works), so it is a second opinion, never the
      source of truth. Any disagreement is resolved by a human, and if it
      cannot be resolved the year clue is dropped rather than guessed.

Catalog scope: public-domain classics with full Gutenberg text. Modern
bestsellers have no verifiable source text, so they cannot be grounded and
are excluded by design — see the catalog-limit note in the vault's
"Knowledge games" file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from slug import author_slug, book_slug

# Words that carry no identifying power on their own, so they are dropped
# when the title is expanded into single-word giveaway terms. (The full
# title PHRASE is always banned regardless — see banned_terms.)
_TITLE_STOPWORDS = {
    "a", "an", "and", "of", "or", "the", "in", "on", "at", "to", "de",
    "strange", "case", "adventures", "picture", "tale", "two", "under",
    "around", "world", "days", "eighty", "twenty", "thousand", "little",
    "great", "call", "wild", "war", "worlds", "time", "machine", "heart",
    "sea", "wonderful", "crime", "punishment", "peace", "expectations",
    "letter", "scarlet", "women", "count", "island", "treasure", "pride",
    "prejudice", "leagues", "darkness",
}


@dataclass(frozen=True)
class PoolEntry:
    """One book eligible to become a daily puzzle."""

    gutenberg_id: int
    title: str                 # the answer as players see it in the picker
    author: str
    first_published: int       # the WORK's year — never an edition/printing
    language: str              # language of composition (may differ from the text we quote)
    nationality: str           # for the "author initials" clue
    author_years: str          # "1819–1891"
    setting: str               # one plain factual sentence — the easiest clue
    setting_anchor: str        # a word from `setting` that must appear in the text
    # Verbatim in the text. [0] feeds the character clue, so it must share NO
    # word with the title — "Alice" would hand over Alice's Adventures in
    # Wonderland, "Emma" would hand over Emma. Enforced by the verifier.
    characters: tuple[str, ...]
    gutenberg_title: str       # exact "Title:" header value, pinned for verification
    # Proper nouns that would hand the answer over if they turned up in a
    # quote (ships, houses, places, surnames). Union'd with the automatic
    # bans in banned_terms(); over-banning is cheap, under-banning ruins a
    # puzzle, so err long.
    giveaway_terms: tuple[str, ...] = field(default_factory=tuple)
    translator: str | None = None  # set when we quote a translation, credited on the clue
    # Pinned only where Gutenberg's spelling differs from the name we display
    # (transliterations: Gutenberg says "Dostoyevsky", modern usage says
    # "Dostoevsky"). Left None, the verifier just requires our surname to
    # appear in Gutenberg's author line.
    gutenberg_author: str | None = None

    @property
    def canonical_id(self) -> str:
        """Matches tools/summary.py's _canonical_id_from — base-title slug +
        author slug — so a puzzle's answer keys to the same identity the rest
        of the site already uses for that book."""
        base = self.title.split(":")[0].strip()
        return f"{book_slug(base)}-{author_slug(self.author)}"

    @property
    def author_initials(self) -> str:
        """'Herman Melville' → 'H. M.' — the author clue must narrow without naming."""
        parts = [p for p in re.split(r"[\s.]+", self.author) if p]
        return " ".join(f"{p[0].upper()}." for p in parts)

    def banned_terms(self) -> set[str]:
        """Everything a candidate quote must NOT contain, lowercased.

        Mechanical, not model judgement: a quote that names the book, its
        author or its cast is not a clue, it is the answer. The generator
        drops such candidates outright rather than asking Gemini to be tasteful.
        """
        terms: set[str] = set()

        for title in (self.title, self.gutenberg_title):
            terms.add(title.lower())
            # Same title minus a leading article — "the-sea-wolf" also hides
            # behind "Sea-Wolf" in the prose.
            terms.add(re.sub(r"^(the|a|an)\s+", "", title.lower()))
            for word in re.split(r"[^a-z0-9']+", title.lower()):
                if len(word) >= 4 and word not in _TITLE_STOPWORDS:
                    terms.add(word)

        terms.add(self.author.lower())
        for part in self.author.split():
            if len(part) >= 4:
                terms.add(part.lower())

        for name in self.characters:
            terms.add(name.lower())
            for part in name.split():
                # "Mr."/"Dr." carry nothing; first names and surnames do.
                if len(part) >= 4 and part.lower() not in {"lord", "lady", "aunt", "miss"}:
                    terms.add(part.lower())

        terms.update(t.lower() for t in self.giveaway_terms)
        return {t for t in terms if len(t) >= 3}


# ── The pool ─────────────────────────────────────────────────
# Ordered roughly by how widely read the book is, which is also the order a
# reviewer wants to read them in. Deliberate same-author pairs (Austen,
# Dickens, Twain, Stevenson, Wells, London, Verne) give the "same author —
# wrong book" feedback signal something real to say.

GTB_POOL: list[PoolEntry] = [
    PoolEntry(
        gutenberg_id=2489,
        title="Moby-Dick",
        author="Herman Melville",
        first_published=1851,
        language="English",
        nationality="American",
        author_years="1819–1891",
        setting="A whaling voyage that sails out of Nantucket.",
        setting_anchor="Nantucket",
        characters=("Ishmael", "Queequeg", "Starbuck", "Ahab"),
        gutenberg_title="Moby Dick; Or, The Whale",
        giveaway_terms=("Pequod", "Moby", "whale", "whaling", "whaleman"),
    ),
    PoolEntry(
        gutenberg_id=1342,
        title="Pride and Prejudice",
        author="Jane Austen",
        first_published=1813,
        language="English",
        nationality="English",
        author_years="1775–1817",
        setting="A family of five unmarried sisters in rural Hertfordshire.",
        setting_anchor="Hertfordshire",
        characters=("Elizabeth Bennet", "Mr. Darcy", "Jane Bennet", "Mr. Bingley"),
        gutenberg_title="Pride and Prejudice",
        giveaway_terms=("Netherfield", "Pemberley", "Longbourn", "Meryton", "Lydia",
                        "Wickham", "Collins", "Lucas"),
    ),
    PoolEntry(
        gutenberg_id=158,
        title="Emma",
        author="Jane Austen",
        first_published=1815,
        language="English",
        nationality="English",
        author_years="1775–1817",
        setting="A wealthy young woman plays matchmaker in the village of Highbury.",
        setting_anchor="Highbury",
        characters=("Mr. Knightley", "Harriet Smith", "Mr. Elton", "Emma Woodhouse"),
        gutenberg_title="Emma",
        giveaway_terms=("Highbury", "Hartfield", "Donwell", "Randalls", "Weston",
                        "Churchill", "Fairfax", "Bates"),
    ),
    PoolEntry(
        gutenberg_id=84,
        title="Frankenstein",
        author="Mary Shelley",
        first_published=1818,
        language="English",
        nationality="English",
        author_years="1797–1851",
        setting="A student's laboratory in Ingolstadt, and a pursuit across the Arctic ice.",
        setting_anchor="Ingolstadt",
        # "Victor Frankenstein" as a phrase is NOT in the text — he is "Victor"
        # to his family and "Frankenstein" to everyone else. Verified, not assumed.
        characters=("Victor", "Robert Walton", "Henry Clerval", "Elizabeth"),
        gutenberg_title="Frankenstein; Or, The Modern Prometheus",
        giveaway_terms=("Ingolstadt", "Prometheus", "Geneva", "Justine", "daemon"),
    ),
    PoolEntry(
        gutenberg_id=345,
        title="Dracula",
        author="Bram Stoker",
        first_published=1897,
        language="English",
        nationality="Irish",
        author_years="1847–1912",
        setting="A journey from a castle in Transylvania to the English coast at Whitby.",
        setting_anchor="Whitby",
        characters=("Jonathan Harker", "Mina", "Van Helsing", "Lucy"),
        gutenberg_title="Dracula",
        giveaway_terms=("Transylvania", "Whitby", "Carfax", "Renfield", "Borgo",
                        "vampire", "Un-Dead", "Seward"),
    ),
    PoolEntry(
        gutenberg_id=11,
        title="Alice's Adventures in Wonderland",
        author="Lewis Carroll",
        first_published=1865,
        language="English",
        nationality="English",
        author_years="1832–1898",
        setting="A girl falls down a rabbit-hole into a kingdom of playing cards.",
        setting_anchor="rabbit-hole",
        # Not "the White Rabbit": adding White Fang to the pool made that clue
        # point at two books at once. The verifier catches this collision.
        characters=("the Cheshire Cat", "the Mock Turtle", "the White Rabbit", "Alice"),
        gutenberg_title="Alice's Adventures in Wonderland",
        giveaway_terms=("Wonderland", "Cheshire", "Hatter", "Dormouse", "Gryphon",
                        "Duchess", "Caterpillar"),
    ),
    PoolEntry(
        gutenberg_id=1661,
        title="The Adventures of Sherlock Holmes",
        author="Arthur Conan Doyle",
        first_published=1892,
        language="English",
        nationality="Scottish",
        author_years="1859–1930",
        setting="Twelve cases solved from a lodging in Baker Street, London.",
        setting_anchor="Baker Street",
        characters=("Dr. Watson", "Mrs. Hudson", "Irene Adler", "Sherlock Holmes"),
        gutenberg_title="The Adventures of Sherlock Holmes",
        giveaway_terms=("Baker Street", "Lestrade", "Scotland Yard", "Bohemia"),
    ),
    PoolEntry(
        gutenberg_id=1260,
        title="Jane Eyre",
        author="Charlotte Brontë",
        first_published=1847,
        language="English",
        nationality="English",
        author_years="1816–1855",
        setting="A governess arrives at a country house called Thornfield Hall.",
        setting_anchor="Thornfield",
        characters=("Mr. Rochester", "Mrs. Fairfax", "Adèle", "Jane Eyre"),
        gutenberg_title="Jane Eyre: An Autobiography",
        giveaway_terms=("Thornfield", "Lowood", "Gateshead", "Rochester", "Reed",
                        "Brocklehurst", "Ferndean"),
    ),
    PoolEntry(
        gutenberg_id=768,
        title="Wuthering Heights",
        author="Emily Brontë",
        first_published=1847,
        language="English",
        nationality="English",
        author_years="1818–1848",
        setting="Two households on the Yorkshire moors, ruined across two generations.",
        setting_anchor="moors",
        characters=("Heathcliff", "Catherine", "Mr. Lockwood", "Nelly Dean"),
        gutenberg_title="Wuthering Heights",
        giveaway_terms=("Wuthering", "Thrushcross", "Grange", "Earnshaw", "Linton",
                        "Hindley", "Joseph"),
    ),
    PoolEntry(
        gutenberg_id=174,
        title="The Picture of Dorian Gray",
        author="Oscar Wilde",
        first_published=1890,
        language="English",
        nationality="Irish",
        author_years="1854–1900",
        setting="A young man in London stays beautiful while his portrait ages.",
        setting_anchor="portrait",
        characters=("Lord Henry", "Basil Hallward", "Sibyl Vane", "Dorian Gray"),
        gutenberg_title="The Picture of Dorian Gray",
        giveaway_terms=("Hallward", "Wotton", "Sibyl", "portrait", "Selby"),
    ),
    PoolEntry(
        gutenberg_id=98,
        title="A Tale of Two Cities",
        author="Charles Dickens",
        first_published=1859,
        language="English",
        nationality="English",
        author_years="1812–1870",
        setting="London and Paris on either side of the French Revolution.",
        setting_anchor="Paris",
        characters=("Sydney Carton", "Charles Darnay", "Lucie", "Doctor Manette"),
        gutenberg_title="A Tale of Two Cities",
        giveaway_terms=("Darnay", "Manette", "Carton", "Defarge", "Bastille",
                        "Guillotine", "Saint Antoine"),
    ),
    PoolEntry(
        gutenberg_id=1400,
        title="Great Expectations",
        author="Charles Dickens",
        first_published=1861,
        language="English",
        nationality="English",
        author_years="1812–1870",
        setting="An orphaned blacksmith's boy comes into money from an unknown benefactor.",
        setting_anchor="forge",
        characters=("Pip", "Miss Havisham", "Estella", "Joe Gargery"),
        gutenberg_title="Great Expectations",
        giveaway_terms=("Havisham", "Estella", "Gargery", "Magwitch", "Jaggers",
                        "Satis House", "Wemmick", "Pumblechook"),
    ),
    PoolEntry(
        gutenberg_id=2554,
        title="Crime and Punishment",
        author="Fyodor Dostoevsky",
        first_published=1866,
        language="Russian",
        nationality="Russian",
        author_years="1821–1881",
        setting="A destitute former student murders a pawnbroker in St Petersburg.",
        setting_anchor="Petersburg",
        characters=("Raskolnikov", "Sonia", "Razumihin", "Porfiry"),
        gutenberg_title="Crime and Punishment",
        gutenberg_author="Fyodor Dostoyevsky",  # Gutenberg's transliteration
        translator="Constance Garnett",
        giveaway_terms=("Raskolnikov", "Petersburg", "Marmeladov", "Svidrigailov",
                        "Dounia", "Pulcheria", "pawnbroker"),
    ),
    PoolEntry(
        gutenberg_id=1399,
        title="Anna Karenina",
        author="Leo Tolstoy",
        first_published=1878,
        language="Russian",
        nationality="Russian",
        author_years="1828–1910",
        setting="A married woman's affair with a cavalry officer in imperial Russia.",
        setting_anchor="Moscow",
        characters=("Vronsky", "Levin", "Kitty", "Anna"),
        gutenberg_title="Anna Karenina",
        translator="Constance Garnett",
        giveaway_terms=("Karenin", "Vronsky", "Oblonsky", "Stepan Arkadyevitch",
                        "Shtcherbatsky", "Petersburg"),
    ),
    PoolEntry(
        gutenberg_id=76,
        title="Adventures of Huckleberry Finn",
        author="Mark Twain",
        first_published=1884,
        language="English",
        nationality="American",
        author_years="1835–1910",
        setting="A boy and an escaping slave raft down the Mississippi.",
        setting_anchor="raft",
        # "Tom Sawyer" is in this book, but it is ANOTHER pool title — using it
        # as the character clue would point players at the wrong puzzle answer.
        characters=("Jim", "the Widow Douglas", "Huck", "Tom Sawyer"),
        gutenberg_title="Adventures of Huckleberry Finn",
        giveaway_terms=("Huckleberry", "Mississippi", "Widow Douglas", "Pap",
                        "Grangerford", "Phelps"),
    ),
    PoolEntry(
        gutenberg_id=74,
        title="The Adventures of Tom Sawyer",
        author="Mark Twain",
        first_published=1876,
        language="English",
        nationality="American",
        author_years="1835–1910",
        setting="A mischievous boy in a Missouri village on the Mississippi.",
        setting_anchor="village",
        characters=("Becky Thatcher", "Aunt Polly", "Tom Sawyer", "Huckleberry Finn"),
        gutenberg_title="The Adventures of Tom Sawyer, Complete",
        giveaway_terms=("Thatcher", "Polly", "Injun Joe", "Petersburg", "Sid",
                        "whitewash", "Mississippi"),
    ),
    PoolEntry(
        gutenberg_id=120,
        title="Treasure Island",
        author="Robert Louis Stevenson",
        first_published=1883,
        language="English",
        nationality="Scottish",
        author_years="1850–1894",
        setting="A boy finds a map and sails with a crew of disguised pirates.",
        setting_anchor="map",
        characters=("Jim Hawkins", "Long John Silver", "Doctor Livesey", "Squire Trelawney"),
        gutenberg_title="Treasure Island",
        giveaway_terms=("Hispaniola", "Hawkins", "Silver", "Livesey", "Trelawney",
                        "Flint", "Benbow", "treasure", "buccaneer"),
    ),
    PoolEntry(
        gutenberg_id=43,
        title="Strange Case of Dr Jekyll and Mr Hyde",
        author="Robert Louis Stevenson",
        first_published=1886,
        language="English",
        nationality="Scottish",
        author_years="1850–1894",
        setting="A London lawyer investigates his friend's sinister associate.",
        setting_anchor="London",
        characters=("Mr. Utterson", "Dr. Jekyll", "Mr. Hyde", "Dr. Lanyon"),
        gutenberg_title="The Strange Case of Dr. Jekyll and Mr. Hyde",
        giveaway_terms=("Utterson", "Lanyon", "Poole", "Enfield", "Soho", "draught"),
    ),
    PoolEntry(
        gutenberg_id=35,
        title="The Time Machine",
        author="H. G. Wells",
        first_published=1895,
        language="English",
        nationality="English",
        author_years="1866–1946",
        # The famous "802,701" appears NOWHERE in Gutenberg #35 — not in
        # digits, not spelled out. Every clue has to survive the text, not the
        # book's reputation, so the setting was rewritten around the White
        # Sphinx, which is verifiably there.
        setting="An inventor travels into the far future and wakes beside a great White Sphinx.",
        setting_anchor="White Sphinx",
        characters=("Weena", "Filby", "the Medical Man", "the Time Traveller"),
        gutenberg_title="The Time Machine",
        giveaway_terms=("Morlock", "Eloi", "Weena", "Time Traveller", "Sphinx",
                        "Fourth Dimension"),
    ),
    PoolEntry(
        gutenberg_id=36,
        title="The War of the Worlds",
        author="H. G. Wells",
        first_published=1898,
        language="English",
        nationality="English",
        author_years="1866–1946",
        setting="Cylinders fall on Horsell Common and an invasion moves on London.",
        setting_anchor="Horsell",
        characters=("Ogilvy", "the artilleryman", "the curate", "my brother"),
        gutenberg_title="The War of the Worlds",
        giveaway_terms=("Martian", "Horsell", "Woking", "cylinder", "Heat-Ray",
                        "tripod", "Ogilvy"),
    ),
    PoolEntry(
        gutenberg_id=219,
        title="Heart of Darkness",
        author="Joseph Conrad",
        first_published=1899,
        language="English",
        nationality="Polish-British",
        author_years="1857–1924",
        setting="A steamboat journey up an African river to reach an ivory agent.",
        setting_anchor="ivory",
        characters=("Marlow", "Kurtz", "the manager", "the harlequin"),
        gutenberg_title="Heart of Darkness",
        giveaway_terms=("Marlow", "Kurtz", "Congo", "ivory", "Nellie", "pilgrims"),
    ),
    PoolEntry(
        gutenberg_id=215,
        title="The Call of the Wild",
        author="Jack London",
        first_published=1903,
        language="English",
        nationality="American",
        author_years="1876–1916",
        setting="A stolen sled dog is driven north into the Klondike gold rush.",
        setting_anchor="Klondike",
        characters=("Buck", "John Thornton", "Spitz", "Curly"),
        gutenberg_title="The Call of the Wild",
        giveaway_terms=("Klondike", "Thornton", "Spitz", "Yukon", "Dawson",
                        "sled", "traces", "husky"),
    ),
    PoolEntry(
        gutenberg_id=1074,
        title="The Sea-Wolf",
        author="Jack London",
        first_published=1904,
        language="English",
        nationality="American",
        author_years="1876–1916",
        setting="A shipwrecked gentleman is pressed into the crew of a sealing schooner.",
        setting_anchor="schooner",
        characters=("Humphrey Van Weyden", "Wolf Larsen", "Maud Brewster", "Thomas Mugridge"),
        gutenberg_title="The Sea-Wolf",
        giveaway_terms=("Larsen", "Ghost", "Van Weyden", "Mugridge", "Brewster",
                        "sealing", "schooner"),
    ),
    PoolEntry(
        gutenberg_id=5200,
        title="Metamorphosis",
        author="Franz Kafka",
        first_published=1915,
        language="German",
        nationality="Austrian-Czech",
        author_years="1883–1924",
        setting="A travelling salesman wakes transformed into an insect in his own bedroom.",
        setting_anchor="bedroom",
        characters=("Gregor Samsa", "Grete", "the chief clerk", "his father"),
        gutenberg_title="Metamorphosis",
        translator="David Wyllie",
        giveaway_terms=("Samsa", "Gregor", "Grete", "vermin", "insect"),
    ),
    PoolEntry(
        gutenberg_id=1184,
        title="The Count of Monte Cristo",
        author="Alexandre Dumas",
        first_published=1844,
        language="French",
        nationality="French",
        author_years="1802–1870",
        setting="A sailor is imprisoned in an island fortress and returns immensely rich.",
        setting_anchor="Marseilles",
        characters=("Edmond Dantès", "Abbé Faria", "Mercédès", "Danglars"),
        gutenberg_title="The Count of Monte Cristo",
        giveaway_terms=("Dantes", "Dantès", "Monte Cristo", "Faria", "Mercedes",
                        "Villefort", "Morrel", "Chateau d'If", "Marseilles"),
    ),
    PoolEntry(
        gutenberg_id=135,
        title="Les Misérables",
        author="Victor Hugo",
        first_published=1862,
        language="French",
        nationality="French",
        author_years="1802–1885",
        setting="An ex-convict builds a new life while a police inspector hunts him.",
        setting_anchor="convict",
        characters=("Jean Valjean", "Javert", "Cosette", "Marius"),
        gutenberg_title="Les Misérables",
        translator="Isabel Florence Hapgood",
        giveaway_terms=("Valjean", "Javert", "Cosette", "Fantine", "Thénardier",
                        "Thenardier", "Gavroche", "Marius", "Digne"),
    ),
    PoolEntry(
        gutenberg_id=514,
        title="Little Women",
        author="Louisa May Alcott",
        first_published=1868,
        language="English",
        nationality="American",
        author_years="1832–1888",
        setting="Four sisters grow up in New England while their father is away at war.",
        setting_anchor="sisters",
        characters=("Jo", "Meg", "Beth", "Amy", "Laurie"),
        gutenberg_title="Little Women",
        giveaway_terms=("March", "Laurie", "Laurence", "Hannah", "Marmee", "Plumfield"),
    ),
    PoolEntry(
        gutenberg_id=164,
        title="Twenty Thousand Leagues Under the Sea",
        author="Jules Verne",
        first_published=1870,
        language="French",
        nationality="French",
        author_years="1828–1905",
        setting="Three castaways live aboard a submarine commanded by an exile.",
        setting_anchor="submarine",
        characters=("Captain Nemo", "Professor Aronnax", "Ned Land", "Conseil"),
        gutenberg_title="Twenty Thousand Leagues under the Sea",
        giveaway_terms=("Nautilus", "Nemo", "Aronnax", "Conseil", "Ned Land",
                        "submarine", "narwhal"),
    ),
    PoolEntry(
        gutenberg_id=103,
        title="Around the World in Eighty Days",
        author="Jules Verne",
        first_published=1873,
        language="French",
        nationality="French",
        author_years="1828–1905",
        setting="An English gentleman bets his fortune on a race against the clock.",
        setting_anchor="wager",
        characters=("Phileas Fogg", "Passepartout", "Aouda", "Detective Fix"),
        gutenberg_title="Around the World in Eighty Days",
        translator="George M. Towle",
        giveaway_terms=("Fogg", "Passepartout", "Aouda", "Reform Club", "wager"),
    ),
    PoolEntry(
        gutenberg_id=64317,
        title="The Great Gatsby",
        author="F. Scott Fitzgerald",
        first_published=1925,
        language="English",
        nationality="American",
        author_years="1896–1940",
        setting="A summer of parties on Long Island, told by the neighbour next door.",
        setting_anchor="Long Island",
        # "Nick Carraway" never appears as a phrase in the novel — the narrator
        # is "Nick" in dialogue and "the Carraways" as a family.
        characters=("Nick", "Daisy", "Tom Buchanan", "Jay Gatsby"),
        gutenberg_title="The Great Gatsby",
        giveaway_terms=("Gatsby", "Carraway", "Buchanan", "West Egg", "East Egg",
                        "Wilson", "Jordan Baker"),
    ),
    PoolEntry(
        gutenberg_id=25344,
        title="The Scarlet Letter",
        author="Nathaniel Hawthorne",
        first_published=1850,
        language="English",
        nationality="American",
        author_years="1804–1864",
        setting="A woman in Puritan Boston is sentenced to wear a mark of adultery.",
        setting_anchor="Puritan",
        characters=("Hester Prynne", "Pearl", "Arthur Dimmesdale", "Roger Chillingworth"),
        gutenberg_title="The Scarlet Letter",
        giveaway_terms=("Hester", "Prynne", "Dimmesdale", "Chillingworth", "Pearl",
                        "scaffold", "Puritan"),
    ),
    PoolEntry(
        gutenberg_id=55,
        title="The Wonderful Wizard of Oz",
        author="L. Frank Baum",
        first_published=1900,
        language="English",
        nationality="American",
        author_years="1856–1919",
        setting="A cyclone carries a Kansas farm girl to a country of witches.",
        setting_anchor="cyclone",
        characters=("Dorothy", "the Scarecrow", "the Tin Woodman", "the Cowardly Lion"),
        gutenberg_title="The Wonderful Wizard of Oz",
        giveaway_terms=("Dorothy", "Scarecrow", "Woodman", "Munchkin", "Emerald City",
                        "Kansas", "Toto", "Winkie"),
    ),
    # ── second batch: added to give a 30-day bank headroom (a failed book
    # spends its slot, so 30 days needs meaningfully more than 30 books). ──
    PoolEntry(
        gutenberg_id=16,
        title="Peter Pan",
        author="J. M. Barrie",
        first_published=1911,
        language="English",
        nationality="Scottish",
        author_years="1860–1937",
        setting="A boy who refuses to grow up flies three children out of a London nursery.",
        setting_anchor="nursery",
        characters=("Wendy", "Captain Hook", "Tinker Bell", "Mr. Darling"),
        gutenberg_title="Peter Pan",
        giveaway_terms=("Neverland", "Wendy", "Hook", "Tinker Bell", "Darling", "Nana",
                        "Tootles", "fairy"),
    ),
    PoolEntry(
        gutenberg_id=521,
        title="Robinson Crusoe",
        author="Daniel Defoe",
        first_published=1719,
        language="English",
        nationality="English",
        author_years="1660–1731",
        # "castaway" isn't a word Defoe uses — the anchor has to survive the
        # actual text, not sound right.
        setting="A sailor is wrecked alone on an island and stays there for years.",
        setting_anchor="island",
        characters=("Friday", "Xury", "the Spaniard"),
        # This Gutenberg file predates the metadata header entirely — the
        # verifier falls back to checking the title page. See its note.
        gutenberg_title="",
        giveaway_terms=("Crusoe", "Robinson", "Friday", "Xury", "island", "shipwreck",
                        "savages", "cannibals"),
    ),
    PoolEntry(
        gutenberg_id=46,
        title="A Christmas Carol",
        author="Charles Dickens",
        first_published=1843,
        language="English",
        nationality="English",
        author_years="1812–1870",
        setting="A miser in a London counting-house is visited by three spirits in one night.",
        setting_anchor="counting-house",
        characters=("Bob Cratchit", "Tiny Tim", "Marley", "Fezziwig"),
        gutenberg_title="A Christmas Carol in Prose; Being a Ghost Story of Christmas",
        giveaway_terms=("Scrooge", "Marley", "Cratchit", "Fezziwig", "humbug", "Christmas"),
    ),
    PoolEntry(
        gutenberg_id=2852,
        title="The Hound of the Baskervilles",
        author="Arthur Conan Doyle",
        first_published=1902,
        language="English",
        nationality="Scottish",
        author_years="1859–1930",
        setting="A family curse and a great black dog on the Devon moors.",
        setting_anchor="moor",
        characters=("Sir Henry", "Stapleton", "Dr. Mortimer", "Barrymore"),
        gutenberg_title="The Hound of the Baskervilles",
        giveaway_terms=("Baskerville", "Holmes", "Watson", "Stapleton", "Dartmoor",
                        "Grimpen", "moor", "Devonshire"),
    ),
    PoolEntry(
        gutenberg_id=113,
        title="The Secret Garden",
        author="Frances Hodgson Burnett",
        first_published=1911,
        language="English",
        nationality="English-American",
        author_years="1849–1924",
        setting="An orphan sent to a Yorkshire manor finds a door nobody has opened in ten years.",
        setting_anchor="Yorkshire",
        characters=("Dickon", "Martha", "Colin", "Mary Lennox"),
        gutenberg_title="The Secret Garden",
        giveaway_terms=("Misselthwaite", "Lennox", "Dickon", "Craven", "moor", "robin",
                        "Mistress Mary"),
    ),
    PoolEntry(
        gutenberg_id=45,
        title="Anne of Green Gables",
        author="Lucy Maud Montgomery",
        first_published=1908,
        language="English",
        nationality="Canadian",
        author_years="1874–1942",
        setting="An orphan girl arrives by mistake at a farm near Avonlea.",
        setting_anchor="Avonlea",
        characters=("Marilla", "Matthew", "Diana", "Mrs. Rachel Lynde"),
        gutenberg_title="",   # headerless file — verified from the title page
        giveaway_terms=("Avonlea", "Marilla", "Cuthbert", "Matthew", "Diana Barry",
                        "Prince Edward Island", "orphan"),
    ),
    PoolEntry(
        gutenberg_id=236,
        title="The Jungle Book",
        author="Rudyard Kipling",
        first_published=1894,
        language="English",
        nationality="English",
        author_years="1865–1936",
        setting="A boy raised by wolves in the Seeonee hills.",
        setting_anchor="Seeonee",
        characters=("Baloo", "Bagheera", "Shere Khan", "Mowgli"),
        gutenberg_title="The Jungle Book",
        giveaway_terms=("Mowgli", "Baloo", "Bagheera", "Shere Khan", "Seeonee", "Akela",
                        "jungle", "Man-cub", "wolves"),
    ),
    PoolEntry(
        gutenberg_id=5230,
        title="The Invisible Man",
        author="H. G. Wells",
        first_published=1897,
        language="English",
        nationality="English",
        author_years="1866–1946",
        setting="A stranger swathed in bandages takes a room at a village inn.",
        setting_anchor="bandages",
        characters=("Kemp", "Griffin", "Marvel"),
        gutenberg_title="The Invisible Man: A Grotesque Romance",
        giveaway_terms=("Griffin", "Kemp", "Iping", "bandages", "Marvel", "Burdock"),
    ),
    PoolEntry(
        gutenberg_id=910,
        title="White Fang",
        author="Jack London",
        first_published=1906,
        language="English",
        nationality="American",
        author_years="1876–1916",
        setting="A wolf-dog born in the Wild passes from master to master.",
        setting_anchor="the Wild",
        characters=("Grey Beaver", "Weedon Scott", "Beauty Smith"),
        gutenberg_title="",   # headerless file — verified from the title page
        giveaway_terms=("Grey Beaver", "Weedon", "Beauty Smith", "Klondike", "Yukon",
                        "wolf", "cub", "she-wolf", "sled"),
    ),
    PoolEntry(
        gutenberg_id=2413,
        title="Madame Bovary",
        author="Gustave Flaubert",
        first_published=1856,
        language="French",
        nationality="French",
        author_years="1821–1880",
        setting="A country doctor's wife in Yonville ruins herself chasing the life she has read about.",
        setting_anchor="Yonville",
        characters=("Homais", "Rodolphe", "Charles", "Emma"),
        gutenberg_title="Madame Bovary",
        translator="Eleanor Marx Aveling",
        giveaway_terms=("Bovary", "Rodolphe", "Homais", "Yonville", "Tostes", "Rouen",
                        "Leon", "Charbovari"),
    ),
    PoolEntry(
        gutenberg_id=1257,
        title="The Three Musketeers",
        author="Alexandre Dumas",
        first_published=1844,
        language="French",
        nationality="French",
        author_years="1802–1870",
        setting="A young Gascon comes to Paris to join the king's guards.",
        setting_anchor="Gascon",
        characters=("Athos", "Porthos", "Aramis"),
        gutenberg_title="The three musketeers",
        # "Artagnan" without the apostrophe on purpose: this text spells it
        # D’Artagnan with a typographic apostrophe, so the bare surname is the
        # form that reliably matches.
        giveaway_terms=("Athos", "Porthos", "Aramis", "Artagnan", "musketeer", "Richelieu",
                        "Treville", "Gascon", "cardinal"),
    ),
    PoolEntry(
        gutenberg_id=110,
        title="Tess of the d'Urbervilles",
        author="Thomas Hardy",
        first_published=1891,
        language="English",
        nationality="English",
        author_years="1840–1928",
        setting="A village girl from Marlott is sent to claim kin with a rich family.",
        setting_anchor="Marlott",
        characters=("Angel Clare", "Alec", "Tess"),
        gutenberg_title="Tess of the d'Urbervilles: A Pure Woman",
        giveaway_terms=("Durbeyfield", "Urberville", "Marlott", "Talbothays", "Wessex",
                        "Angel", "Clare", "dairymaid"),
    ),
    PoolEntry(
        gutenberg_id=145,
        title="Middlemarch",
        author="George Eliot",
        first_published=1871,
        language="English",
        nationality="English",
        author_years="1819–1880",
        setting="Two unhappy marriages in a provincial English town.",
        setting_anchor="provincial",
        characters=("Dorothea", "Lydgate", "Casaubon", "Rosamond"),
        gutenberg_title="Middlemarch",
        giveaway_terms=("Dorothea", "Casaubon", "Lydgate", "Rosamond", "Bulstrode",
                        "Brooke", "Ladislaw", "Tipton"),
    ),
    PoolEntry(
        gutenberg_id=209,
        title="The Turn of the Screw",
        author="Henry James",
        first_published=1898,
        language="English",
        nationality="American",
        author_years="1843–1916",
        setting="A governess at a country house called Bly becomes certain the children see ghosts.",
        setting_anchor="Bly",
        characters=("Mrs. Grose", "Miles", "Flora"),
        gutenberg_title="The Turn of the Screw",
        giveaway_terms=("Grose", "Quint", "Jessel", "governess", "Harley Street"),
    ),
    PoolEntry(
        gutenberg_id=203,
        title="Uncle Tom's Cabin",
        author="Harriet Beecher Stowe",
        first_published=1852,
        language="English",
        nationality="American",
        author_years="1811–1896",
        setting="An enslaved man is sold away from a Kentucky plantation.",
        setting_anchor="plantation",
        characters=("Eliza", "Legree", "Shelby", "Eva"),
        gutenberg_title="Uncle Tom's Cabin",
        giveaway_terms=("Legree", "Shelby", "Eliza", "Topsy", "Ophelia", "slave",
                        "slavery", "plantation", "Kentucky", "master"),
    ),
    PoolEntry(
        gutenberg_id=105,
        title="Persuasion",
        author="Jane Austen",
        first_published=1817,
        language="English",
        nationality="English",
        author_years="1775–1817",
        setting="A family leaves Kellynch Hall for Bath, and a refused suitor returns.",
        setting_anchor="Kellynch",
        characters=("Captain Wentworth", "Lady Russell", "Anne Elliot"),
        gutenberg_title="Persuasion",
        giveaway_terms=("Kellynch", "Wentworth", "Elliot", "Uppercross", "Musgrove",
                        "Bath", "baronet"),
    ),
    # ── third batch: the 30-day bank spent 30 of the first 48, and a
    # 180-day cooldown needs a pool far larger than the cooldown to keep
    # running. Each book here was probed against the real Gutenberg text
    # before being written — names that weren't there were replaced, not
    # guessed (Journey to the Centre's "Axel" and War and Peace's "Natasha"
    # are absent from these particular translations). ──
    PoolEntry(
        gutenberg_id=161,
        title="Sense and Sensibility",
        author="Jane Austen",
        first_published=1811,
        language="English",
        nationality="English",
        author_years="1775–1817",
        setting="Two sisters lose their home and must marry on a reduced income.",
        setting_anchor="Devonshire",
        characters=("Colonel Brandon", "Elinor", "Marianne", "Willoughby"),
        gutenberg_title="Sense and Sensibility",
        giveaway_terms=("Dashwood", "Elinor", "Marianne", "Willoughby", "Norland",
                        "Barton", "Ferrars"),
    ),
    PoolEntry(
        gutenberg_id=730,
        title="Oliver Twist",
        author="Charles Dickens",
        first_published=1838,
        language="English",
        nationality="English",
        author_years="1812–1870",
        setting="A workhouse orphan falls in with a gang of child thieves in London.",
        setting_anchor="workhouse",
        characters=("Fagin", "the Artful Dodger", "Bill Sikes", "Nancy"),
        gutenberg_title="Oliver Twist",
        giveaway_terms=("Fagin", "Sikes", "Dodger", "Bumble", "Brownlow", "workhouse",
                        "parish boy"),
    ),
    PoolEntry(
        gutenberg_id=766,
        title="David Copperfield",
        author="Charles Dickens",
        first_published=1850,
        language="English",
        nationality="English",
        author_years="1812–1870",
        setting="A boy's life told from birth, through a cruel stepfather and a Yarmouth boathouse.",
        setting_anchor="Yarmouth",
        characters=("Mr. Micawber", "Peggotty", "Uriah Heep", "Betsey Trotwood"),
        gutenberg_title="David Copperfield",
        giveaway_terms=("Copperfield", "Micawber", "Peggotty", "Heep", "Steerforth",
                        "Trotwood", "Murdstone", "Yarmouth"),
    ),
    PoolEntry(
        gutenberg_id=421,
        title="Kidnapped",
        author="Robert Louis Stevenson",
        first_published=1886,
        language="English",
        nationality="Scottish",
        author_years="1850–1894",
        setting="A boy is sold to a ship's captain and flees across the Highlands.",
        setting_anchor="Highlands",
        characters=("Alan Breck", "Ebenezer", "David Balfour"),
        gutenberg_title="Kidnapped",
        giveaway_terms=("Balfour", "Ebenezer", "Alan Breck", "Appin", "Shaws",
                        "Highlands", "brig"),
    ),
    PoolEntry(
        gutenberg_id=86,
        title="A Connecticut Yankee in King Arthur's Court",
        author="Mark Twain",
        first_published=1889,
        language="English",
        nationality="American",
        author_years="1835–1910",
        setting="An engineer wakes in the sixth century and sets about industrialising it.",
        setting_anchor="sixth century",
        characters=("Merlin", "Sandy", "Clarence"),
        gutenberg_title="A Connecticut Yankee in King Arthur's Court",
        giveaway_terms=("Merlin", "Camelot", "Arthur", "Round Table", "Yankee",
                        "Hartford", "knight-errantry"),
    ),
    PoolEntry(
        gutenberg_id=1268,
        title="The Mysterious Island",
        author="Jules Verne",
        first_published=1875,
        language="French",
        nationality="French",
        author_years="1828–1905",
        setting="Five escapees from a siege come down by balloon on an uncharted shore.",
        setting_anchor="balloon",
        characters=("Cyrus Harding", "Pencroft", "Herbert", "Neb"),
        gutenberg_title="The Mysterious Island",
        translator="Agnes Kinloch Kingston",
        giveaway_terms=("Cyrus Harding", "Pencroft", "Ayrton", "Lincoln Island",
                        "castaways", "balloon"),
    ),
    PoolEntry(
        gutenberg_id=18857,
        title="A Journey to the Centre of the Earth",
        author="Jules Verne",
        first_published=1864,
        language="French",
        nationality="French",
        author_years="1828–1905",
        setting="A professor and his nephew climb down an Icelandic volcano.",
        setting_anchor="Iceland",
        # This translation renames the narrator and the professor, so the
        # familiar "Axel" and "Lidenbrock" appear nowhere in it.
        characters=("Hans", "the Professor"),
        gutenberg_title="A Journey to the Centre of the Earth",
        giveaway_terms=("Iceland", "Sneffels", "crater", "volcano", "Hans",
                        "subterranean"),
    ),
    PoolEntry(
        gutenberg_id=28054,
        title="The Brothers Karamazov",
        author="Fyodor Dostoevsky",
        first_published=1880,
        language="Russian",
        nationality="Russian",
        author_years="1821–1881",
        setting="Three brothers and the murder of their father in a provincial town.",
        setting_anchor="monastery",
        characters=("Alyosha", "Dmitri", "Smerdyakov", "Grushenka"),
        gutenberg_title="The Brothers Karamazov",
        gutenberg_author="Fyodor Dostoyevsky",
        translator="Constance Garnett",
        giveaway_terms=("Karamazov", "Alyosha", "Dmitri", "Ivan", "Smerdyakov",
                        "Grushenka", "Zossima", "parricide"),
    ),
    PoolEntry(
        gutenberg_id=2600,
        title="War and Peace",
        author="Leo Tolstoy",
        first_published=1869,
        language="Russian",
        nationality="Russian",
        author_years="1828–1910",
        setting="Five aristocratic families through Napoleon's invasion of Russia.",
        setting_anchor="Moscow",
        # "Natasha" is spelled Natásha in the Maude translation and so never
        # matches a plain search; Pierre does.
        characters=("Pierre", "Prince Andrew", "Napoleon"),
        gutenberg_title="War and Peace",
        gutenberg_author="graf Leo Tolstoy",
        translator="Aylmer Maude",
        giveaway_terms=("Bezukhov", "Bolkonski", "Rostov", "Napoleon", "Borodino",
                        "Moscow", "Kutuzov", "Pierre"),
    ),
    PoolEntry(
        gutenberg_id=107,
        title="Far from the Madding Crowd",
        author="Thomas Hardy",
        first_published=1874,
        language="English",
        nationality="English",
        author_years="1840–1928",
        setting="A woman inherits a farm and is courted by a shepherd, a soldier and a landowner.",
        setting_anchor="Wessex",
        characters=("Gabriel Oak", "Bathsheba", "Sergeant Troy", "Boldwood"),
        gutenberg_title="Far from the Madding Crowd",
        giveaway_terms=("Bathsheba", "Everdene", "Gabriel Oak", "Boldwood", "Troy",
                        "Weatherbury", "Wessex", "shepherd"),
    ),
    PoolEntry(
        gutenberg_id=550,
        title="Silas Marner",
        author="George Eliot",
        first_published=1861,
        language="English",
        nationality="English",
        author_years="1819–1880",
        setting="A miserly weaver loses his gold and raises a foundling instead.",
        setting_anchor="Raveloe",
        characters=("Eppie", "Godfrey", "Dolly Winthrop"),
        gutenberg_title="Silas Marner",
        giveaway_terms=("Marner", "Raveloe", "Eppie", "Godfrey", "Cass", "weaver",
                        "Lantern Yard"),
    ),
    PoolEntry(
        gutenberg_id=599,
        title="Vanity Fair",
        author="William Makepeace Thackeray",
        first_published=1848,
        language="English",
        nationality="English",
        author_years="1811–1863",
        setting="A penniless schemer claws her way up English society around Waterloo.",
        setting_anchor="Waterloo",
        characters=("Becky Sharp", "Amelia Sedley", "Rawdon", "Dobbin"),
        gutenberg_title="Vanity Fair",
        giveaway_terms=("Becky Sharp", "Sedley", "Crawley", "Rawdon", "Dobbin",
                        "Osborne", "Waterloo", "Vanity"),
    ),
    PoolEntry(
        gutenberg_id=2833,
        title="The Portrait of a Lady",
        author="Henry James",
        first_published=1881,
        language="English",
        nationality="American",
        author_years="1843–1916",
        setting="A young American heiress in Europe chooses badly among her suitors.",
        setting_anchor="Rome",
        characters=("Isabel Archer", "Ralph Touchett", "Gilbert Osmond", "Madame Merle"),
        # Gutenberg splits this work; volume 1 is the source text.
        gutenberg_title="The Portrait of a Lady — Volume 1",
        giveaway_terms=("Isabel Archer", "Touchett", "Osmond", "Merle", "Gardencourt",
                        "Warburton", "heiress"),
    ),
    PoolEntry(
        gutenberg_id=73,
        title="The Red Badge of Courage",
        author="Stephen Crane",
        first_published=1895,
        language="English",
        nationality="American",
        author_years="1871–1900",
        setting="A young soldier runs from his first battle and returns to face a second.",
        setting_anchor="regiment",
        characters=("Henry Fleming", "Wilson", "the tattered man"),
        gutenberg_title="The Red Badge of Courage: An Episode of the American Civil War",
        giveaway_terms=("Fleming", "Civil War", "regiment", "the youth", "Union",
                        "Confederate", "colours"),
    ),
    PoolEntry(
        gutenberg_id=289,
        title="The Wind in the Willows",
        author="Kenneth Grahame",
        first_published=1908,
        language="English",
        nationality="Scottish",
        author_years="1859–1932",
        setting="Riverbank animals, a stolen motor-car and a hall to be recaptured.",
        setting_anchor="river bank",
        characters=("Mr. Toad", "Ratty", "Badger", "Mole"),
        gutenberg_title="The Wind in the Willows",
        giveaway_terms=("Toad", "Ratty", "Badger", "Mole", "Toad Hall", "Wild Wood",
                        "river bank", "weasels"),
    ),
    PoolEntry(
        gutenberg_id=271,
        title="Black Beauty",
        author="Anna Sewell",
        first_published=1877,
        language="English",
        nationality="English",
        author_years="1820–1878",
        setting="A horse tells his own life through a succession of owners.",
        setting_anchor="stable",
        characters=("Ginger", "Merrylegs", "John Manly", "Squire Gordon"),
        gutenberg_title="Black Beauty",
        giveaway_terms=("Ginger", "Merrylegs", "Birtwick", "bearing rein", "cab",
                        "stable", "colt", "harness"),
    ),
    PoolEntry(
        gutenberg_id=175,
        title="The Phantom of the Opera",
        author="Gaston Leroux",
        first_published=1910,
        language="French",
        nationality="French",
        author_years="1868–1927",
        setting="A masked figure in the cellars beneath a Paris opera house.",
        setting_anchor="cellars",
        characters=("Christine", "Raoul", "the Persian", "Erik"),
        gutenberg_title="The Phantom of the Opera",
        giveaway_terms=("Christine", "Raoul", "Erik", "Opera", "chandelier", "cellars",
                        "Daae", "phantom", "ghost"),
    ),
    PoolEntry(
        gutenberg_id=19942,
        title="Candide",
        author="Voltaire",
        first_published=1759,
        language="French",
        nationality="French",
        author_years="1694–1778",
        setting="A young optimist is thrown out of a castle in Westphalia and sees the world.",
        setting_anchor="Westphalia",
        characters=("Pangloss", "Cunegonde", "Martin"),
        gutenberg_title="Candide",
        giveaway_terms=("Pangloss", "Cunegonde", "Westphalia", "optimism", "Thunder",
                        "best of all possible worlds"),
    ),
    PoolEntry(
        gutenberg_id=82,
        title="Ivanhoe",
        author="Walter Scott",
        first_published=1819,
        language="English",
        nationality="Scottish",
        author_years="1771–1832",
        setting="A disinherited knight returns to Norman England and fights a tournament.",
        setting_anchor="tournament",
        characters=("Cedric", "Gurth", "Wamba", "Rebecca"),
        gutenberg_title="Ivanhoe: A Romance",
        giveaway_terms=("Ivanhoe", "Cedric", "Rotherwood", "Templar", "Saxon", "Norman",
                        "tournament", "Rebecca"),
    ),
    PoolEntry(
        gutenberg_id=940,
        title="The Last of the Mohicans",
        author="James Fenimore Cooper",
        first_published=1826,
        language="English",
        nationality="American",
        author_years="1789–1851",
        setting="A scout guides two sisters through forest and ambush during a frontier war.",
        setting_anchor="forest",
        characters=("Chingachgook", "Hawkeye", "Uncas", "Magua"),
        gutenberg_title="The Last of the Mohicans; A narrative of 1757",
        giveaway_terms=("Chingachgook", "Hawkeye", "Uncas", "Magua", "Mohican",
                        "Huron", "Delaware", "scout"),
    ),
    PoolEntry(
        gutenberg_id=12,
        title="Through the Looking-Glass",
        author="Lewis Carroll",
        first_published=1871,
        language="English",
        nationality="English",
        author_years="1832–1898",
        setting="A girl climbs through a mirror into a country laid out as a chessboard.",
        setting_anchor="chess",
        characters=("Humpty Dumpty", "Tweedledum", "the Red Queen", "the White Knight"),
        gutenberg_title="Through the Looking-Glass",
        giveaway_terms=("Looking-Glass", "Humpty", "Tweedledum", "Tweedledee",
                        "Jabberwocky", "chessboard", "Red Queen"),
    ),
]


def by_gutenberg_id(gid: int) -> PoolEntry | None:
    return next((e for e in GTB_POOL if e.gutenberg_id == gid), None)


def by_canonical_id(canonical_id: str) -> PoolEntry | None:
    return next((e for e in GTB_POOL if e.canonical_id == canonical_id), None)
