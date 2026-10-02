"""Normalize a Studio research sheet on stdin without writing catalogue data."""
import copy
import json
import re
import sys
from urllib.parse import urlsplit
from publication import answers, author_status, validate_fact


URL_RE = re.compile(r"https?://[^\s<>\"'\\\[\]\(\)]+", re.IGNORECASE)


def _source_urls(items):
    """Turn plain or Markdown-wrapped source strings into canonical URLs."""
    urls = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        candidates = ([stripped] if stripped.startswith(("http://", "https://"))
                      else URL_RE.findall(item))
        for candidate in candidates:
            # Markdown renderers commonly leave punctuation attached to the
            # captured destination. It is never part of a catalogue URL.
            candidate = candidate.rstrip(".,;:!?")
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc and candidate not in urls:
                urls.append(candidate)
    return urls


def _normalize_sources(sheet):
    sheet = copy.deepcopy(sheet)
    for field in ("publication", "authorship"):
        entry = sheet.get(field)
        if isinstance(entry, dict) and "sources" in entry:
            entry["sources"] = _source_urls(entry["sources"])
    direct = sheet.get("publication_answers")
    if isinstance(direct, dict):
        for entry in direct.values():
            if isinstance(entry, dict) and "sources" in entry:
                entry["sources"] = _source_urls(entry["sources"])
    return sheet


def normalize(sheet):
    sheet = _normalize_sources(sheet)
    fact={key:sheet[key] for key in ('publication','publication_answers','authorship') if key in sheet}
    validate_fact(fact)
    result=dict(sheet.get('answers') or {})
    for key,value in answers(fact).items():
        result[key]='unknown' if value is None else 'yes' if value else 'no'
    status=author_status(fact)
    result['fact:anonymous']='yes' if status=='anonymous' else 'no' if status=='known' else 'unknown'
    if status in ('anonymous','disputed'):
        result.update({k:'unknown' for k in result if k.startswith('author:')})
    return {**sheet,'answers':result}


if __name__=='__main__':
    print(json.dumps(normalize(json.load(sys.stdin)),ensure_ascii=False))
