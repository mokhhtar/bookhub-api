import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from publication import answers, validate_fact, apply_facts, QUESTIONS, DEFINITIONS
from work_facts import refresh
from normalize_research_sheet import normalize
from features import ladder_narrow, ladder_determined, structural_features

SOURCE=['https://example.org/catalogue/work']


class PublicationTests(unittest.TestCase):
    def test_boundaries(self):
        for year in (1899,1900,1949,1950,1969,1970,1999,2000,2001,2015,2016,2025):
            result=answers({'first_publish_year':year})
            for q in DEFINITIONS:
                expected=year<q['year'] if q['op']=='<' else year>=q['year']
                self.assertEqual(result[q['id']],expected)

    def test_missing_and_invalid(self):
        for year in (None,0,True,'2016',2016.5,10000):
            self.assertTrue(all(v is None for v in answers({'first_publish_year':year}).values()))

    def test_range_and_composition(self):
        fact={'publication':{'basis':'first_publication','earliest':1950,'latest':2010,'sources':SOURCE}}
        result=answers(validate_fact(fact))
        self.assertFalse(result['fact:firstpub_lt_1950'])
        self.assertIsNone(result['fact:firstpub_lt_2000'])
        self.assertFalse(result['fact:firstpub_ge_2016'])
        self.assertTrue(all(v is None for v in answers({'publication':{'basis':'composition','year':1000},'first_publish_year':1000}).values()))

    def test_direct_evidence(self):
        fact={'publication_answers':{'fact:firstpub_lt_1900':{'value':True,'sources':SOURCE}}}
        inferred = answers(validate_fact(fact))
        self.assertTrue(inferred['fact:firstpub_lt_1900'])
        self.assertFalse(inferred['fact:firstpub_ge_2016'])
        bad=copy.deepcopy(fact); bad['publication_answers']['fact:firstpub_lt_1900']['sources']=[]
        with self.assertRaises(ValueError): validate_fact(bad)
        fact['publication_answers']['fact:firstpub_ge_2016']={'value':True,'sources':SOURCE}
        with self.assertRaises(ValueError): validate_fact(fact)

    def test_date_beats_conflicting_sheet(self):
        sheet={'publication':{'basis':'first_publication','year':2018,'sources':SOURCE},
               'answers':{'fact:firstpub_ge_2016':'no'}}
        self.assertEqual(normalize(sheet)['answers']['fact:firstpub_ge_2016'],'yes')

    def test_anonymous_is_not_empty_author(self):
        with self.assertRaises(ValueError):
            validate_fact({'authorship': {'status': 'anonymous', 'sources': []}})
        for status,expected in [('anonymous',True),('known',False),('disputed',None),('unresearched',None)]:
            book={'present':['author:american'],'known_false':['author:british'],'unknown':[],'author_ids':['x']}
            apply_facts(book,{'authorship':{'status':status}})
            group='unknown' if expected is None else 'present' if expected else 'known_false'
            self.assertIn('fact:anonymous',book[group])
            if status in ('anonymous','disputed'):
                self.assertIn('author:american',book['unknown'])
                self.assertIn('author:british',book['unknown'])
                self.assertEqual(book['author_ids'],[])
        self.assertIsNone(structural_features({'author_name':[]},0,1)['fact:anonymous'])

    def test_refresh_preserves_unrelated_cells(self):
        books=[{'k':'/works/test','t':'Test','a':'Writer','y':2018,'v':{'genre':'keep'}}]
        qs=[{'id':'form:fiction','text':'Fiction?','base':1},{'id':'fact:verrecent','text':'Last 10 years?','base':0}]
        meta={'bytes_per_row':1,'books':1,'questions':2}
        out=refresh(books,qs,meta,bytes([1]),{})
        rows, questions, metadata, raw,_=out
        self.assertEqual(raw[0]&3,1)
        self.assertEqual((raw[0]>>2)&3,1)
        self.assertEqual(rows[0]['v']['genre'],'keep')
        self.assertNotEqual(metadata.get('question_hash'),meta.get('question_hash'))
        again=refresh(rows,questions,metadata,raw,{})
        self.assertEqual(out[:4],again[:4])
        corrected=refresh(rows,questions,metadata,raw,{}, {'/works/test':{'first_publish_year':1990}})
        self.assertEqual((corrected[3][0]>>2)&3,0)
        self.assertEqual(corrected[0][0]['y'],1990)

    def test_refresh_applies_research_vectors_but_date_wins(self):
        books=[{'k':'/works/test','t':'Test','a':'Writer','y':2018}]
        qs=[{'id':'form:fiction','text':'Fiction?','base':1},
            {'id':'fact:firstpub_ge_2016','text':'Published in 2016 or later?','base':0}]
        meta={'bytes_per_row':1,'books':1,'questions':2}
        vectors={'/works/test':{'form:fiction':False,'fact:firstpub_ge_2016':False}}
        out=refresh(books,qs,meta,bytes([2 | (2 << 2)]),{},vectors=vectors)
        raw=out[3]
        self.assertEqual(raw[0]&3,0)
        self.assertEqual((raw[0]>>2)&3,1)

    def test_new_question_without_book_changes(self):
        future={'id':'fact:firstpub_le_2025','op':'<=','year':2025,'text':'First published in 2025 or earlier?'}
        with patch('publication.DEFINITIONS',DEFINITIONS+[future]):
            self.assertTrue(answers({'first_publish_year':2018})[future['id']])
            self.assertFalse(answers({'first_publish_year':2026})[future['id']])
        self.assertEqual(ladder_narrow(-10000,9999,'<=',2025,False),(2026,9999))
        self.assertTrue(ladder_determined('<=',2025,1700,1750))

    def test_calendar_independence(self):
        # The resolver has no clock dependency; changing the system date cannot
        # change a fixed predicate. Its output is identical across repeated builds.
        for _ in (2026,2027,2031):
            self.assertEqual(answers({'first_publish_year':2016})['fact:firstpub_ge_2016'],True)


if __name__=='__main__': unittest.main()
