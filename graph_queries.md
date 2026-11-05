## delete all
:auto
MATCH (n)
CALL (n) {
    DETACH DELETE n
} IN TRANSACTIONS OF 10000 ROWS;

# COPERTURA DEGLI ARTICOLI

MATCH (a:Article)
RETURN count(a) AS articles_ingested;

# Dimensioni del grafo

## Numero nodi
MATCH (n)
UNWIND labels(n) AS label
RETURN label, count(*) AS total
ORDER BY total DESC;


## Numero relazioni
MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS total
ORDER BY total DESC;

## Predicati non normalizzati
MATCH (c:Claim)
RETURN
    c.predicate AS predicate,
    count(*) AS occurrences
ORDER BY occurrences DESC;

## Numero di Other
MATCH (c:Claim)
RETURN
    count(*) AS total_claims,
    sum(
        CASE
            WHEN c.predicate = 'OTHER'
            THEN 1 ELSE 0
        END
    ) AS other_claims,
    round(
        100.0 *
        sum(CASE
            WHEN c.predicate = 'OTHER'
            THEN 1 ELSE 0
        END) /
        CASE WHEN count(*) = 0 THEN 1 ELSE count(*) END,
        2
    ) AS other_percentage;

## Distribuzione di categorie
MATCH (e:GlobalEntity)
RETURN
    e.kind AS entity_type,
    count(*) AS occurrences
ORDER BY occurrences DESC;

## Verifica Offset
MATCH (a:Article)--(ch:Chunk)--(c:Claim)
WITH
    a, c,
    substring(
        a.body,
        c.body_start,
        c.body_end - c.body_start
    ) AS extracted_quote
RETURN
    count(*) AS claims_checked,
    sum(
        CASE
            WHEN extracted_quote = c.quote
            THEN 1 ELSE 0
        END
    ) AS exact_matches,
    sum(
        CASE
            WHEN extracted_quote IS NULL
              OR extracted_quote <> c.quote
            THEN 1 ELSE 0
        END
    ) AS mismatches;

## Tutti i nodi collegati a Gori (esclude articles e claims)
MATCH p = (g:GlobalEntity)-[:FACT]-(n:GraphNode)
WHERE toLower(trim(g.name)) = 'gori'
  AND NOT n:Claim
  AND NOT n:Article
  AND NOT n:Chunk
RETURN p;


# Table

## Articoli collegati ad un entita' (RAW GRaph)
MATCH (e:GraphNode)<-[:SUBJECT|OBJECT]-(c:Claim)
MATCH (a:Article {id: c.article_id})
OPTIONAL MATCH (a)-[:PUBLISHED_BY]->(s:Source)

WHERE toLower(e.name) CONTAINS toLower('GORI')

RETURN DISTINCT
    a.id AS article_id,
    a.title AS title,
    a.publication_date AS date,
    coalesce(s.name, a.source_name) AS source
ORDER BY date DESC

## Articoli collegati ad un entita' per numero di claim
MATCH (e:GraphNode)<-[:SUBJECT|OBJECT]-(c:Claim)
WHERE toLower(e.name) CONTAINS toLower('GORI')

MATCH (a:Article {id: c.article_id})

RETURN
    a.title AS article,
    a.publication_date AS date,
    count(DISTINCT c) AS claims
ORDER BY claims DESC
LIMIT 50

## Articoli collegati ad un entita' (Grafo ripulito)
MATCH (a:Article)-[:HAS_VALIDATED_FACT]->(f:ValidatedFact)
MATCH (f)-[:SUBJECT_MENTION|OBJECT_MENTION]->(m:ValidatedMention)

WHERE toLower(
    coalesce(m.name, m.surface, m.text, '')
) CONTAINS 'gori'

RETURN DISTINCT
    a.title AS article,
    a.publication_date AS date,
    a.id AS article_id
ORDER BY date DESC
LIMIT 100


## Tutte le relazioni collegate ad una data entita'
MATCH (c:Claim)-[:SUBJECT]->(s:GraphNode)
MATCH (c)-[:OBJECT]->(o:GraphNode)

WHERE
    toLower(s.name) CONTAINS 'gori'
    OR toLower(o.name) CONTAINS 'gori'

RETURN
    c.id AS claim_id,
    s.name AS subject,
    c.predicate AS predicate,
    o.name AS object,
    c.modality AS modality,
    c.attribution AS attribution,
    c.quote AS evidence
ORDER BY predicate
LIMIT 200

## Tutti i claim in cui una data entita' e' il soggetto
MATCH (c:Claim)-[:SUBJECT]->(s:GraphNode)
MATCH (c)-[:OBJECT]->(o:GraphNode)

WHERE toLower(s.name) = 'gori'

RETURN
    c.id AS claim_id,
    c.predicate AS predicate,
    o.name AS object,
    o.kind AS object_type,
    c.quote AS evidence
ORDER BY predicate

## Tutti i claim in cui una data entita' e' l'oggetto
MATCH (c:Claim)-[:SUBJECT]->(s:GraphNode)
MATCH (c)-[:OBJECT]->(o:GraphNode)

WHERE toLower(o.name) CONTAINS 'gori'

RETURN
    s.name AS subject,
    c.predicate AS predicate,
    o.name AS object,
    c.modality AS modality,
    c.quote AS evidence
ORDER BY predicate
LIMIT 200

## Claim collegatyi ad una data entita' raggruppati per validazione
MATCH (c:Claim)-[:SUBJECT|OBJECT]->(e:GraphNode)
MATCH (c)-[:HAS_VALIDATION]->(v:ClaimValidation)

WHERE toLower(e.name) CONTAINS 'gori'

RETURN
    v.status AS validation_status,
    count(DISTINCT c) AS claims
ORDER BY claims DESC

## Tutte le correzzioni verificate riguardanti una data entita'
MATCH (c:Claim)-[:HAS_VERIFIED_CORRECTION]->
      (x:VerifiedClaimCorrection)

WHERE
    toLower(coalesce(x.subject_name, '')) CONTAINS 'gori'
    OR toLower(coalesce(x.object_name, '')) CONTAINS 'gori'

RETURN
    c.id AS original_claim,
    x.subject_name AS corrected_subject,
    x.predicate AS corrected_predicate,
    x.object_name AS corrected_object,
    x.supporting_span AS evidence,
    x.verification_model AS verifier
LIMIT 100

## Entita' con nome simile a quello specificato
MATCH (e:GlobalEntity)

WHERE toLower(e.name) CONTAINS toLower('gori')

RETURN
    e.id AS entity_id,
    e.name AS name,
    e.kind AS type,
    e.subtype AS subtype
ORDER BY name
LIMIT 200

## Entita' simili con numero di claim associati
MATCH (e:GlobalEntity)

WHERE toLower(e.name) CONTAINS 'gori'

OPTIONAL MATCH (c:Claim)-[:SUBJECT|OBJECT]->(e)

RETURN
    e.id AS entity_id,
    e.name AS name,
    e.kind AS type,
    count(DISTINCT c) AS claims
ORDER BY claims DESC
LIMIT 100



## Tutti i fatti validati collegati ad una data entita' (grafo ripulito)
MATCH (f:ValidatedFact)
      -[:SUBJECT_MENTION]->(s:ValidatedMention)

MATCH (f)
      -[:OBJECT_MENTION]->(o:ValidatedMention)

WHERE
    toLower(coalesce(s.name, s.surface, s.text, ''))
        CONTAINS 'gori'
    OR
    toLower(coalesce(o.name, o.surface, o.text, ''))
        CONTAINS 'gori'

RETURN
    coalesce(s.name, s.surface, s.text) AS subject,
    f.predicate AS predicate,
    coalesce(o.name, o.surface, o.text) AS object,
    f.id AS fact_id
ORDER BY predicate
LIMIT 200


## Distribuzione dei predicati in relazione ad una data entita'
MATCH (c:Claim)-[:SUBJECT|OBJECT]->(e:GraphNode)

WHERE toLower(e.name) CONTAINS 'gori'

RETURN
    c.predicate AS predicate,
    count(DISTINCT c) AS occurrences
ORDER BY occurrences DESC

## Articoli senza claim
MATCH (a:Article)

WHERE NOT EXISTS {
    MATCH (a)-[:HAS_CHUNK]->
          (:Chunk)-[:HAS_CLAIM]->(:Claim)
}

RETURN
    a.id AS article_id,
    a.title AS title,
    a.publication_date AS date,
    a.source_name AS source
ORDER BY date DESC
LIMIT 100


# Graph
## Tutte le entita' collegate ad una data entita' (RAW GRAPH)
MATCH p =
    (e:GraphNode)-[:FACT]-(other:GraphNode)

WHERE toLower(e.name) = 'gori'

RETURN p
LIMIT 100

### Per eventuali varianti di nome
MATCH p =
    (e:GraphNode)-[:FACT]-(other:GraphNode)

WHERE toLower(e.name) CONTAINS 'gori'

RETURN p
LIMIT 100

## Tutti i fatti validati collegati ad una data entita' (grafo ripulito)
MATCH (s:ValidatedMention)
      <-[rs:SUBJECT_MENTION]-(f:ValidatedFact)
      -[ro:OBJECT_MENTION]->(o:ValidatedMention)

WHERE
    toLower(coalesce(s.name, s.surface, s.text, ''))
        CONTAINS 'gori'
    OR
    toLower(coalesce(o.name, o.surface, o.text, ''))
        CONTAINS 'gori'

RETURN s, rs, f, ro, o
LIMIT 100