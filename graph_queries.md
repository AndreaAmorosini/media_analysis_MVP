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