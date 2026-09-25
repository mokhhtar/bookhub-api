"""Normalize a Studio research sheet on stdin without writing catalogue data."""
import json
import sys
from publication import answers, author_status, validate_fact


def normalize(sheet):
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
