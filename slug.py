"""
slug.py — URL slug generation shared by the summary tool and github_publisher.

Rules: NFKD-normalize → ASCII-fold → lowercase → every non-[a-z0-9] run
becomes a single hyphen → strip/collapse hyphens → truncate at 80 chars on a
hyphen boundary. Full title (subtitle included) — richer for SEO.
"""
import re
import unicodedata

MAX_SLUG_LEN = 80


def slugify(text: str) -> str:
    if not text:
        return ""
    # NFKD + ASCII-fold drops accents; non-Latin chars (CJK, Arabic) fall away —
    # for a fully non-Latin title this can yield "", callers must handle that.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(text) > MAX_SLUG_LEN:
        text = text[:MAX_SLUG_LEN].rsplit("-", 1)[0]
    return text


def book_slug(title: str) -> str:
    return slugify(title)


def author_slug(author: str) -> str:
    """First author only — 'Miya Kazuki, You Shiina' → 'miya-kazuki'."""
    first = (author or "").split(",")[0].strip()
    return slugify(first)


def character_slug(name: str) -> str:
    """Character names are single entities — no author-style comma splitting
    ('Klein Moretti' and even 'Ahab, Captain of the Pequod' stay whole)."""
    return slugify(name)
