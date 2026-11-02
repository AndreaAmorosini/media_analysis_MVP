"""Basic tests without requiring a live Neo4j/Unsloth installation.

Run: pixi run python -m unittest discover -s tests -p 'test_quality.py'
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

# The tested implementation is installed at scripts/kg_quality.py.
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'kg_quality.py'

# Use real dependencies if installed; tests of pure helpers can run without them.
try:
    import neo4j
except ImportError:
    fake_neo4j = types.ModuleType('neo4j')
    fake_neo4j.GraphDatabase = object()
    sys.modules['neo4j'] = fake_neo4j

try:
    import presskg.settings
except ImportError:
    fake_pkg = types.ModuleType('presskg')
    fake_pkg.__path__ = []
    fake_settings = types.ModuleType('presskg.settings')
    fake_settings.CFG = types.SimpleNamespace(
        neo4j_uri='bolt://test:7687',neo4j_user='neo4j',neo4j_password='x',
        neo4j_database='neo4j',data_dir='Data')
    sys.modules['presskg'] = fake_pkg
    sys.modules['presskg.settings'] = fake_settings

spec = importlib.util.spec_from_file_location('kg_quality_suite_testable', SCRIPT)
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


class MockStore:
    database = 'neo4j'

    def fetch(self, query, **kwargs):
        if 'MATCH (a:Article)--(ch:Chunk)--(c:Claim)' in query:
            return [{'claim_id':'c1','article_id':'a1'}]
        if 'MATCH (c:Claim) WHERE c.article_id IS NOT NULL' in query:
            return []
        if 'MATCH (ch:Chunk)--(c:Claim) WHERE ch.article_id IS NOT NULL' in query:
            return []
        if 'HAS_TOPIC' in query:
            return []
        return []


class QualityTests(unittest.TestCase):
    def test_quote_offsets(self):
        body = 'L’acqua è tornata. La rete idrica è funzionante.'
        quote = 'La rete idrica è funzionante.'
        p = body.index(quote)
        self.assertEqual(quality.check_evidence(body,{'body_start':p,'body_end':p+len(quote),'quote':quote}),'ok')
        self.assertEqual(quality.check_evidence(body,{'body_start':p,'body_end':p+len(quote),'quote':'errato'}),'quote_mismatch')
        self.assertEqual(quality.check_evidence(None,{}),None)

    def test_sampling_disjoint_reproducible(self):
        candidates = [{'claim_id':f'c{i:03}', 'family':'services',
                       'modality':'reported' if i%2 else 'alleged',
                       'subject_type':'Company','object_type':'Event',
                       'predicate':'OTHER' if i%4 == 0 else 'AFFECTS'} for i in range(100)]
        a,b = quality.sample_claims(candidates,10,5,42)
        v = list(a)+list(b)
        x,y = quality.sample_claims(candidates,10,5,42)
        self.assertEqual(v,list(x)+list(y))
        self.assertEqual(len(v),15)
        self.assertEqual(len({r['claim_id'] for r in v}),15)

    def test_judgment_validation(self):
        payload={k:'yes' for k in quality.FLAGS}
        payload.update(context_sufficient=True,error_type='none',note='OK')
        self.assertEqual(quality.normalize_judgment(payload)['supported'],'yes')
        payload['predicate_correct']='MAYBE'
        with self.assertRaises(ValueError):
            quality.normalize_judgment(payload)

    def test_wilson(self):
        lo,hi = quality.wilson(90,100)
        self.assertLess(lo,.9)
        self.assertGreater(hi,.9)

    def test_gemma_judge_and_resume(self):
        from unittest.mock import patch
        call_count = {'n':0}

        def fake_response(**params):
            call_count['n'] += 1
            judgment = {k:'yes' for k in quality.FLAGS}
            judgment.update({'context_sufficient':True,'error_type':'none','note':'Supportato'})
            choice = types.SimpleNamespace(finish_reason='stop',
                        message=types.SimpleNamespace(content=json.dumps(judgment)))
            return types.SimpleNamespace(choices=[choice])

        fake_client = types.SimpleNamespace(
            models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(
                data=[types.SimpleNamespace(id='unsloth/gemma-4-E4B-it-GGUF')])),
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_response)),
        )
        fake_openai = types.ModuleType('openai')
        fake_openai.OpenAI = lambda **kwargs: fake_client
        base = {'claims':[], 'facts':[]}
        for i in range(5):
            base['claims'].append({'id':f'c{i}', 'predicate':'MANAGES',
                                  'family':'services','modality':'reported',
                                  'quote':'GORI gestisce il servizio idrico integrato.'})
            base['facts'].append({'claim_id':f'c{i}', 'subject':'GORI',
                                  'object':'servizio idrico',
                                  'subject_type':'Company','object_type':'Service',
                                  'edge_predicate':'MANAGES'})
        args=types.SimpleNamespace(base_url='http://test/v1',api_key='test',
               timeout=60,model='auto',temperature=0.0,random=2,diagnostic=1,
               seed=42,retries=2,delay=0,max_tokens=512,json_mode=True,dry_run=False)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(sys.modules,{'openai':fake_openai}):
                report = quality.phase_semantic(MockStore(),base,Path(tmp),args)
                self.assertEqual(report['results']['random']['judged'],2)
                self.assertEqual(report['results']['diagnostic']['judged'],1)
                self.assertEqual(call_count['n'],3)
                report2 = quality.phase_semantic(MockStore(),base,Path(tmp),args)
                self.assertEqual(call_count['n'],3)  # All reused from disk.
                self.assertEqual(report2['pending'],0)
                self.assertTrue((Path(tmp)/'semantic_judgments.jsonl').exists())

    def test_audit_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            art = root/'Data'/'2026'/'article_0001.json'
            art.parent.mkdir(parents=True)
            body = 'GORI ha ripristinato il servizio idrico.'
            art.write_text(json.dumps({'body':body},ensure_ascii=False),encoding='utf-8')
            base = {
                'articles':[{'id':'a1','rel_path':'2026/article_0001.json','body':None}],
                'claims':[{'id':'c1','predicate':'RESTORES','predicate_raw':'RESTORES',
                           'family':'services','modality':'reported','quote':body,
                           'body_start':0,'body_end':len(body),'article_id':None}],
                'facts':[{'claim_id':'c1','edge_predicate':'RESTORES',
                          'subject':'GORI','object':'servizio idrico','subject_id':'g1',
                          'object_id':'g2','subject_labels':['GlobalEntity'],
                          'object_labels':['GlobalEntity']}],
                'entities':[{'id':'g1','element_id':'1','name':'GORI','kind':'Company'},
                            {'id':'g2','element_id':'2','name':'servizio idrico','kind':'Service'}],
                'events':[],
            }
            result = quality.phase_audit(MockStore(),base,root/'out',root/'Data')
            self.assertEqual(result['integrity']['offset_checked'],1)
            self.assertEqual(result['integrity']['offset_ok'],1)
            self.assertEqual(result['coverage']['target_articles'],1)
            res = quality.phase_resolution(MockStore(),base,root/'out')
            self.assertEqual(res['entity_nodes'],2)


if __name__ == '__main__':
    unittest.main()
