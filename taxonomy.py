"""
taxonomy.py — the fixed category taxonomy for BookHub.

WE define these categories (not Gemini, not Google Books). Gemini's only job
is to pick 1-3 slugs FROM THIS LIST for each summarized book; anything it
returns outside the list is discarded by validate_categories().

INVARIANT: the frontend keeps a mirror of this list in _data/categories.yml
(bookhub repo) for the /categories pages. Change both files together.

Gemini-free module by design — safe to import anywhere (like book_data.py).
"""

CATEGORIES: list[dict] = [
    {"slug": "fantasy",                 "name": "Fantasy"},
    {"slug": "progression-fantasy",     "name": "Progression Fantasy & Cultivation"},
    {"slug": "science-fiction",         "name": "Science Fiction"},
    {"slug": "mystery-thriller",        "name": "Mystery & Thriller"},
    {"slug": "horror",                  "name": "Horror"},
    {"slug": "romance",                 "name": "Romance"},
    {"slug": "historical-fiction",      "name": "Historical Fiction"},
    {"slug": "classics",                "name": "Classics & Literary Fiction"},
    {"slug": "light-novels",            "name": "Light Novels"},
    {"slug": "web-novels",              "name": "Web Novels"},
    {"slug": "manga-graphic-novels",    "name": "Manga & Graphic Novels"},
    {"slug": "young-adult",             "name": "Young Adult"},
    {"slug": "adventure",               "name": "Adventure"},
    {"slug": "magic-supernatural",      "name": "Magic & Supernatural"},
    {"slug": "self-help",               "name": "Self-Help & Productivity"},
    {"slug": "business-finance",        "name": "Business & Finance"},
    {"slug": "psychology",              "name": "Psychology"},
    {"slug": "philosophy",              "name": "Philosophy"},
    {"slug": "history",                 "name": "History"},
    {"slug": "biography-memoir",        "name": "Biography & Memoir"},
    {"slug": "science-nature",          "name": "Science & Nature"},
    {"slug": "religion-spirituality",   "name": "Religion & Spirituality"},
]

CATEGORY_SLUGS: set = {c["slug"] for c in CATEGORIES}

MAX_CATEGORIES_PER_BOOK = 3


def validate_categories(slugs: list) -> list[str]:
    """Filters to known slugs, dedupes preserving order, caps the count."""
    out: list[str] = []
    seen: set = set()
    for s in slugs or []:
        if not isinstance(s, str):
            continue
        s = s.strip().lower()
        if s in CATEGORY_SLUGS and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= MAX_CATEGORIES_PER_BOOK:
            break
    return out


# Keyword → slug map applied to Google Books category strings when Gemini
# fails or returns nothing valid. First match wins; scanned in order.
_FALLBACK_KEYWORDS: list[tuple] = [
    ("comic", "manga-graphic-novels"),
    ("graphic novel", "manga-graphic-novels"),
    ("manga", "manga-graphic-novels"),
    ("juvenile", "young-adult"),
    ("young adult", "young-adult"),
    ("self-help", "self-help"),
    ("business", "business-finance"),
    ("economic", "business-finance"),
    ("psychology", "psychology"),
    ("philosophy", "philosophy"),
    ("religion", "religion-spirituality"),
    ("biography", "biography-memoir"),
    ("history", "history"),
    ("science fiction", "science-fiction"),
    ("fantasy", "fantasy"),
    ("horror", "horror"),
    ("romance", "romance"),
    ("thriller", "mystery-thriller"),
    ("mystery", "mystery-thriller"),
    ("detective", "mystery-thriller"),
    ("adventure", "adventure"),
    ("science", "science-nature"),
    ("nature", "science-nature"),
    ("literary", "classics"),
]

DEFAULT_CATEGORY = "classics"


def fallback_category(google_category: str = "") -> str:
    """Maps a raw Google Books category string to one of OUR slugs."""
    gc = (google_category or "").lower()
    for keyword, slug in _FALLBACK_KEYWORDS:
        if keyword in gc:
            return slug
    return DEFAULT_CATEGORY


def prompt_list() -> str:
    """The 'slug: name' list injected into the Gemini categorization prompt."""
    return "\n".join(f"- {c['slug']}: {c['name']}" for c in CATEGORIES)
