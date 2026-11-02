#!/usr/bin/env python3
"""PressKG quality suite (phases 1, 2, 3). Read-only Neo4j.

Uses the existing presskg.settings.CFG, neo4j and openai packages.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import sys
import time
import unicodedata
from dotenv import load_dotenv

from neo4j import GraphDatabase

try:
    from presskg.settings import CFG
except ImportError as exc:
    raise SystemExit("Eseguire con 'pixi run python scripts/kg_quality.py' dalla root del progetto") from exc

try:
    from presskg.entity_ontology import TYPES
except ImportError:
    TYPES = frozenset()
    
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / '.env', override=False)

VERSION = 'kg-quality-1.0'
JUDGE_VERSION = "news-claim-judge-v3-raw-context"
MODES = {'asserted','reported','alleged','denied','hypothetical','opinion','uncertain'}
FLAGS = ('subject_correct', 'object_correct', 'subject_type_correct', 'object_type_correct',
         'predicate_correct', 'direction_correct', 'modality_correct', 'attribution_correct', 'supported')


CLAIMS_QUERY = """
MATCH (c:Claim)
RETURN
    c.id AS id,
    c.article_id AS article_id,
    c.predicate AS predicate,
    c.predicate_raw AS predicate_raw,
    c.predicate_family AS family,
    c.modality AS modality,
    c.attribution AS attribution,
    c.quote AS quote,
    c.body_start AS body_start,
    c.body_end AS body_end,
    c.context_quote AS context_quote,
    c.context_body_start AS context_body_start,
    c.context_body_end AS context_body_end
"""

ARTICLES_QUERY = '''
MATCH (a:Article)
RETURN a.id AS id, a.rel_path AS rel_path, a.body AS body
'''

ENTITIES_QUERY = '''
MATCH (e:GlobalEntity)
RETURN elementId(e) AS element_id, e.id AS id,
       e.name AS name, e.kind AS kind
'''

EVENTS_QUERY = '''
MATCH (e:EventMention)
RETURN
    elementId(e) AS element_id,
    e.id AS id,
    e.name AS name,
    properties(e)['event_date'] AS event_date,
    e.article_id AS article_id
'''

FACTS_QUERY = '''
MATCH (s)-[r:FACT]->(o)
RETURN r.claim_id AS claim_id, r.predicate AS edge_predicate,
       coalesce(s.name, s.id, '') AS subject,
       coalesce(s.kind, CASE WHEN s:EventMention THEN 'Event' ELSE 'Other' END) AS subject_type,
       coalesce(o.name, o.id, '') AS object,
       coalesce(o.kind, CASE WHEN o:EventMention THEN 'Event' ELSE 'Other' END) AS object_type,
       s.id AS subject_id, o.id AS object_id,
       labels(s) AS subject_labels, labels(o) AS object_labels
'''

ARTICLE_CLAIM_QUERY = '''
MATCH (a:Article)--(ch:Chunk)--(c:Claim)
RETURN DISTINCT c.id AS claim_id, a.id AS article_id
'''

EVENT_TOPIC_QUERY = '''
MATCH (e:EventMention)-[r]->()
WHERE type(r) = 'HAS_TOPIC'
RETURN DISTINCT elementId(e) AS element_id
'''

def iso_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    temp.replace(path)


def write_csv(path: Path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, '') for c in columns})


def percentage(a, b):
    return round(100.0*a/b, 3) if b else None


def name_key(name):
    value = unicodedata.normalize('NFKD', str(name or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', re.sub(r'[^\w]+', ' ', value)).strip()


def alias_key(name):
    s = name_key(name)
    return re.sub(r'(?:\s+(?:s\s*p\s*a|spa|srl|s\s*r\s*l))+$', '', s).strip()


def counts_by(records, key):
    return dict(Counter(str(r.get(key) or 'MISSING') for r in records).most_common())


def find_body(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    if isinstance(data, dict):
        if isinstance(data.get('body'), str):
            return data['body']
        art = data.get('article')
        if isinstance(art, dict) and isinstance(art.get('body'), str):
            return art['body']
    return None


def safe_input_path(root: Path, rel):
    if not isinstance(rel, str) or not rel.strip():
        return None
    path = (root / rel).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def check_evidence(body, claim):
    """None if untestable; otherwise error code or 'ok'. Python offsets (code points)."""
    start, end, quote = claim.get('body_start'), claim.get('body_end'), claim.get('quote')
    if not isinstance(body, str):
        return None
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(quote, str):
        return 'missing_offset_or_quote'
    if start < 0 or end <= start or end > len(body):
        return 'invalid_offset'
    return 'ok' if body[start:end] == quote else 'quote_mismatch'


class Store:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            CFG.neo4j_uri,
            auth=(CFG.neo4j_user, CFG.neo4j_password),
            connection_timeout=20,
        )
        self.database = CFG.neo4j_database
        self.driver.verify_connectivity()

    def fetch(self, query, **params):
        with self.driver.session(database=self.database) as session:
            return session.run(query, **params).data()

    def close(self):
        self.driver.close()


def article_map(store):
    mapping = {}
    for row in store.fetch(ARTICLE_CLAIM_QUERY):
        if row.get('claim_id') and row.get('article_id'):
            mapping.setdefault(row['claim_id'], row['article_id'])
    if not mapping:
        print('[WARN] Non trovata la catena Article--Chunk--Claim. Provo article_id su Claim/Chunk.')
    for query in (
        '''MATCH (c:Claim) WHERE c.article_id IS NOT NULL
           RETURN c.id AS claim_id, c.article_id AS article_id''',
        '''MATCH (ch:Chunk)--(c:Claim) WHERE ch.article_id IS NOT NULL
           RETURN DISTINCT c.id AS claim_id, ch.article_id AS article_id''',
    ):
        for row in store.fetch(query):
            if row.get('claim_id') and row.get('article_id'):
                mapping.setdefault(row['claim_id'], row['article_id'])
    return mapping


def get_base(store):
    print('Lettura nodi/relazioni da Neo4j (sola lettura)...', flush=True)
    return {
        'articles': store.fetch(ARTICLES_QUERY),
        'claims': store.fetch(CLAIMS_QUERY),
        'facts': store.fetch(FACTS_QUERY),
        'entities': store.fetch(ENTITIES_QUERY),
        'events': store.fetch(EVENTS_QUERY),
    }


def semantic_components(entities, events, facts):
    """Weakly connected components on FACT subgraph only (no Article/Claim hubs)."""
    nodes = {str(e['id']) for e in entities + events if e.get('id') is not None}
    parent = {nid:nid for nid in nodes}
    degree = Counter()

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f in facts:
        s,o = f.get('subject_id'),f.get('object_id')
        if s is None or o is None:
            continue
        s,o = str(s),str(o)
        if s not in nodes or o not in nodes:
            continue
        degree[s] += 1
        degree[o] += 1
        parent[root(s)] = root(o)
    sizes = Counter(root(nid) for nid in nodes)
    largest = max(sizes.values(),default=0)
    return {
        'nodes_with_global_ids':len(nodes),
        'weakly_connected_components':len(sizes),
        'largest_component_nodes':largest,
        'largest_component_percent':percentage(largest,len(nodes)),
        'isolated_nodes':sum(not degree[nid] for nid in nodes),
        'note':'Calcolato esclusivamente sugli archi FACT tra entita/eventi: senza Article, Chunk e Claim.'
    }


def phase_audit(store, base, dest, data_dir: Path, expected=None):
    print('Fase 1: audit strutturale, provenance e offset...', flush=True)
    arts, claims, facts, entities, events = (base[k] for k in
                                              ('articles','claims','facts','entities','events'))
    by_claim = {c['id']: c for c in claims if c.get('id')}
    by_art = {a['id']: a for a in arts if a.get('id')}
    c_to_a = article_map(store)
    for c in claims:
        if c.get('article_id') and c.get('id'):
            c_to_a.setdefault(c['id'], c['article_id'])
    facts_ids = Counter(str(f.get('claim_id')) for f in facts if f.get('claim_id'))
    orphan = [c for c in claims if not facts_ids.get(str(c.get('id')))]
    missing_claim = [f for f in facts if f.get('claim_id') not in by_claim]
    bad_predicate = [f for f in facts if f.get('claim_id') in by_claim and
                     f.get('edge_predicate') and by_claim[f['claim_id']].get('predicate') and
                     f['edge_predicate'] != by_claim[f['claim_id']]['predicate']]

    source_files = len(list(data_dir.rglob('article_*.json'))) if data_dir.is_dir() else None
    target = expected if expected is not None else source_files
    body_by_art = {}
    unavailable = 0
    offsets = Counter()
    errors = []
    for c in claims:
        aid = c_to_a.get(c.get('id'))
        if aid is None or aid not in by_art:
            unavailable += 1
            continue
        if aid not in body_by_art:
            a = by_art[aid]
            filepath = safe_input_path(data_dir, a.get('rel_path'))
            body = find_body(filepath) if filepath else None
            if body is None and isinstance(a.get('body'), str):
                body = a['body']
            body_by_art[aid] = body
        status = check_evidence(body_by_art[aid], c)
        if status is None:
            unavailable += 1
            continue
        offsets[status] += 1
        if status != 'ok':
            errors.append({'claim_id':c.get('id'), 'article_id':aid,
                           'rel_path':by_art[aid].get('rel_path'), 'status':status,
                           'body_start':c.get('body_start'), 'body_end':c.get('body_end'),
                           'quote':c.get('quote')})
    write_csv(dest/'offset_errors.csv', errors,
              ['claim_id','article_id','rel_path','status','body_start','body_end','quote'])
    write_csv(dest/'orphan_claims.csv', orphan,
              ['id','predicate','quote','body_start','body_end'])
    write_csv(dest/'predicate_distribution.csv',
              [{'predicate':k, 'count':v} for k,v in Counter(c.get('predicate') or 'MISSING' for c in claims).most_common()],
              ['predicate','count'])
    write_csv(dest/'entity_type_distribution.csv',
              [{'kind':k, 'count':v} for k,v in Counter(e.get('kind') or 'MISSING' for e in entities).most_common()],
              ['kind','count'])
    topic_edges = store.fetch(EVENT_TOPIC_QUERY)
    attached_topics = {v['element_id'] for v in topic_edges}
    other_n = sum(1 for c in claims if c.get('predicate') == 'OTHER')
    used_articles = set(c_to_a.values())
    claim_count_by_article = Counter(aid for cid,aid in c_to_a.items() if cid in by_claim)
    claim_values = sorted(claim_count_by_article.values())
    claim_p95 = claim_values[min(len(claim_values)-1, math.ceil(.95*len(claim_values))-1)] if claim_values else None
    output = {
        'version': VERSION, 'timestamp': iso_now(), 'database': store.database,
        'counts': {'articles':len(arts), 'claims':len(claims), 'facts':len(facts),
                   'global_entities':len(entities), 'event_mentions':len(events),
                   'articles_with_mapped_claim':len(used_articles & set(by_art)),
                   'articles_without_mapped_claim':len(set(by_art)-used_articles),
                   'claims_per_article_with_claim_mean':round(statistics.mean(claim_values),3) if claim_values else None,
                   'claims_per_article_with_claim_median':statistics.median(claim_values) if claim_values else None,
                   'claims_per_article_with_claim_p95':claim_p95},
        'coverage': {'input_article_files':source_files, 'target_articles':target,
                     'articles_ingested_percent':percentage(len(arts),target)},
        'integrity': {'orphan_claims':len(orphan), 'fact_edges_without_claim':len(missing_claim),
                      'fact_predicate_mismatches':len(bad_predicate),
                      'claims_without_article_mapping':len(claims)-len(set(c_to_a) & set(by_claim)),
                      'offset_checked':sum(offsets.values()), 'offset_ok':offsets.get('ok',0),
                      'offset_unverifiable':unavailable,
                      'offset_error_counts':dict(offsets - Counter({'ok':offsets.get('ok',0)})),
                      'offset_exact_percent':percentage(offsets.get('ok',0),sum(offsets.values()))},
        'ontology': {'other_claims':other_n,
                     'other_percent':percentage(other_n,len(claims)),
                     'entity_kinds':counts_by(entities,'kind'),
                     'invalid_entity_kind_nodes':sum(e.get('kind') not in TYPES for e in entities) if TYPES else None,
                     'predicates':counts_by(claims,'predicate'),
                     'modality':counts_by(claims,'modality')},
        'semantic_structure':semantic_components(entities,events,facts),
        'topics': {'events_with_has_topic':len(attached_topics),
                   'event_topic_coverage_percent':percentage(len(attached_topics),len(events)),
                   'note':'Valutare solo dopo enrichment HAS_TOPIC; senza enrichment non e una misura di estrazione.'},
        'quality_flags': {
            'offset_test_incomplete':unavailable > 0,
            'grounding_integrity_failed':len(errors)>0,
            'unmatched_claims':bool(orphan or missing_claim),
        },
    }
    write_json(dest/'audit.json',output)
    print(f"  Articoli {len(arts)}/{target or '?'}; claim {len(claims)}; "
          f"offset esatti {offsets.get('ok',0)}/{sum(offsets.values())} "
          f"(non verificabili {unavailable}); OTHER {percentage(other_n,len(claims))}%")
    return output



JUDGE_PROMPT = """
Sei un valutatore indipendente di relazioni estratte
da articoli giornalistici italiani.

Riceverai una relazione composta da:
- subject e relativo tipo
- predicate
- object e relativo tipo
- modality
- attribution
- evidence (testo originale)

Devi VALUTARE la correttezza della relazione
rispetto all'evidenza, NON estrarre nuovamente
le informazioni.

Restituisci esclusivamente JSON valido.

CAMPI OBBLIGATORI

I seguenti 9 campi sono GIUDIZI DI CORRETTEZZA:

subject_correct
object_correct
subject_type_correct
object_type_correct
predicate_correct
direction_correct
modality_correct
attribution_correct
supported

Per OGNUNO di questi campi sono consentiti
ESCLUSIVAMENTE tre valori stringa:

"yes"       = corretto e supportato
"no"        = scorretto o contraddetto
"uncertain" = evidenza insufficiente

Non utilizzare MAI altri valori.

SIGNIFICATO DEI CAMPI

subject_correct:
Il soggetto della relazione è identificato
correttamente nell'evidenza?

object_correct:
L'oggetto è identificato correttamente?

subject_type_correct:
Il tipo assegnato al soggetto è appropriato?

object_type_correct:
Il tipo assegnato all'oggetto è appropriato?

predicate_correct:
La relazione indicata dal predicato è
effettivamente supportata dal testo?

direction_correct:
Il verso subject -> predicate -> object
è semanticamente corretto?

modality_correct:
La modalità assegnata è appropriata?
Valuta correttamente accuse, smentite,
ipotesi, opinioni e fatti riportati.

ATTENZIONE:
"asserted", "reported", "alleged", "denied",
"hypothetical" e "opinion" sono possibili
modalità di INPUT.
NON sono valori validi per modality_correct.

attribution_correct:
L'attribuzione presente nel claim è corretta?

Se attribution è null:
- "yes" se non serve un'attribuzione specifica;
- "no" se manca una fonte necessaria;
- "uncertain" se non è possibile stabilirlo.

ATTENZIONE:
null, "null", "none" e "" NON sono risposte
valide per attribution_correct.

supported:
L'intera affermazione è semanticamente
supportata dall'evidenza, considerando
anche modalità e attribuzione?

ALTRI CAMPI OBBLIGATORI

context_sufficient:
Booleano JSON true o false.
Non utilizzare stringhe.

error_type:
Stringa con il principale tipo di errore.
Usa "none" quando non ci sono errori.

note:
Breve spiegazione in italiano, massimo
160 caratteri.

REGOLE SEMANTICHE

1. Usa soltanto il testo dell'evidenza.
2. Non utilizzare conoscenze esterne.
3. La presenza di due entità non dimostra
   automaticamente una relazione.
4. Un'accusa non è un fatto accertato.
5. Una smentita non conferma il fatto smentito.
6. Un'azione annunciata non è necessariamente
   un'azione già avvenuta.
7. Non assegnare "yes" senza supporto testuale.
8. In caso di ambiguità usa "uncertain".
9. Valuta ogni campo indipendentemente.

VINCOLI FINALI

Restituisci esattamente 12 campi:
9 giudizi, context_sufficient, error_type, note.

I 9 giudizi devono essere SEMPRE stringhe
"yes", "no" o "uncertain".

Non copiare nei giudizi i valori dei campi
originali del claim.

Non restituire null nei giudizi.
Non aggiungere altri campi.
Non aggiungere testo prima o dopo il JSON.
"""

JUDGE_PROMPT += """

REGOLE SUL CONTESTO E SUL PREDICATO

Ricevi evidence, context e context_evidence.

- evidence è il segmento principale che
  dovrebbe dimostrare la relazione.
- context contiene testo circostante dello
  stesso articolo, con offset verificati.
- context_evidence può identificare il
  referente implicito o la fonte.

Usa il contesto per disambiguare soggetti,
oggetti, attribuzioni e riferimenti pronominali.

Non considerare supportata una relazione
soltanto perché soggetto e oggetto compaiono
nello stesso contesto.

Se predicate_canonical è OTHER, valuta il
predicato originale indicato in predicate,
non l'etichetta OTHER.

Distingui sempre:
- relazione realmente espressa;
- semplice partecipazione;
- azione soltanto annunciata;
- responsabilità accertata;
- responsabilità soltanto contestata;
- opinione attribuita a una fonte.

Se il contesto non è sufficiente per decidere,
utilizza uncertain.

Restituisci esclusivamente i dodici campi
previsti dallo schema JSON del giudice.
"""


def judge_key(row, model, temp):
    source = {'v':JUDGE_VERSION,'prompt_hash':hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),'model':model,'temperature':temp,
              'claim':{k:row.get(k) for k in ('claim_id','subject','subject_type','object',
                    'object_type','predicate', 'predicate_raw', 'predicate_effective','modality','attribution','evidence', "context", "context_status", "context_evidence")}}
    return hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def normalize_judgment(value):
    if not isinstance(value, dict):
        raise ValueError('Risposta non e un oggetto JSON')
    result = {}
    aliases = {True:'yes',False:'no','true':'yes','false':'no',
               'si':'yes','sì':'yes','no':'no','yes':'yes',
               'uncertain':'uncertain','unknown':'uncertain','non_so':'uncertain',
               'not_enough_context':'uncertain'}
    for key in FLAGS:
        raw = value.get(key)
        v = aliases.get(raw if isinstance(raw,bool) else str(raw).strip().lower())
        if v is None:
            raise ValueError(f'Campo non valido: {key}={raw!r}')
        result[key] = v
    if not isinstance(value.get('context_sufficient'),bool):
        raise ValueError('context_sufficient deve essere bool')
    result['context_sufficient'] = value['context_sufficient']
    result['error_type'] = str(value.get('error_type','unknown'))[:80]
    result['note'] = str(value.get('note',''))[:250]
    return result


def parse_json_text(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    a, b = text.find('{'), text.rfind('}')
    if a < 0 or b < a:
        raise ValueError('Oggetto JSON non presente')
    sub = text[a:b+1]
    try:
        return json.loads(sub)
    except json.JSONDecodeError:
        try:
            from json_repair import loads as repair_loads
        except ImportError:
            raise ValueError('JSON malformato: installare json-repair')
        return repair_loads(sub)


def sample_claims(rows, n_random, n_diagnostic, seed):
    """Random sample gives prevalence estimates; diagnostic sample is deliberately biased."""
    rows = sorted({r['claim_id']:r for r in rows if r.get('claim_id')}.values(),
                  key=lambda r:r['claim_id'])
    rng = random.Random(seed)
    n_random = min(n_random,len(rows))
    uniform = rng.sample(rows,n_random)
    taken = {r['claim_id'] for r in uniform}
    remaining = [r for r in rows if r['claim_id'] not in taken]
    counts = Counter((r.get('family'),r.get('modality'),r.get('subject_type'),r.get('object_type'))
                     for r in rows)
    # Upweight unusual categories, allegations/denials and OTHER.
    weighted = []
    for r in remaining:
        key = (r.get('family'),r.get('modality'),r.get('subject_type'),r.get('object_type'))
        rarity = 1 / math.sqrt(max(1,counts[key]))
        multiplier = (2.5 if r.get('predicate') == 'OTHER' else 1.0)
        if r.get('modality') in ('alleged','denied','hypothetical'):
            multiplier *= 2
        if 'Event' in (r.get('subject_type'),r.get('object_type')):
            multiplier *= 1.5
        weight = max(.0001,rarity*multiplier)
        # Weighted sampling without replacement, deterministic with seed.
        rank = -math.log(max(1e-12,rng.random()))/weight
        weighted.append((rank,r))
    weighted.sort(key=lambda x:x[0])
    diagnostic = [r for _,r in weighted[:n_diagnostic]]
    return ({**r,'sample_group':'random'} for r in uniform), \
           ({**r,'sample_group':'diagnostic'} for r in diagnostic)


def wilson(yes, total, z=1.96):
    if not total:
        return None
    p = yes/total
    den = 1+z*z/total
    mid = (p+z*z/(2*total))/den
    spread = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [round(max(0,mid-spread),4),round(min(1,mid+spread),4)]


def summarize_judgments(records):
    out = {}
    for group in ('random','diagnostic'):
        rows = [r['judgment'] for r in records if r.get('sample_group') == group and r.get('judgment')]
        by_field = {}
        for field in FLAGS:
            cnt = Counter(j[field] for j in rows)
            decidable = cnt['yes']+cnt['no']
            by_field[field] = {
                'yes':cnt['yes'], 'no':cnt['no'], 'uncertain':cnt['uncertain'],
                'decidable':decidable, 'yes_percent_decidable':percentage(cnt['yes'],decidable),
                'decidable_percent':percentage(decidable,len(rows)),
                'wilson95_sampling_only':wilson(cnt['yes'],decidable) if group == 'random' else None,
            }
        out[group] = {'judged':len(rows),'dimensions':by_field,
                        'context_insufficient':sum(not x['context_sufficient'] for x in rows)}
    return out

def attach_semantic_context(chosen, base, context_chars=350):
    #Recupera il contesto dle body originale da Neo4J
    bodies = {
        article["id"]: article.get("body") for article in base["articles"]
    }
    
    for row in chosen:
        body = bodies.get(row.get("article_id"))
        
        start = row.get("body_start")
        end = row.get("body_end")
        evidence = row.get("evidence")
        
        valid = (isinstance(body, str) and isinstance(start, int) and isinstance(end, int) and isinstance(evidence, str) and
                    0 <= start < end <= len(body) and body[start:end] == evidence)
        
        if not valid:
            row["context"] = evidence or ""
            row["context_status"] = "unverifiable"
            row["context_evidence"] = ""
            continue
            
        left = max(0, start - context_chars)
        right = min(len(body), end + context_chars)
        
        row["context"] = body[left:right]
        row["context_status"] = "verified"
        
        cs = row.get("context_body_start")
        ce = row.get("context_body_end")
        cq = row.get("context_quote")
        
        if (isinstance(cs, int) and isinstance(ce, int) and isinstance(cq, str) and 0 <= cs < ce <= len(body) and body[cs:ce] == cq):
            row["context_evidence"] = cq
        else:
            row["context_evidence"] = ""
        
    return chosen
    

def phase_semantic(store, base, dest, args):
    print('Fase 2: sample e LLM judge...', flush=True)
    from openai import OpenAI
    client = None
    available = []
    model = args.model
    if not args.dry_run:
        client = OpenAI(base_url=args.base_url,api_key=args.api_key,timeout=args.timeout,max_retries=0)
        try:
            available = [m.id for m in client.models.list().data]
            print('  Modelli su endpoint:', available[:12])
        except Exception as exc:
            print(f'  [WARN] /v1/models non disponibile: {exc}')
    print('  Modello giudice:',model)

    claim_lookup = {c['id']:c for c in base['claims'] if c.get('id')}
    rows = {}
    for edge in base['facts']:
        cid = edge.get('claim_id')
        if cid not in claim_lookup or cid in rows:
            continue
        c = claim_lookup[cid]
        if not c.get('quote'):
            continue
        rows[cid] = {
            'claim_id':cid,'subject':edge['subject'],'subject_type':edge['subject_type'],
            'object':edge['object'],'object_type':edge['object_type'],
            'predicate':c.get('predicate') or edge.get('edge_predicate'),
            'family':c.get('family') or 'unmapped',
            'modality':c.get('modality') or 'uncertain',
            'attribution':c.get('attribution'), 'evidence':c['quote'],
            "predicate_raw": c.get("predicate_raw"),
            "article_id": c.get("article_id"),
            "predicate_raw": c.get("predicate_raw"),
            "predicate_effective": (c["predicate_raw"] if (c.get("predicate") == "OTHER" and c.get("predicate_raw") and c.get("predicate_raw") != "OTHER") else (c.get("predicate") or edge.get("edge_predicate"))),
            "body_start": c.get("body_start"),
            "body_end": c.get("body_end"),
            "context_quote": c.get("context_quote"),
            "context_body_start": c.get("context_body_start"),
            "context_body_end": c.get("context_body_end"),
        }
    if not rows:
        raise RuntimeError('Non ci sono FACT->Claim con quote. Controllare lo schema.')
    random_rows, diag_rows = sample_claims(list(rows.values()), args.random, args.diagnostic, args.seed)
    chosen = list(random_rows)+list(diag_rows)
    chosen.sort(key=lambda r:(r['sample_group'],r['claim_id']))
    chosen = attach_semantic_context(chosen, base, context_chars=args.judge_context_chars)
    sample_csv = dest/'semantic_sample.csv'
    cols = ['sample_group','claim_id','subject','subject_type','predicate','object','object_type',
            'family','modality','attribution','evidence', "article_id", "predicate_raw", "predicate_effective", "context", "context_status", "context_evidence"]
    write_csv(sample_csv,chosen,cols)

    response_path = dest/'semantic_judgments.jsonl'
    cached = {}
    if response_path.exists():
        with response_path.open(encoding='utf-8') as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if rec.get('key') and rec.get('judgment'):
                        cached[rec['key']] = rec
                except ValueError:
                    pass
    judged, errors = [], []
    total = len(chosen)
    try:
        from tqdm import tqdm
        progress = tqdm(chosen,desc='evaluation',unit='claim')
    except ImportError:
        progress = chosen
    with response_path.open('a',encoding='utf-8') as fh:
        for n,row in enumerate(progress,1):
            key = judge_key(row,model,args.temperature)
            if key in cached:
                record = cached[key].copy()
                record['sample_group'] = row['sample_group']
                judged.append(record)
                continue
            if args.dry_run:
                continue
            try:
                judgement = None
                last = ''
                for attempt in range(args.retries):
                    try:
                        params = {
                            'model':model,
                            'messages':[{'role':'system','content':JUDGE_PROMPT},
                                        {'role':'user','content':json.dumps({
                                            'evidence':row['evidence'], 'context':row["context"], 'context_status':row["context_status"], 'context_evidence':row["context_evidence"],
                                            'subject':{'name':row['subject'],'type':row['subject_type']},
                                            "predicate": row["predicate_effective"], "predicate_canonical": row["predicate"], "predicate_raw": row["predicate_raw"],
                                            'object':{'name':row['object'],'type':row['object_type']},
                                            'modality':row['modality'],
                                            'attribution':row['attribution'],
                                        },ensure_ascii=False)}],
                            'temperature':args.temperature,
                            'max_tokens':args.max_tokens,
                        }
                        if args.json_mode:
                            params['response_format'] = {'type':'json_object'}
                            params['extra_body'] = {
                                'enable_thinking': False,
                                'chat_template_kwargs': {
                                    'enable_thinking': False,
                                },
                            }
                        response = client.chat.completions.create(**params)
                        choice = response.choices[0]

                        if choice.finish_reason == "length":
                            usage = response.usage
                            print(
                                "[TRUNCATED]",
                                "model=", model,
                                "output_tokens=",
                                usage.completion_tokens if usage else "unknown",
                                file=sys.stderr,
                            )
                            
                        if choice.finish_reason == 'length':
                            raise ValueError('Output del judge troncato')
                        judgement = normalize_judgment(parse_json_text(choice.message.content or ''))
                        break
                    except Exception as exc:
                        last = repr(exc)
                        if attempt < args.retries-1:
                            time.sleep(1)
                            
                if judgement is None:
                    raise RuntimeError(last)
                
                record = {'key':key,'claim_id':row['claim_id'],
                            'sample_group':row['sample_group'],'model':model,
                            'judge_version':JUDGE_VERSION,'timestamp':iso_now(),
                            'judgment':judgement}
                
                fh.write(json.dumps(record,ensure_ascii=False)+'\n')
                fh.flush()
                judged.append(record)
            except Exception as exc:
                errors.append({'claim_id':row['claim_id'],'error':repr(exc)})
                print(f'  [WARN] LLM claim={row["claim_id"]}: {exc}',file=sys.stderr)
            if args.delay:
                time.sleep(args.delay)
                
    write_csv(dest/'semantic_errors.csv',errors,['claim_id','error'])
    judgment_by_id = {rec['claim_id']:rec['judgment'] for rec in judged}
    review = [dict(r,**(judgment_by_id.get(r['claim_id']) or {})) for r in chosen]
    write_csv(dest/'semantic_review.csv',review,cols+list(FLAGS)+['context_sufficient','error_type','note'])
    summary = {
        'version':VERSION,'timestamp':iso_now(),'model':model,
        'base_url':args.base_url,'prompt_sha256':hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
        'population_claims_with_fact_evidence':len(rows),'requested':total,
        'results':summarize_judgments(judged),'failures':len(errors),
        'pending':max(0,total-len(judged)),
        'interpretation': 'Stime del giudice LLM, NON verita-terreno. Il campione diagnostic e sovracampionato e non va aggregato al random per una stima della precisione globale.',
    }
    write_json(dest/'semantic_summary.json',summary)
    print('  Valutati:',len(judged),'di',total,'; errori:',len(errors))
    return summary


def phase_resolution(store, base, dest):
    print('Fase 3: possibili errori di Entity/Event Resolution...', flush=True)
    entities,events,facts = base['entities'],base['events'],base['facts']
    by_name = defaultdict(list)
    aliases = defaultdict(list)
    for e in entities:
        if e.get('name'):
            by_name[name_key(e['name'])].append(e)
            aliases[(alias_key(e['name']),e.get('kind'))].append(e)
    duplicate = []
    collision = []
    for normalized, group in by_name.items():
        uniq_ids = {e['element_id'] for e in group}
        if len(uniq_ids) > 1:
            duplicate.append({'normalized':normalized,'n':len(uniq_ids),
                              'kinds':','.join(sorted(set(str(e.get('kind')) for e in group))),
                              'names':' | '.join(sorted(set(str(e.get('name')) for e in group))[:8]),
                              'ids':' | '.join(sorted(str(e.get('id')) for e in group)[:10])})
        kinds = set(str(e.get('kind')) for e in group)
        if len(kinds)>1:
            collision.append({'normalized':normalized,'kinds':','.join(sorted(kinds)),
                              'names':' | '.join(sorted(set(str(e.get('name')) for e in group))[:8]),
                              'n':len(uniq_ids)})
    alias_candidates = []
    for (normalized,kind),group in aliases.items():
        names = set(name_key(e['name']) for e in group)
        if len(names)>1 and normalized:
            alias_candidates.append({'alias_key':normalized,'kind':kind,'n':len(group),
                                     'names':' | '.join(sorted(set(e['name'] for e in group))[:12])})
    write_csv(dest/'entity_duplicates.csv',duplicate,['normalized','n','kinds','names','ids'])
    write_csv(dest/'cross_type_collisions.csv',collision,['normalized','n','kinds','names'])
    write_csv(dest/'entity_alias_candidates.csv',alias_candidates,['alias_key','kind','n','names'])

    # Use FACT->Claim->Article mapping if the event itself lacks article_id.
    claimed_in = article_map(store)
    by_event = {e.get('id'):e for e in events if e.get('id')}
    evt_docs = defaultdict(set)
    mapped_event_ids = set()
    evt_events = defaultdict(set)
    evt_uses = Counter()
    for f in facts:
        cid = f.get('claim_id')
        article = claimed_in.get(cid)
        for endpoint in ('subject','object'):
            if 'EventMention' not in (f.get(endpoint+'_labels') or []):
                continue
            eid = f.get(endpoint+'_id')
            if eid is None:
                continue
            evt_uses[eid] += 1
            ev = by_event.get(eid)
            if ev:
                if article is None:
                    article = ev.get('article_id')
                if article is not None:
                    evt_docs[article].add(eid)
                    mapped_event_ids.add(eid)
                    evt_events[(article,name_key(ev.get('name')))].add(eid)
    same_article = []
    for (aid,normalized),ids in evt_events.items():
        if len(ids)>1 and normalized:
            same_article.append({'article_id':aid,'event_name_normalized':normalized,
                                 'mentions':len(ids),'event_ids':' | '.join(sorted(map(str,ids)))})
    write_csv(dest/'event_same_article_candidates.csv',same_article,
              ['article_id','event_name_normalized','mentions','event_ids'])

    # Repeated event names across articles are candidates, NOT definitely duplicates.
    enames = defaultdict(list)
    for e in events:
        if e.get('name'):
            enames[name_key(e['name'])].append(e)
    cross_doc = []
    for normalized,group in enames.items():
        if len(group) >= 3 and normalized and len(normalized)>=8:
            cross_doc.append({'normalized':normalized,'mentions':len(group),
                              'names':' | '.join(sorted(set(e['name'] for e in group))[:5])})
    cross_doc.sort(key=lambda r:-r['mentions'])
    write_csv(dest/'event_repeated_names.csv',cross_doc,['normalized','mentions','names'])

    degree = Counter()
    for f in facts:
        for ep in ('subject','object'):
            eid = f.get(ep+'_id')
            if eid:
                degree[eid] += 1
    degrees = sorted(degree.values())
    p99 = degrees[min(len(degrees)-1,int(len(degrees)*.99))] if degrees else 0
    threshold = max(30,p99)
    hubs = []
    for e in entities:
        d = degree.get(e.get('id'),0)
        if d >= threshold:
            hubs.append({'id':e.get('id'),'name':e.get('name'),
                         'kind':e.get('kind'),'degree':d})
    hubs.sort(key=lambda x:-x['degree'])
    write_csv(dest/'high_degree_entities.csv',hubs,['id','name','kind','degree'])
    suspicious = []
    for e in events:
        name = name_key(e.get('name'))
        if name in {'evento','intervento','problema','acqua','balneazione','servizio idrico',
                    'lavori','evento infrastrutturale idrico'} or re.search(r'\b(?:xxx|placeholder|nome evento|esempio)\b',name):
            suspicious.append({'id':e.get('id'),'name':e.get('name'),
                               'reason':'generic_or_placeholder_event_name'})
    write_csv(dest/'suspicious_events.csv',suspicious,['id','name','reason'])
    report = {
        'version':VERSION,'timestamp':iso_now(),
        'entity_nodes':len(entities),'event_mentions':len(events),
        'duplicate_normalized_global_entity_names':len(duplicate),
        'cross_type_same_name_candidates':len(collision),
        'same_kind_alias_candidates':len(alias_candidates),
        'same_article_event_name_candidates':len(same_article),
        'repeated_event_names_at_least_3_mentions':len(cross_doc),
        'event_with_generic_or_placeholder_name':len(suspicious),
        'high_degree_threshold':threshold,'high_degree_entities':len(hubs),
        'events_with_article_mapping':len(mapped_event_ids),
        'event_article_mapping_coverage_percent':percentage(len(mapped_event_ids),len(events)),
        'note': ('Le tabelle raccolgono CANDIDATI da ispezionare; senza gold standard non '
                 'si possono misurare false-merge rate, under-merging recall o precision effettivi.')
    }
    write_json(dest/'resolution_report.json',report)
    print('  Gruppi nomi duplicati',len(duplicate),'alias',len(alias_candidates),
          'eventi stesso articolo',len(same_article),'hubs',len(hubs))
    return report


def report_markdown(reports, dest):
    lines = ['# PressKG — Quality Report (fasi 1–3)', '',
             f'Data: {iso_now()}', '',
             'Le percentuali semantiche sono giudizi del modello, non etichette gold.', '']
    if 'audit' in reports:
        r=reports['audit']
        lines += ['## Fase 1 — Audit strutturale', '',
                  f"- Articoli acquisiti: **{r['counts']['articles']}** / {r['coverage']['target_articles'] or 'non disponibile'}",
                  f"- Claim: **{r['counts']['claims']}**; archi FACT: **{r['counts']['facts']}**",
                  f"- Offset esatti: **{r['integrity']['offset_ok']} / {r['integrity']['offset_checked']}**",
                  f"- Claim non verificabili per assenza di body/mapping: **{r['integrity']['offset_unverifiable']}**",
                  f"- Predicati OTHER: **{r['ontology']['other_percent']}%**",
                  f"- Componenti debolmente connesse: **{r['semantic_structure']['weakly_connected_components']}**",
                  f"- EventMention con topic: **{r['topics']['event_topic_coverage_percent']}%** (solo se enrichment eseguito)", '']
    if 'semantic' in reports:
        r=reports['semantic']
        rnd=r['results']['random']
        sup=rnd['dimensions']['supported']
        lines += ['## Fase 2 — Valutazione semantica con LLM differente', '',
                  f"- Modello: `{r['model']}`",
                  f"- Risposte random: **{rnd['judged']}**; diagnostic: **{r['results']['diagnostic']['judged']}**",
                  f"- Claim supportati sui casi decidibili, random: **{sup['yes_percent_decidable']}%**",
                  f"- Decidibili: **{sup['decidable']}**; incerti: **{sup['uncertain']}**",
                  f"- Errore chiamate/parsing: **{r['failures']}**; rimanenti: **{r['pending']}**",
                  '- **Attenzione:** il campione diagnostic è selezionato con bias intenzionale e non va sommato al random.',
                  '- Validare manualmente una piccola porzione delle decisioni LLM prima di usare questi numeri come qualità effettiva.', '']
    if 'resolution' in reports:
        r=reports['resolution']
        lines += ['## Fase 3 — Entity/Event Resolution (candidati)', '',
                  f"- Nomi globali duplicati: **{r['duplicate_normalized_global_entity_names']}** gruppi",
                  f"- Collisioni fra tipi: **{r['cross_type_same_name_candidates']}**",
                  f"- Alias della stessa categoria: **{r['same_kind_alias_candidates']}**",
                  f"- Possibili eventi duplicati nello stesso articolo: **{r['same_article_event_name_candidates']}**",
                  f"- Entità ad alto grado: **{r['high_degree_entities']}**",
                  '- Questi sono casi sospetti, **non** tassi di errore validati.', '']
    lines += ['## Output', '',
              'Report e CSV sono nella stessa directory di questo file.', '']
    (dest/'quality_report.md').write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fasi 1-3 di valutazione Press KG, senza scritture su Neo4j.')
    parser.add_argument('phase',choices=('audit','semantic','resolution','all'))
    parser.add_argument('--out',default='output/evaluations/v3_2')
    parser.add_argument('--data-dir',default=str(getattr(CFG,'data_dir','Data')))
    parser.add_argument('--expected-articles',type=int,default=None)
    parser.add_argument('--random',type=int,default=120,help='Campione casuale per stima semantica')
    parser.add_argument('--diagnostic',type=int,default=40,help='Campione sovracampionato per diagnosi')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--base-url',default=os.environ.get('UNSLOTH_BASE_URL','http://127.0.0.1:8888/v1'))
    parser.add_argument('--api-key',default=os.environ.get('UNSLOTH_API_KEY','local'))
    parser.add_argument('--model',default=os.environ.get('UNSLOTH_EVAL_MODEL','auto'))
    parser.add_argument('--temperature',type=float,default=0.0)
    parser.add_argument('--max-tokens',type=int,default=1000)
    parser.add_argument('--timeout',type=float,default=180)
    parser.add_argument('--retries',type=int,default=3)
    parser.add_argument('--delay',type=float,default=0)
    parser.add_argument('--no-json-mode',action='store_false',dest='json_mode')
    parser.add_argument('--dry-run',action='store_true',help='Prepara campione senza chiamare LLM')
    parser.add_argument("--judge-context-chars", type=int, default=350)
    args = parser.parse_args(argv)
    if args.random < 0 or args.diagnostic < 0 or args.retries < 1:
        parser.error('--random/--diagnostic devono essere >=0; --retries >=1')
    out = Path(args.out)
    out.mkdir(parents=True,exist_ok=True)
    manifest = {'suite_version':VERSION,'timestamp':iso_now(),'phase':args.phase,
                'neo4j_uri':CFG.neo4j_uri,'neo4j_database':CFG.neo4j_database,
                'data_dir':args.data_dir,'judge_model_requested':args.model,
                'seed':args.seed,'random':args.random,'diagnostic':args.diagnostic}
    write_json(out/'run_manifest.json',manifest)
    store = Store()
    try:
        base = get_base(store)
        reports = {}
        if args.phase in ('audit','all'):
            reports['audit'] = phase_audit(store,base,out,Path(args.data_dir),args.expected_articles)
        if args.phase in ('semantic','all'):
            reports['semantic'] = phase_semantic(store,base,out,args)
        if args.phase in ('resolution','all'):
            reports['resolution'] = phase_resolution(store,base,out)
        report_markdown(reports,out)
        write_json(out/'summary.json',{
            'version':VERSION,'timestamp':iso_now(),'phases':reports,
            'notes':['Neo4j usato esclusivamente in lettura.',
                     'Il giudizio semantico di LLM e una stima, non ground truth.',
                     'Event/Entity resolution senza gold set produce solo candidati.']})
    finally:
        store.close()
    print('\nCompletato. Risultati in:',out.resolve())
    return 0


if __name__ == '__main__':
    sys.exit(main())
