# PressKG Quality Suite — fasi 1, 2 e 3

Suite di valutazione **read-only** per il Knowledge Graph derivato dalla rassegna stampa.
Non modifica Neo4j, non rielabora gli articoli, non altera la cache dell'estrattore.
Non include la fase 4 (confronto tra versioni) richiesta dall'utente.

## Fase 1 — audit automatico

```
pixi run python scripts/kg_quality.py audit --data-dir Data --expected-articles 5415 \
  --out output/evaluations/v3_2
```

Output principali: `audit.json`, `offset_errors.csv`, `orphan_claims.csv`,
`predicate_distribution.csv`, `entity_type_distribution.csv`, `quality_report.md`.

Controlla: copertura, numero claim/archi/nodi, predicati `OTHER`, categorie,
integrità dei riferimenti FACT -> Claim, correttezza di `quote`/offset sul body
originale, componenti connesse del sottografo FACT e copertura `HAS_TOPIC`.

Se il grafo non collega `Article--Chunk--Claim` e non conserva `article_id`,
lo script segnala `offset_unverifiable`, non dichiara successo.

## Fase 2 — judge Gemma 4 E4B GGUF via Unsloth

Caricare **unsloth/gemma-4-E4B-it-GGUF** tramite Unsloth Studio, abilitare
l'endpoint OpenAI-compatible e generare l'API key in Settings > API.


Esegui realmente:

```
pixi run python scripts/kg_quality.py semantic \
  --random 300 --diagnostic 100 --seed 42 \
  --model auto \
  --out output/evaluations/v3_2
```

Il judge valuta ogni claim usando
soltanto il segmento `quote` come evidenza. Verifica correttezza di soggetto,
oggetto, tipologie, predicato, direzione, modalità, attribuzione e supporto.
Ammette giudizi `uncertain` quando il segmento non contiene abbastanza contesto.

**I due campioni non sono equivalenti:**
- `random`: campione casuale del corpus, per stimare la prevalenza delle risposte del giudice;
- `diagnostic`: sovracampiona tipi/modalità/predicati rari, per trovare errori.

La percentuale "yes" si calcola solo fra i casi giudicati decidibili;
controllare anche la quota `uncertain`. La CI di Wilson riflette solo
l'incertezza del campionamento, **non** gli errori sistematici del giudice.

Output: `semantic_sample.csv`, `semantic_review.csv`,
`semantic_judgments.jsonl`, `semantic_summary.json`, `semantic_errors.csv`.
Se interrotto, il programma riprende automaticamente i giudizi già salvati in
`semantic_judgments.jsonl` per stesso modello, prompt, temperatura e claim.

## Fase 3 — Entity/Event Resolution

```
pixi run python scripts/kg_quality.py resolution \
  --out output/evaluations/v3_2
```

Output: `resolution_report.json`, `entity_duplicates.csv`,
`cross_type_collisions.csv`, `entity_alias_candidates.csv`,
`event_same_article_candidates.csv`, `event_repeated_names.csv`,
`suspicious_events.csv`, `high_degree_entities.csv`.

Sono **liste di candidati** all'ispezione, NON una valutazione quantitativa
con ground truth del false merge rate o del recall di entity/event resolution.
Due entità che condividono nome possono essere entità diverse;
lo stesso evento riportato da più articoli non significa duplicato certo.

## Eseguire tutte le fasi

```
pixi run python scripts/kg_quality.py all \
  --data-dir Data --expected-articles 5415 \
  --random 300 --diagnostic 100 --seed 42 \
  --out output/evaluations/v3_2
```

Report finali: `summary.json` e `quality_report.md`.

