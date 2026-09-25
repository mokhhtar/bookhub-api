"""Stable work facts. Publication is distinct from composition and edition dates."""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[3] / 'bookhub/games/data/akinator'
LEGACY_IDS = dict(zip(
    ('fact:veryold', 'fact:old', 'fact:pre1970', 'fact:pre2000', 'fact:recent', 'fact:verrecent'),
    ('fact:firstpub_lt_1900', 'fact:firstpub_lt_1950', 'fact:firstpub_lt_1970',
     'fact:firstpub_lt_2000', 'fact:firstpub_ge_2001', 'fact:firstpub_ge_2016')))


def read_json(name, default):
    path = DATA / name
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


DEFINITIONS = json.loads(Path(__file__).with_name('publication_questions.json').read_text(encoding='utf-8'))
QUESTIONS = {q['id']: q['text'] for q in DEFINITIONS}
LADDER = [(q['id'], q['op'], q['year']) for q in DEFINITIONS]
AUTHOR_STATUSES = {'known', 'anonymous', 'disputed', 'unresearched'}


def valid_year(value):
    # Signed historical years are allowed; there is no calendar year zero.
    return type(value) is int and value != 0 and -10000 <= value <= 9999


def compare(year, op, threshold):
    return {'<': year < threshold, '<=': year <= threshold,
            '>': year > threshold, '>=': year >= threshold}[op]


def answers(doc):
    publication = doc.get('publication') or {}
    year = publication.get('year') if publication else doc.get('first_publish_year')
    if publication and publication.get('basis') != 'first_publication':
        year = None
    lo = hi = year if valid_year(year) else None
    if publication.get('basis') == 'first_publication' and lo is None:
        lo, hi = publication.get('earliest'), publication.get('latest')
        lo = lo if valid_year(lo) else None
        hi = hi if valid_year(hi) else None
    if lo is not None and hi is not None and lo > hi:
        lo = hi = None
    # A supported comparison also constrains the year, without inventing an
    # exact date. Its implications settle neighbouring questions automatically.
    for q in DEFINITIONS:
        evidence = (doc.get('publication_answers') or {}).get(q['id'], {})
        if not evidence.get('sources') or type(evidence.get('value')) is not bool:
            continue
        yes, op, t = evidence['value'], q['op'], q['year']
        boundary = t - 1 if op == '<' else t if op == '<=' else t + 1 if op == '>' else t
        if (op in ('<', '<=') and yes) or (op in ('>', '>=') and not yes):
            upper = boundary if yes else boundary - 1
            hi = min(hi, upper) if hi is not None else upper
        else:
            lower = boundary if yes else boundary + 1
            lo = max(lo, lower) if lo is not None else lower
    if lo is not None and hi is not None and lo > hi:
        return {q['id']: None for q in DEFINITIONS}
    out = {}
    for q in DEFINITIONS:
        a = compare(lo, q['op'], q['year']) if lo is not None else None
        b = compare(hi, q['op'], q['year']) if hi is not None else None
        value = a if a is not None and a == b else None
        if q['op'] in ('<', '<='):
            if a is False: value = False
            if b is True: value = True
        else:
            if a is True: value = True
            if b is False: value = False
        out[q['id']] = value
    return out


def author_status(doc):
    if doc.get('no_author'):
        return 'anonymous'
    return (doc.get('authorship') or {}).get('status', 'unresearched')


def derive_vector(vector, doc):
    """Legacy research bins remain compatible, but are never model guesses."""
    result = dict(vector)
    publication = doc.get('publication') or {}
    year = publication.get('year') if publication else doc.get('first_publish_year')
    for name, bounds in {'published_before_1900':(-10000,1899),
                         'published_1900_to_1950':(1900,1950),
                         'published_1951_to_2000':(1951,2000),
                         'published_after_2000':(2001,9999)}.items():
        result[name] = int(bounds[0] <= year <= bounds[1]) if valid_year(year) else None
    if author_status(doc) in ('anonymous','disputed'):
        for key in result:
            if key.startswith('author_'): result[key] = None
    return result


def apply_facts(book, doc):
    """Apply after enrichers so unrelated overlays cannot contradict work facts."""
    values = answers(doc)
    status = author_status(doc)
    values['fact:anonymous'] = True if status == 'anonymous' else False if status == 'known' else None
    if status in ('anonymous', 'disputed'):
        for key in set(book.get('present', [])) | set(book.get('unknown', [])) | set(book.get('known_false', [])):
            if key.startswith('author:'):
                values[key] = None
        book['author_ids'] = []
        if status == 'anonymous': book['author'] = 'Unknown author'
    sets = {name: set(book.get(name, [])) for name in ('present', 'unknown', 'known_false')}
    for key, value in values.items():
        for group in sets.values(): group.discard(key)
        sets['unknown' if value is None else 'present' if value else 'known_false'].add(key)
    for name, group in sets.items(): book[name] = sorted(group)
    publication = doc.get('publication') or {}
    direct = doc.get('publication_answers') or {}
    authorship = doc.get('authorship') or {}
    if publication: book['publication'] = publication
    else: book.pop('publication', None)
    if direct: book['publication_answers'] = direct
    else: book.pop('publication_answers', None)
    if authorship and status != 'unresearched': book['authorship'] = authorship
    else: book.pop('authorship', None)
    # Publication and anonymous-status cells are protected globally. Per-row
    # protection is needed only for author traits made inapplicable by a
    # sourced anonymous/disputed verdict.
    book['protected_questions'] = sorted(k for k in values if k.startswith('author:'))


def overlay_docs(docs, records):
    for doc in docs:
        fact = records.get(doc.get('key'), {})
        for field in ('publication', 'publication_answers', 'authorship'):
            if field in fact: doc[field] = fact[field]
        if 'publication' in fact:
            doc['first_publish_year'] = (fact['publication'] or {}).get('year')


def validate_fact(fact):
    """Reject unsupported claims rather than accepting model confidence."""
    if not isinstance(fact, dict) or set(fact) - {'publication','publication_answers','authorship'}:
        raise ValueError('Unknown work fact field')
    for key, value in fact.items():
        if value is not None and not isinstance(value, dict):
            raise ValueError('Work facts must be objects')
    def sourced(entry):
        sources = entry.get('sources')
        if not isinstance(sources, list) or not sources or not all(
                isinstance(s, str) and s.startswith(('https://', 'http://')) for s in sources):
            raise ValueError('A factual claim needs source URLs')
    publication = fact.get('publication')
    if publication:
        if publication.get('basis') != 'first_publication':
            raise ValueError('Publication must describe first publication, not composition or an edition')
        sourced(publication)
        if not any(publication.get(k) is not None for k in ('year','earliest','latest')):
            raise ValueError('Publication needs a year or a supported bound')
        for key in ('year', 'earliest', 'latest'):
            if publication.get(key) is not None and not valid_year(publication[key]):
                raise ValueError('Invalid publication year')
        lo, hi = publication.get('earliest'), publication.get('latest')
        if lo is not None and hi is not None and lo > hi: raise ValueError('Reversed publication range')
        year = publication.get('year')
        if year is not None and ((lo is not None and year < lo) or (hi is not None and year > hi)):
            raise ValueError('Year contradicts publication range')
    authorship = fact.get('authorship')
    if authorship:
        if authorship.get('status') not in AUTHOR_STATUSES: raise ValueError('Invalid authorship status')
        if authorship['status'] != 'unresearched': sourced(authorship)
    direct = fact.get('publication_answers') or {}
    derived = answers({**fact, 'publication_answers': {}})
    for key, entry in direct.items():
        if key not in QUESTIONS or not isinstance(entry, dict) or type(entry.get('value')) is not bool: raise ValueError('Invalid direct publication answer')
        sourced(entry)
        if derived[key] is not None and derived[key] != entry['value']:
            raise ValueError('Direct answer contradicts publication date')
    # Direct answers must also define a non-empty interval together.
    lo, hi = -float('inf'), float('inf')
    for q in DEFINITIONS:
        if q['id'] not in direct: continue
        yes, t, op = direct[q['id']]['value'], q['year'], q['op']
        boundary = t - 1 if op == '<' else t if op == '<=' else t + 1 if op == '>' else t
        if op in ('<', '<='):
            if yes: hi = min(hi, boundary)
            else: lo = max(lo, boundary + 1)
        else:
            if yes: lo = max(lo, boundary)
            else: hi = min(hi, boundary - 1)
    if lo > hi: raise ValueError('Contradictory direct publication answers')
    return fact
