"""
tools/fandom_catalog.py — Structured Fandom series catalog.

Provides reliable volume indexes and chapter lists for series whose Fandom wikis
have known (or discoverable) layout patterns.  Pilot series: Shadow Slave, COTE.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("bookhub-api.tools.fandom_catalog")

HEADERS = {"User-Agent": "BookHub/1.0 (mokhhtar@github.com)"}

# ── Data models ──────────────────────────────────────────────


class StructureType(str, Enum):
    DEDICATED_PAGES = "dedicated_pages"       # one wiki page per volume (prefix scan)
    WIKILINK_LIST = "wikilink_list"           # master page with [[Light Novel Volume N]] links
    NOVEL_MASTER_PAGE = "novel_master_page"   # single page with per-volume chapter tables
    MASTER_SECTIONS = "master_sections"       # master page with == Volume N == sections


@dataclass
class FandomSeriesConfig:
    series_key: str
    subdomain: str
    series_display_name: str
    author: str
    aliases: list[str] = field(default_factory=list)
    cover_url: Optional[str] = None

    structure_type: StructureType = StructureType.DEDICATED_PAGES
    volumes_master_page: Optional[str] = None
    page_prefix: Optional[str] = None
    page_pattern: Optional[str] = None          # regex for valid volume page titles
    exclude_page_patterns: list[str] = field(default_factory=list)

    # Chapters
    novel_master_page: Optional[str] = None     # e.g. "Shadow Slave (Novel)"
    chapters_wikitext_section: Optional[str] = None  # e.g. "List of Chapters"

    # Series isolation (spin-offs on same subdomain)
    series_filter: Optional[str] = None
    exclude_series_patterns: list[str] = field(default_factory=list)


@dataclass
class FandomVolume:
    volume_number: float
    wiki_page: str
    display_title: str
    series_key: str
    subtitle: Optional[str] = None
    cover_url: Optional[str] = None
    synopsis: Optional[str] = None
    chapter_start: Optional[int] = None
    chapter_end: Optional[int] = None

    def to_search_dict(self, author: str, fallback_cover: Optional[str] = None) -> dict:
        return {
            "title": self.display_title,
            "author": author,
            "cover_url": self.cover_url or fallback_cover,
            "isbn_10": None,
            "isbn_13": None,
            "published_year": None,
            "google_id": None,
            "openlibrary_id": None,
            "bookwyrm_id": None,
            "source": "fandom",
            "fandom_series_key": self.series_key,
            "fandom_wiki_page": self.wiki_page,
            "fandom_volume_number": self.volume_number,
        }


# ── Series registry (pilot) ──────────────────────────────────

CATALOG_SERIES: dict[str, FandomSeriesConfig] = {
    "shadowslave": FandomSeriesConfig(
        series_key="shadowslave",
        subdomain="shadowslave",
        series_display_name="Shadow Slave",
        author="Guiltythree",
        aliases=["shadow slave"],
        cover_url="https://covers.openlibrary.org/b/id/15173101-M.jpg",
        structure_type=StructureType.DEDICATED_PAGES,
        page_prefix="Volume",
        page_pattern=r"^Volume\s+\d+(?:\.\d+)?(?::\s*.+)?$",
        exclude_page_patterns=[
            r"mini arc", r"summary", r"/", r"untitled",
        ],
        novel_master_page="Shadow Slave (Novel)",
    ),
    "you-zitsu": FandomSeriesConfig(
        series_key="you-zitsu",
        subdomain="you-zitsu",
        series_display_name="Classroom of the Elite",
        author="Shōgo Kinugasa",
        aliases=["classroom of the elite", "you-zitsu", "cote"],
        cover_url="https://covers.openlibrary.org/b/id/10166148-M.jpg",
        structure_type=StructureType.WIKILINK_LIST,
        volumes_master_page="List of Volumes",
        page_prefix="Light Novel Volume",
        page_pattern=r"^Light Novel Volume\s+\d+(?:\.\d+)?$",
        exclude_page_patterns=[
            r"/", r"illustration", r"summary", r"short story", r"unnamed",
        ],
        chapters_wikitext_section="List of Chapters",
    ),
    "lotm": FandomSeriesConfig(
        series_key="lotm",
        subdomain="lordofthemysteries",
        series_display_name="Lord of the Mysteries",
        author="Cuttlefish That Loves Diving",
        aliases=["lord of the mysteries", "lord of mystery"],
        cover_url="https://static.wikia.nocookie.net/lord-of-the-mystery/images/c/cd/LOM_Manhua_cover.png/revision/latest?cb=20200113124228",
        structure_type=StructureType.MASTER_SECTIONS,
        volumes_master_page="Volumes and Chapters",
        page_pattern=r"^Volume\s+\d+(?:\.\d+)?(?::\s*.+)?$",
        exclude_page_patterns=[r"summary", r"illustration", r"/", r"mini arc", r"untitled"],
        series_filter="lord of the mysteries",
        exclude_series_patterns=[r"circle of inevitability", r"coi"],
    ),
    "coi": FandomSeriesConfig(
        series_key="coi",
        subdomain="lordofthemysteries",
        series_display_name="Circle of Inevitability",
        author="Cuttlefish That Loves Diving",
        aliases=["circle of inevitability", "coi"],
        cover_url="https://static.wikia.nocookie.net/lord-of-the-mystery/images/d/dd/Circle_of_Inevitability_Official_Art.jpg/revision/latest/scale-to-width-down/268?cb=20230615130622",
        structure_type=StructureType.MASTER_SECTIONS,
        volumes_master_page="Volumes and Chapters",
        page_pattern=r"^Volume\s+\d+(?:\.\d+)?(?::\s*.+)?$",
        exclude_page_patterns=[r"summary", r"illustration", r"/", r"mini arc", r"untitled"],
        series_filter="circle of inevitability",
        exclude_series_patterns=[r"lord of the mysteries"],
    ),
}

# Build alias → config lookup
_ALIAS_INDEX: dict[str, FandomSeriesConfig] = {}
for _cfg in CATALOG_SERIES.values():
    _ALIAS_INDEX[_cfg.series_key] = _cfg
    for _alias in _cfg.aliases:
        _ALIAS_INDEX[re.sub(r"\s+", " ", _alias.lower()).strip()] = _cfg


# ── Helpers ──────────────────────────────────────────────────


def _api_url(subdomain: str) -> str:
    return f"https://{subdomain}.fandom.com/api.php"


def _clean_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").lower()).strip()


def _should_exclude(title: str, patterns: list[str]) -> bool:
    t = title.lower()
    return any(re.search(p, t, re.IGNORECASE) for p in patterns)


def _parse_volume_number(title: str) -> Optional[float]:
    m = re.search(
        r"(?:light novel volume|volume|vol\.?|book)\s+(\d+(?:\.\d+)?)",
        title, flags=re.IGNORECASE,
    )
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", title)
    return float(m.group(1)) if m else None


def _extract_subtitle(wiki_page: str) -> Optional[str]:
    m = re.search(r":\s*(.+)$", wiki_page)
    return m.group(1).strip() if m else None


def _sort_volumes(volumes: list[FandomVolume]) -> list[FandomVolume]:
    return sorted(volumes, key=lambda v: v.volume_number)


def _matches_series_context(config: FandomSeriesConfig, title: str, wikitext: str) -> bool:
    if not config.series_filter:
        return True
    title_text = re.sub(r"\s+", " ", f"{title} {wikitext or ''}").lower()
    filter_text = re.sub(r"\s+", " ", config.series_filter.lower()).strip()
    if not filter_text:
        return True
    if filter_text in title_text:
        return True

    compact_filter = re.sub(r"\bthe\b", "", filter_text).strip()
    compact_text = re.sub(r"\bthe\b", "", title_text).strip()
    if compact_filter and compact_filter in compact_text:
        return True

    display_text = re.sub(r"\s+", " ", config.series_display_name.lower()).strip()
    if display_text and display_text in title_text:
        return True
    compact_display = re.sub(r"\bthe\b", "", display_text).strip()
    if compact_display and compact_display in compact_text:
        return True
    return False


def _fetch_wikitext(subdomain: str, page: str) -> str:
    try:
        r = httpx.get(
            _api_url(subdomain),
            params={"action": "query", "titles": page, "prop": "revisions",
                    "rvprop": "content", "format": "json"},
            headers=HEADERS, timeout=10.0,
        )
        if r.status_code != 200:
            return ""
        for pinfo in r.json().get("query", {}).get("pages", {}).values():
            if "missing" not in pinfo:
                return pinfo.get("revisions", [{}])[0].get("*", "") or ""
    except Exception as e:
        log.warning(f"Wikitext fetch failed for {page} on {subdomain}: {e}")
    return ""


def _extract_chapter_range_from_wikitext(wikitext: str) -> tuple[Optional[int], Optional[int]]:
    m = re.search(r"\|chapters\s*=\s*(\d+)\s*[-–]\s*(\d+)", wikitext, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _parse_nihongo_english(line: str) -> str:
    """Extract English title from {{nihongo|"English"|...}} or quoted strings."""
    m = re.search(r'\{\{nihongo\|"([^"]+)"', line)
    if m:
        return m.group(1).strip()
    m = re.search(r'\[\[[^\]|]+\|([^\]]+)\]\][:\s]*"?([^"\n]+)"?', line)
    if m:
        label = m.group(1).strip()
        quoted = (m.group(2) or "").strip().strip('"')
        if quoted and not quoted.startswith("{"):
            return quoted
        return label
    m = re.search(r'\|([^\]]+)\]\]', line)
    if m:
        return m.group(1).strip()
    return ""


# ── Config resolution ────────────────────────────────────────


def resolve_series_config(query: str) -> Optional[FandomSeriesConfig]:
    """Match a search query to a cataloged series config."""
    q = _clean_query(query)
    if q in _ALIAS_INDEX:
        return _ALIAS_INDEX[q]

    for alias, cfg in _ALIAS_INDEX.items():
        if alias in q or q in alias:
            return cfg

    # Try stripping volume suffix
    base = re.sub(
        r"\s*,\s*(vol\.|volume|vol|light novel volume)\s*\d+.*",
        "", q, flags=re.IGNORECASE,
    )
    base = re.sub(
        r"\s+\b(vol\.|volume|vol|light novel volume)\s*\d+.*",
        "", base, flags=re.IGNORECASE,
    ).strip()
    if base in _ALIAS_INDEX:
        return _ALIAS_INDEX[base]

    return None


def get_config_by_subdomain(subdomain: str, query: Optional[str] = None) -> Optional[FandomSeriesConfig]:
    if query:
        cfg = resolve_series_config(query)
        if cfg and cfg.subdomain == subdomain:
            return cfg
    for cfg in CATALOG_SERIES.values():
        if cfg.subdomain == subdomain:
            return cfg
    return None


# ── Volume index fetchers ────────────────────────────────────


def _fetch_dedicated_pages(config: FandomSeriesConfig) -> list[FandomVolume]:
    prefix = config.page_prefix or "Volume"
    url = _api_url(config.subdomain)
    volumes: list[FandomVolume] = []
    seen: set[str] = set()

    try:
        r = httpx.get(url, params={
            "action": "query", "list": "allpages",
            "apprefix": prefix, "aplimit": 500, "format": "json",
        }, headers=HEADERS, timeout=12.0)
        if r.status_code != 200:
            return []
        for item in r.json().get("query", {}).get("allpages", []):
            title = item.get("title", "").strip()
            if not title or title in seen:
                continue
            if config.page_pattern and not re.match(config.page_pattern, title, re.IGNORECASE):
                continue
            if _should_exclude(title, config.exclude_page_patterns):
                continue
            seen.add(title)
            vol_num = _parse_volume_number(title)
            if vol_num is None:
                continue
            wt = _fetch_wikitext(config.subdomain, title)
            ch_start, ch_end = _extract_chapter_range_from_wikitext(wt)
            volumes.append(FandomVolume(
                volume_number=vol_num,
                wiki_page=title,
                display_title=f"{config.series_display_name}, {title}",
                series_key=config.series_key,
                subtitle=_extract_subtitle(title),
                chapter_start=ch_start,
                chapter_end=ch_end,
            ))
    except Exception as e:
        log.warning(f"Dedicated pages fetch failed for {config.series_key}: {e}")

    return _sort_volumes(volumes)


def _fetch_wikilink_list(config: FandomSeriesConfig) -> list[FandomVolume]:
    volumes: list[FandomVolume] = []
    seen: set[str] = set()

    # Source 1: master page wikilinks
    if config.volumes_master_page:
        wt = _fetch_wikitext(config.subdomain, config.volumes_master_page)
        if wt:
            links = re.findall(
                r"\[\[([^\]|]*?(?:light novel volume|volume)\s+\d+(?:\.\d+)?)"
                r"(?:\|[^\]]*)?\]\]",
                wt, flags=re.IGNORECASE,
            )
            for raw in links:
                title = raw.strip()
                if title in seen:
                    continue
                if config.page_pattern and not re.match(config.page_pattern, title, re.IGNORECASE):
                    continue
                if _should_exclude(title, config.exclude_page_patterns):
                    continue
                if config.series_filter and config.series_filter.lower() not in title.lower() and config.series_filter.lower() not in wt.lower():
                    continue
                seen.add(title)
                vol_num = _parse_volume_number(title)
                if vol_num is None:
                    continue
                volumes.append(FandomVolume(
                    volume_number=vol_num,
                    wiki_page=title,
                    display_title=f"{config.series_display_name}, {title}",
                    series_key=config.series_key,
                    subtitle=None,
                ))

    # Source 2: allpages prefix (fills gaps)
    if config.page_prefix:
        for vol in _fetch_dedicated_pages(config):
            if vol.wiki_page not in seen:
                seen.add(vol.wiki_page)
                volumes.append(vol)

    return _sort_volumes(volumes)


def _fetch_master_sections(config: FandomSeriesConfig) -> list[FandomVolume]:
    volumes: list[FandomVolume] = []
    seen: set[str] = set()

    wt = _fetch_wikitext(config.subdomain, config.volumes_master_page or "Volumes and Chapters")
    if wt:
        headings = re.findall(r"^==+\s*(Volume\s+\d+(?:\.\d+)?(?::\s*.+)?)\s*==+", wt, flags=re.IGNORECASE | re.MULTILINE)
        for raw in headings:
            title = raw.strip()
            if title in seen:
                continue
            if config.page_pattern and not re.match(config.page_pattern, title, re.IGNORECASE):
                continue
            if _should_exclude(title, config.exclude_page_patterns):
                continue
            if not _matches_series_context(config, title, wt):
                continue
            seen.add(title)
            vol_num = _parse_volume_number(title)
            if vol_num is None:
                continue
            volumes.append(FandomVolume(
                volume_number=vol_num,
                wiki_page=title,
                display_title=f"{config.series_display_name}, {title}",
                series_key=config.series_key,
                subtitle=_extract_subtitle(title),
            ))

    if volumes:
        return _sort_volumes(volumes)

    # Fallback: query all pages with prefix "Volume" and filter by the series context.
    try:
        r = httpx.get(_api_url(config.subdomain), params={
            "action": "query", "list": "allpages", "apprefix": "Volume",
            "aplimit": 200, "format": "json",
        }, headers=HEADERS, timeout=12.0)
        if r.status_code == 200:
            for item in r.json().get("query", {}).get("allpages", []):
                title = item.get("title", "").strip()
                if not title or title in seen:
                    continue
                if "/" in title:
                    continue
                if config.page_pattern and not re.match(config.page_pattern, title, re.IGNORECASE):
                    continue
                if _should_exclude(title, config.exclude_page_patterns):
                    continue
                # For some wikis (e.g. LOTM) the volume pages do not contain the
                # series-filter phrase in their wikitext, so the fallback should
                # still include them rather than fail the entire index.
                seen.add(title)
                vol_num = _parse_volume_number(title)
                if vol_num is None:
                    continue
                volumes.append(FandomVolume(
                    volume_number=vol_num,
                    wiki_page=title,
                    display_title=f"{config.series_display_name}, {title}",
                    series_key=config.series_key,
                    subtitle=_extract_subtitle(title),
                ))
    except Exception as e:
        log.warning(f"Master-section fallback failed for {config.series_key}: {e}")

    return _sort_volumes(volumes)


def fetch_volume_index(config: FandomSeriesConfig) -> list[FandomVolume]:
    if config.structure_type == StructureType.DEDICATED_PAGES:
        return _fetch_dedicated_pages(config)
    if config.structure_type == StructureType.WIKILINK_LIST:
        return _fetch_wikilink_list(config)
    if config.structure_type == StructureType.MASTER_SECTIONS:
        return _fetch_master_sections(config)
    return []


def fetch_volumes_for_search(subdomain: str, query: str) -> Optional[list[FandomVolume]]:
    """
    Entry point for book search.  Returns None if the series is not in the catalog.
    """
    config = resolve_series_config(query) or get_config_by_subdomain(subdomain, query)
    if not config or config.subdomain != subdomain:
        return None
    volumes = fetch_volume_index(config)
    return volumes if volumes else None


# ── Chapter fetchers ─────────────────────────────────────────


def _chapters_from_novel_master_page(
    config: FandomSeriesConfig, volume: FandomVolume,
) -> list[str]:
    if not config.novel_master_page:
        return []
    if volume.chapter_start is None or volume.chapter_end is None:
        wt = _fetch_wikitext(config.subdomain, volume.wiki_page)
        volume.chapter_start, volume.chapter_end = _extract_chapter_range_from_wikitext(wt)

    if volume.chapter_start is None or volume.chapter_end is None:
        return []

    try:
        r = httpx.get(
            _api_url(config.subdomain),
            params={"action": "parse", "page": config.novel_master_page,
                    "prop": "text", "format": "json"},
            headers=HEADERS, timeout=15.0,
        )
        if r.status_code != 200:
            return []
        html = r.json().get("parse", {}).get("text", {}).get("*", "")
        soup = BeautifulSoup(html, "html.parser")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue
            # Find header row with chapter # and chapter title
            header_idx = -1
            num_col = title_col = -1
            for ri, tr in enumerate(rows[:3]):
                cells = tr.find_all(["th", "td"])
                headers = [c.get_text().strip().lower() for c in cells]
                if not any("chapter" in h for h in headers):
                    continue
                for ci, h in enumerate(headers):
                    if "#" in h or h in ("chapter", "ch", "no", "no."):
                        num_col = ci
                    if "title" in h or "name" in h:
                        title_col = ci
                if title_col == -1 and num_col != -1 and len(headers) >= 2:
                    title_col = 1 if num_col == 0 else 0
                if title_col != -1:
                    header_idx = ri
                    break
            if header_idx == -1 or title_col == -1:
                continue

            chapters: list[str] = []
            for tr in rows[header_idx + 1:]:
                cells = tr.find_all(["td", "th"])
                if len(cells) <= title_col:
                    continue
                num_text = cells[num_col].get_text().strip() if num_col < len(cells) else ""
                title_text = cells[title_col].get_text().strip()
                if not num_text.isdigit():
                    continue
                num = int(num_text)
                if num < volume.chapter_start:
                    continue
                if num > volume.chapter_end:
                    if chapters:
                        break
                    continue
                if title_text and not _is_pov_only(title_text):
                    chapters.append(title_text)

            if len(chapters) >= 5:
                return _validate_chapters(chapters)

    except Exception as e:
        log.warning(f"Novel master page chapter fetch failed for {volume.wiki_page}: {e}")

    return []


def _chapters_from_wikitext_section(
    config: FandomSeriesConfig, volume: FandomVolume,
) -> list[str]:
    wt = _fetch_wikitext(config.subdomain, volume.wiki_page)
    if not wt:
        return []

    # LOTM/COI expose the chapter table in the rendered page HTML, so prefer that
    # over brittle plain-text scraping.
    if config.subdomain == "lordofthemysteries":
        try:
            r = httpx.get(
                _api_url(config.subdomain),
                params={"action": "parse", "page": volume.wiki_page, "prop": "text", "format": "json"},
                headers=HEADERS, timeout=15.0,
            )
            if r.status_code == 200:
                html = r.json().get("parse", {}).get("text", {}).get("*", "")
                soup = BeautifulSoup(html, "html.parser")
                # Find the chapter table in rendered HTML.
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    if len(rows) < 3:
                        continue
                    headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
                    if not any("chapter" in h for h in headers):
                        continue
                    chapters: list[str] = []
                    for tr in rows[1:]:
                        cells = tr.find_all(["td", "th"])
                        if not cells:
                            continue
                        title_cell = None
                        for idx, cell in enumerate(cells):
                            txt = cell.get_text(" ", strip=True)
                            if not txt:
                                continue
                            if "chapter" in headers[idx].lower() and "name" in headers[idx].lower():
                                title_cell = txt
                                break
                        if title_cell and title_cell not in {"Chapter Name", "Chapter"}:
                            chapters.append(re.sub(r"\s+", " ", title_cell).strip())
                    if chapters:
                        return _validate_chapters(chapters)
        except Exception as e:
            log.warning(f"Rendered chapter table parse failed for {volume.wiki_page}: {e}")

    section = config.chapters_wikitext_section or "List of Chapters"
    m = re.search(
        rf"==\s*{re.escape(section)}\s*==(.*?)(?=\n==|\Z)",
        wt, flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []

    chapters: list[str] = []
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line.startswith("*"):
            continue
        title = _parse_nihongo_english(line)
        if not title:
            lm = re.search(r"\[\[[^\]|]+\|([^\]]+)\]\]", line)
            if lm:
                title = lm.group(1).strip()
            else:
                lm2 = re.search(r"\[\[([^\]|#]+)\]\]", line)
                if lm2:
                    title = lm2.group(1).strip()
        if title:
            title = re.sub(r"^(prologue|epilogue|chapter\s+\d+)[:\s]*", "", title, flags=re.IGNORECASE).strip()
            if title:
                chapters.append(title)

    return _validate_chapters(chapters) if chapters else []


def _is_pov_only(text: str) -> bool:
    return bool(re.match(r"^[\w\s]+POV$", text.strip(), re.IGNORECASE))


def _validate_chapters(chapters: list[str]) -> list[str]:
    if not chapters:
        return []
    # Reject POV-only entries
    cleaned = [c for c in chapters if not _is_pov_only(c)]
    if not cleaned:
        return []
    # Reject if >40% duplicates
    dup_ratio = 1 - len(set(c.lower() for c in cleaned)) / len(cleaned)
    if dup_ratio > 0.4:
        log.warning(f"Chapter list rejected: {dup_ratio:.0%} duplicates")
        return []
    return cleaned[:300]


def get_volume_chapters(config: FandomSeriesConfig, volume: FandomVolume) -> list[str]:
    if config.novel_master_page:
        return _chapters_from_novel_master_page(config, volume)
    if config.chapters_wikitext_section:
        return _chapters_from_wikitext_section(config, volume)
    return []


def find_volume_by_title(
    config: FandomSeriesConfig, title: str,
    volumes: Optional[list[FandomVolume]] = None,
) -> Optional[FandomVolume]:
    """Resolve a user-facing title to a catalog volume."""
    volumes = volumes or fetch_volume_index(config)
    if not volumes:
        return None

    title_low = title.lower()
    req_vol = _parse_volume_number(title)

    # Exact wiki_page or display_title match
    for v in volumes:
        if v.wiki_page.lower() in title_low or v.display_title.lower() == title_low:
            return v

    # Match by volume number
    if req_vol is not None:
        for v in volumes:
            if v.volume_number == req_vol:
                return v

    return None


def fetch_chapters_for_title(subdomain: str, book_title: str) -> list[str]:
    """Entry point for summary tool chapter extraction."""
    config = get_config_by_subdomain(subdomain, book_title)
    if not config:
        return []
    volume = find_volume_by_title(config, book_title)
    if not volume:
        return []
    return get_volume_chapters(config, volume)


def fetch_volume_synopsis(config: FandomSeriesConfig, volume: FandomVolume) -> str:
    """Extract Synopsis section from a volume's wiki page."""
    try:
        r = httpx.get(
            _api_url(config.subdomain),
            params={"action": "parse", "page": volume.wiki_page,
                    "prop": "text", "format": "json"},
            headers=HEADERS, timeout=10.0,
        )
        if r.status_code != 200:
            return ""
        html = r.json().get("parse", {}).get("text", {}).get("*", "")
        soup = BeautifulSoup(html, "html.parser")
        for hl in soup.find_all(class_="mw-headline"):
            if "synopsis" in hl.get_text().lower():
                paragraphs = []
                current = hl.parent
                for sibling in current.next_siblings:
                    if getattr(sibling, "name", None) in ("h2", "h3"):
                        break
                    if sibling.name == "p":
                        t = sibling.get_text().strip()
                        if t:
                            paragraphs.append(t)
                text = "\n\n".join(paragraphs)
                text = re.sub(r"\[\d+\]", "", text)
                return html_lib.unescape(text).strip()
    except Exception as e:
        log.warning(f"Synopsis fetch failed for {volume.wiki_page}: {e}")
    return ""
