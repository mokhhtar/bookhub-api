"""Import sourced research and refresh only derived cells; preserve every shipped row.

python scripts/akinator/work_facts.py migrate
python scripts/akinator/work_facts.py queue --limit 100
python scripts/akinator/work_facts.py import research.json
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
from publication import (DATA, DEFINITIONS, LADDER, LEGACY_IDS, QUESTIONS,
                         answers, author_status, valid_year, validate_fact, derive_vector)


def refresh(books, questions, meta, raw, facts, corrections=None):
    """Pure transformation, usable by the local importer and admin API."""
    books, questions, meta = copy.deepcopy((books, questions, meta))
    old_n, old_bpr = len(questions), meta['bytes_per_row']
    if len(raw) != len(books) * old_bpr: raise ValueError('Matrix size mismatch')
    for q in questions:
        q['id'] = LEGACY_IDS.get(q['id'], q['id'])
        if q['id'] in QUESTIONS: q['text'] = QUESTIONS[q['id']]
    ids = {q['id'] for q in questions}
    for q in DEFINITIONS:
        if q['id'] not in ids: questions.append({'id': q['id'], 'text': q['text'], 'base': 0})
    if 'fact:anonymous' not in ids:
        questions.append({'id': 'fact:anonymous', 'text': 'Is the identity of this work’s author unknown?', 'base': 0})
    bpr = (len(questions) + 3) // 4
    packed = bytearray(len(books) * bpr)
    yes_counts = [0] * len(questions)
    changed = 0
    for i, book in enumerate(books):
        fact = facts.get(book['k'], {})
        doc = {'first_publish_year': book.get('y'),
               **{k: book[k] for k in ('publication','publication_answers','authorship') if k in book}, **fact}
        if 'publication' in fact:
            doc['first_publish_year'] = (fact['publication'] or {}).get('year')
        correction = (corrections or {}).get(book['k'], {})
        if 'authorship' in correction: doc['authorship'] = correction['authorship']
        if 'first_publish_year' in correction:
            doc['first_publish_year'] = correction['first_publish_year']
            doc.pop('publication', None)
            doc.pop('publication_answers', None)
        if correction.get('no_author'): doc['no_author'] = True
        if correction.get('author_name'):
            doc['authorship'] = {'status': 'known'}
        values = answers(doc)
        status = author_status(doc)
        values['fact:anonymous'] = True if status == 'anonymous' else False if status == 'known' else None
        if status in ('anonymous', 'disputed'):
            values.update({q['id']: None for q in questions if q['id'].startswith('author:')})
        publication = doc.get('publication', {})
        book['y'] = publication.get('year') if publication else doc.get('first_publish_year')
        direct = doc.get('publication_answers') or {}
        authorship = doc.get('authorship') or {}
        if publication: book['publication'] = publication
        else: book.pop('publication', None)
        if direct: book['publication_answers'] = direct
        else: book.pop('publication_answers', None)
        if authorship and status != 'unresearched': book['authorship'] = authorship
        else: book.pop('authorship', None)
        if status == 'anonymous': book['a'] = 'Unknown author'
        elif book.get('a') == 'Unknown author': book['a'] = ''
        protected = sorted(k for k in values if k.startswith('author:'))
        if protected: book['protected_questions'] = protected
        else: book.pop('protected_questions', None)
        # Keep historical research vectors, but date bins are derived too.
        if isinstance(book.get('v'), dict):
            book['v'] = derive_vector(book['v'], doc)
        for j, q in enumerate(questions):
            old = (raw[i * old_bpr + j // 4] >> (2 * (j % 4))) & 3 if j < old_n else 2
            value = values.get(q['id'])
            state = (2 if value is None else 1 if value else 0) if q['id'] in values else old
            changed += state != old
            yes_counts[j] += state == 1
            packed[i*bpr+j//4] |= state << (2*(j%4))
    for j, q in enumerate(questions):
        if q['id'] in QUESTIONS or q['id'] == 'fact:anonymous':
            q['base'] = round(yes_counts[j] / max(1, len(books)), 4)
    meta.update(books=len(books), questions=len(questions), bytes_per_row=bpr,
                work_facts_version=1, publication_ladder=LADDER,
                question_hash=hashlib.sha256('|'.join(q['id'] for q in questions).encode()).hexdigest()[:16])
    return books, questions, meta, bytes(packed), changed


def load(root, name, default):
    p = root / name
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def save(root, name, value):
    p = root / name
    compact = name in {'authors.json','characters.json','series.json','questions.json','meta.json',
                       'question_dependencies.json','exclusive_overrides.json'}
    indent = None if compact else 1 if name == 'overrides_locked.json' else 2
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False,
               indent=indent, separators=(',',':') if compact else None)+'\n').encode()
    temp = p.with_suffix(p.suffix + '.tmp')
    temp.write_bytes(data)
    temp.replace(p)


def migrate(root):
    books, qs, meta = (load(root, n, None) for n in ('books.json','questions.json','meta.json'))
    facts = load(root, 'work_facts.json', {})
    # Persist existing model vectors independently of generated books.json.
    vectors = load(root, 'research_vectors.json', {})
    for b in books:
        if 'v' in b: vectors.setdefault(b['k'], b['v'])
    result = refresh(books, qs, meta, (root/'matrix.bin').read_bytes(), facts,
                     load(root, 'admin_corrections.json', {}))
    new_books, new_qs, new_meta, packed, count = result
    quarantine = load(root, 'publication_legacy_answers.json', {})
    for name in ('overrides.json', 'overrides_locked.json', 'question_overrides.json'):
        data = load(root, name, {})
        if name == 'question_overrides.json':
            for key in list(data):
                if key in LEGACY_IDS or key in QUESTIONS:
                    quarantine.setdefault(name, {})[key] = data.pop(key)
        else:
            by_key = {b['k']: set(b.get('protected_questions', [])) for b in new_books}
            for key, cells in list(data.items()):
                retired = (set(LEGACY_IDS) | set(QUESTIONS) |
                           {'fact:anonymous'} | by_key.get(key, set()))
                if isinstance(cells, dict):
                    for q in list(cells):
                        if q in retired: quarantine.setdefault(name, {}).setdefault(key,{})[q] = cells.pop(q)
                elif isinstance(cells, list):
                    removed = [q for q in cells if q in retired]
                    if removed: quarantine.setdefault(name, {})[key] = removed
                    data[key] = [q for q in cells if q not in retired]
        save(root, name, data)
    def rename(obj):
        if isinstance(obj, dict): return {LEGACY_IDS.get(k,k): rename(v) for k,v in obj.items()}
        if isinstance(obj, list): return [rename(v) for v in obj]
        return LEGACY_IDS.get(obj,obj) if isinstance(obj,str) else obj
    for name in ('question_policy.json','question_dependencies.json','exclusive_overrides.json'):
        value = rename(load(root,name,{} if name == 'question_policy.json' else []))
        if name == 'question_policy.json':
            entries = value.setdefault('questions', {})
            entries['fact:anonymous'] = {'level':'general','domain':'author','answerability_cost':0.05}
            for key, entry in entries.items():
                if key.startswith('author:'):
                    anonymous = {'question':'fact:anonymous','answer':'yes'}
                    previous = entry.get('skip_if')
                    if previous and anonymous not in previous.get('any', []) and previous != anonymous:
                        entry['skip_if'] = {'any':[previous, anonymous]}
                    elif not previous: entry['skip_if'] = anonymous
            from question_policy import policy_digest
            new_meta['question_policy_digest'] = policy_digest(value)
        save(root,name,value)
    authors = load(root,'authors.json',{})
    characters = load(root, 'characters.json', {})
    series = load(root, 'series.json', {})
    for i,b in enumerate(new_books):
        if (b.get('authorship') or {}).get('status') in ('anonymous','disputed') and i < len(authors.get('books',[])):
            authors['books'][i] = []
    for name, value in [('books.json',new_books),('questions.json',new_qs),('matrix.bin',packed),
                        ('meta.json',new_meta),('authors.json',authors),
                        ('characters.json',characters),('series.json',series),
                        ('work_facts.json',facts),
                        ('research_vectors.json',vectors),('publication_questions.json',DEFINITIONS),
                        ('publication_legacy_answers.json',quarantine)]: save(root,name,value)
    return {'books':len(books),'questions':len(new_qs),'changed_cells':count}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['migrate','queue','import'])
    p.add_argument('file',nargs='?')
    p.add_argument('--data',type=Path,default=DATA)
    p.add_argument('--limit',type=int,default=100)
    args=p.parse_args()
    if args.command == 'queue':
        books=load(args.data,'books.json',[]); facts=load(args.data,'work_facts.json',{})
        questions=load(args.data,'questions.json',[]); meta=load(args.data,'meta.json',{})
        raw=(args.data/'matrix.bin').read_bytes(); width=meta['bytes_per_row']
        overrides=load(args.data,'overrides.json',{})
        out=[]
        for i,b in enumerate(books):
            doc={'first_publish_year':b.get('y'),
                 **{k:b[k] for k in ('publication','publication_answers','authorship') if k in b},
                 **facts.get(b['k'],{})}
            missing=[]
            if any(v is None for v in answers(doc).values()): missing.append('first_publication')
            if author_status(doc)=='unresearched': missing.append('authorship')
            for j,q in enumerate(questions):
                if q['id'] in QUESTIONS or q['id']=='fact:anonymous': continue
                if author_status(doc) in ('anonymous','disputed') and q['id'].startswith('author:'): continue
                if q['id'] in overrides.get(b['k'],{}): continue
                if (raw[i*width+j//4]>>(2*(j%4)))&3 == 2: missing.append(q['id'])
            if missing: out.append({'key':b['k'],'title':b['t'],'author':b.get('a'),'missing':missing})
        out.sort(key=lambda row:-len(row['missing']))
        print(json.dumps(out[:max(0,args.limit)],ensure_ascii=False,indent=2))
    else:
        if args.command == 'import':
            incoming=json.loads(Path(args.file).read_text(encoding='utf-8'))
            if incoming.get('key'):
                incoming={incoming['key']:incoming}
            elif 'key' in incoming:
                raise ValueError('Research sheet is not linked to a shipped book key')
            records=load(args.data,'work_facts.json',{})
            keys={b['k'] for b in load(args.data,'books.json',[])}
            for key, fact in incoming.items():
                if key not in keys: raise ValueError('Book not in shipped catalogue: '+key)
                merged={**records.get(key,{}),**{k:v for k,v in fact.items() if k in ('publication','publication_answers','authorship')}}
                records[key]=validate_fact(merged)
            save(args.data,'work_facts.json',records)
        print(json.dumps(migrate(args.data)))


if __name__=='__main__': main()
