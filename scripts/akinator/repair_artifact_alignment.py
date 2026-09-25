"""Explicit repair using the books.json saved with the current matrix's Git revision."""
import argparse
import json
from pathlib import Path
from work_facts import load, save


def repair(root, original):
    old=json.loads(original.read_text(encoding='utf-8'))
    current=load(root,'books.json',[]); meta=load(root,'meta.json',{})
    raw=(root/'matrix.bin').read_bytes(); width=meta['bytes_per_row']
    if len(raw)!=len(old)*width: raise ValueError('Original manifest does not match matrix')
    index={}
    for i,b in enumerate(old):
        key=b['k']
        if key in index and raw[index[key]*width:(index[key]+1)*width]!=raw[i*width:(i+1)*width]:
            raise ValueError('Duplicate rows have conflicting answers: '+key)
        index[key]=i
    if {b['k'] for b in current}!=set(index): raise ValueError('Book keys differ')
    order=[index[b['k']] for b in current]
    updates={}
    for name in ('authors.json','characters.json','series.json'):
        data=load(root,name,{})
        if len(data.get('books',[]))>len(old): raise ValueError('Unaligned '+name)
        # Incremental sync historically appends matrix rows without extending
        # these optional indexes; the browser treats the missing suffix as empty.
        rows=data.get('books',[])
        data['books']=[rows[i] if i<len(rows) else None if name=='series.json' else [] for i in order]
        updates[name]=data
    updates['matrix.bin']=b''.join(raw[i*width:(i+1)*width] for i in order)
    meta['books']=len(current);updates['meta.json']=meta
    for name,value in updates.items(): save(root,name,value)
    return {'before':len(old),'after':len(current),'removed_duplicate_rows':len(old)-len(current)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('data',type=Path);p.add_argument('original_books',type=Path)
    a=p.parse_args(); print(repair(a.data,a.original_books))
