# PressKG

PressKG è una pipeline Python per costruire un knowledge graph da articoli di stampa. Legge gli articoli in formato JSON, estrae entità e claim con un modello LLM servito esternamente, valida i claim con giudici LLM e registra risultati e riferimenti al testo in Neo4j, fino al grafo canonico risolto.

## Panoramica del flusso

```
Data/*.json ── ingest ──► Article/Chunk/Claim/EntityMention + Source (PUBLISHED_BY)
                │
                └─ validation (giudici LLM + gate) ──► ClaimValidation + ValidatedFact/ValidatedMention
                       │                                 (ValidatedFactBatch, vista baseline)
                       └─ resolution.pipeline (4 fasi)
                              1. identity   : profili menzioni → alias → clustering → gate → readiness
                              2. projection : dry-run → safety gate → CanonicalEntity → CANONICAL_FACT
                              3. predicates : audit → grounding → candidati → verifica → overlay
                              4. events     : consolidamento → inventario evidenze → CanonicalEvent → audit
                       │
                       └─ arricchimenti: enrich, normalize-predicates, topics (opzionali)
```

Tutte le scritture su Neo4j avvengono solo con `--apply`: il default è dry-run.

## Requisiti

- Docker con Compose, per l'esecuzione tramite container; oppure Pixi per eseguire la pipeline in locale.
- Un modello disponibile tramite endpoint compatibile OpenAI, ad esempio Unsloth Studio. Il server del modello **non** è incluso nei container.
- Articoli JSON nella directory `Data/`, anche in sottodirectory. Ogni articolo deve avere un campo `body` non vuoto; `title`, `subtitle`, `publication_date` (o `date`) e `source` sono supportati.

## Configurazione

Copia `.env.example` in `.env` e imposta almeno `NEO4J_PASSWORD`, `UNSLOTH_MODEL` e l'URL del server del modello.

| Variabile | Default | Uso |
|---|---|---|
| `NEO4J_URI` | `bolt://127.0.0.1:7687` | Indirizzo Neo4j |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / — | Credenziali DB |
| `NEO4J_DATABASE` | `neo4j` | Database Neo4j |
| `UNSLOTH_BASE_URL` | `http://127.0.0.1:8888/v1` | Endpoint OpenAI-compatibile del server LLM |
| `UNSLOTH_API_KEY` | `local` | API key del server LLM |
| `UNSLOTH_MODEL` | — | Modello di **estrazione** (ingest) |
| `UNSLOTH_EVAL_MODEL` | — | Giudice primario della catena di validazione |
| `UNSLOTH_REQUEST_TIMEOUT` | `600` | Timeout richieste LLM (secondi) |
| `PRESSKG_PROFILE` | `4070` | Profilo hardware: `4070` o `5090` (finestre/token massimi) |
| `PRESSKG_DATA_DIR` | `Data` | Directory degli articoli |
| `PRESSKG_OUTPUT_DIR` | `output/extractions` | Cache delle estrazioni |
| `PRESSKG_WINDOW_CHARS` / `PRESSKG_WINDOW_OVERLAP` / `PRESSKG_MAX_OUTPUT_TOKENS` | da profilo | Suddivisione finestre e budget dell'estrazione |
| `PRESSKG_JSON_MODE` | `false` | Modalità JSON del server LLM |

**Batch id**: l'id del batch di validazione (`<ingest>-<giudice>-<giudice secondario>-v1`, tag derivati dai modelli) è deterministico e cambia automaticamente quando cambi i modelli. Ogni catena di modelli produce e legge un proprio batch: non mischiare batch diversi sullo stesso `--out` della pipeline (la pipeline lo rileva e si ferma).

Per l'esecuzione con Docker (dettagli nella sezione dedicata): l'endpoint del
server LLM visto **dentro** il container non può essere `127.0.0.1`; il
servizio `presskg` fornisce già il fallback
`http://host.docker.internal:8888/v1`, quindi in `.env` elimina
`UNSLOTH_BASE_URL` (o impostalo esplicitamente a
`http://host.docker.internal:8888/v1`). `NEO4J_PUBLIC_HOST` definisce
l'indirizzo Neo4j pubblicato ai client (default di compose: `localhost`).

## Esecuzione con Docker Compose

Il file `docker-compose.yaml` definisce due servizi:

- **neo4j** (`neo4j:2026.10.0`): DB con volume `neo4j_data` persistente. Porte pubblicate su `127.0.0.1` (override con `NEO4J_BIND_ADDRESS=0.0.0.0`); le porte host seguono `NEO4J_BOLT_PORT`/`NEO4J_HTTP_PORT` (default 7687/7474) mentre dentro la rete compose il Bolt resta su 7687.
- **presskg**: immagine costruita dal `Dockerfile` (base Pixi ufficiale: installa l'ambiente `--locked` da `pixi.lock` e copia `src/`, `scripts/`, `config/`, `profiles/`, `tests/`). Il container resta in attesa (`sleep infinity`): la pipeline si avvia esplicitamente con `docker compose exec`.

Avvio e comandi:

```bash
docker compose up -d --build                     # costruisce e avvia i servizi
docker compose exec presskg pixi run preflight   # verifica LLM + Neo4j
docker compose exec presskg pixi run init-db     # vincoli (idempotente)
docker compose exec presskg pixi run ingest       # estrazione
docker compose exec presskg pixi run resolution  # pipeline di resolution
docker compose exec presskg pixi run e2e        # end-to-end
docker compose exec presskg pixi run stats       # conteggi del grafo
```

Note per l'uso in container:

- **Server LLM**: dentro il container `127.0.0.1` non raggiunge un server avviato sull'host. Il servizio `presskg` imposta `UNSLOTH_BASE_URL` con fallback `http://host.docker.internal:8888/v1` (host-gateway mappato): se in `.env` hai `UNSLOTH_BASE_URL=http://127.0.0.1:8888/v1` quel valore non funziona in container — elimina la variabile (così vale il fallback) oppure imposta `http://host.docker.internal:8888/v1`.
- **Connessione DB**: il servizio impone `NEO4J_URI=bolt://neo4j:7687` (nome del servizio compose), qualunque valore ci sia in `.env`.
- **Mount**: `Data/` è montato read-only su `/app/Data`; `./output` su `/app/output` (cache e artefatti restano sull'host); `src/`, `scripts/`, `config/`, `profiles/`, `tests/` sono montati, quindi le modifiche al codice sono visibili senza ricostruire l'immagine.
- **Profilo hardware**: il servizio rispetta `PRESSKG_PROFILE` di `.env` (default `5090`).

Interfaccia web di Neo4j: `http://localhost:7474` (salvo porte diverse).

**Reset completo del DB** (distruttivo — elimina anche il volume dei dati):

```bash
docker compose down -v
docker compose up -d --build
```

**Seconda istanza di test** (DB separato su porte diverse, es. 7688 per il collaudo senza toccare il DB principale):

```bash
NEO4J_BOLT_PORT=7688 NEO4J_HTTP_PORT=7475 \
    docker compose -p kgpress-test up -d neo4j
# per lavorarci: NEO4J_URI=bolt://127.0.0.1:7688 in .env
docker compose -p kgpress-test down -v   # alla fine: reset dell'istanza
```

Il suffisso `-p kgpress-test` isola nomi e volumi del progetto compose, così l'istanza di test non collide con quella principale.

## Esecuzione locale con Pixi

```bash
pixi install
pixi run preflight
pixi run init-db
pixi run ingest
```

Pixi utilizza Python 3.11 o 3.12; il progetto dichiara la piattaforma `linux-64`.

## Fase 1 — Ingestione

```bash
pixi run ingest                                        # tutti gli articoli
pixi run python -m presskg.cli ingest --limit 10       # primi 10 (ordinamento fisso di Data/)
pixi run python -m presskg.cli ingest --skip 10 --limit 20   # articoli 11-30
pixi run python -m presskg.cli ingest --missing-only   # solo articoli assenti in Neo4j
pixi run python -m presskg.cli ingest --replace        # rielabora articoli già presenti
pixi run python -m presskg.cli ingest --no-cache       # ignora la cache delle estrazioni
```

| Opzione | Effetto |
|---|---|
| `--limit N` | Al massimo i primi N articoli |
| `--skip N` | Salta i primi N articoli (selezione deterministica) |
| `--missing-only` | Solo articoli non presenti in Neo4j |
| `--replace` | Riscrive articoli già presenti se contenuto/firma estrazione sono cambiati |
| `--no-cache` | Non riusare la cache `output/extractions/` |

L'ingest crea anche `Article.source_name` e, per le fonti note, il nodo `(:Source)` con `(Article)-[:PUBLISHED_BY]->(Source)`. `pixi run stats` mostra i conteggi; `pixi run init-db` crea i vincoli (idempotente).

## Fase 2 — Validazione dei claim

Comandi granulari (i file CSV/jsonl intermedi vanno in una directory dedicata, es. `output/semantic_validation/<run>`):

```bash
# 1. giudizio primario (LLM) sui claim
pixi run python -m presskg.validation primary generate \
    --model "$UNSLOTH_EVAL_MODEL" --limit 0 \
    --context-chars 600 --max-tokens 1600 --retries 1 \
    --out output/semantic_validation/run/primary.csv [--resume]

# 2. preparazione candidate di correzione
pixi run python -m presskg.validation corrections prepare \
    --primary .../primary.csv --out .../correction_candidates.jsonl --context-chars 600 --overwrite

# 3. selezione claim per la seconda verifica
pixi run python -m presskg.validation decisions select-secondary \
    --primary .../primary.csv --out .../secondary_ids.csv --skip-correction-proposals

# 4. secondo giudizio (LLM, modello diverso dal primario)
pixi run python -m presskg.validation primary generate \
    --model <secondario> --claim-ids-file .../secondary_ids.csv \
    --out .../secondary.csv [--resume]

# 5. riverifica delle correzioni (LLM)
pixi run python -m presskg.validation corrections verify \
    --candidates .../correction_candidates.jsonl --model <secondario> \
    --out .../correction_verified.jsonl [--resume]

# 6. gate: decisioni persistite (dry-run di default)
pixi run python -m presskg.validation decisions apply \
    --primary .../primary.csv --secondary .../secondary.csv \
    --batch-id <batch> --context-chars 600 [--commit]

# 7. gate delle correzioni
pixi run python -m presskg.validation corrections apply \
    --primary ... --secondary ... --candidates ... --verified ... \
    --batch-id <batch> --context-chars 600 [--commit]

# 8. materializzazione ValidatedFact/ValidatedMention/Batch (baseline della resolution)
pixi run python -m presskg.validation materialize --batch-id <batch> --commit
```

Opzioni principali: `--limit` (0 = tutti), `--context-chars` (contesto attorno al claim), `--retries`/`--timeout`/`--max-consecutive-errors` (resilienza LLM), `--resume` (riparte da un CSV parziale), `--claim-ids-file`, `--commit`. Le stesse componenti sono task con default: `validation-primary`, `validation-corrections`, `validation-decisions`, `validation-materialize`.

### Script full: `pixi run validation-full`

Esegue l'intera catena 1→8 determinando il batch id dai modelli di `.env`. Comportamento controllato da variabili d'ambiente:

| Variabile | Default | Effetto |
|---|---|---|
| `VALIDATION_DIR` | `output/semantic_validation/qwen38_full` | Directory dei CSV/jsonl (struttura attesa dallo script) |
| `VALIDATION_COMMIT=1` | dry-run | Persiste gate 6-7 su Neo4j e materializza il batch (8) |
| `VALIDATION_ONLY_APPLY=1` | — | Salta le fasi LLM (1-5): riusa i CSV esistenti ed esegue solo i gate |
| `VALIDATION_BATCH_ID` | derivato dai modelli | Override dell'id batch (per batch storici) |
| `VALIDATION_GEMMA31_ID` | `UNSLOTH_EVAL_MODEL` | Giudice primario |
| `VALIDATION_GEMMA12_ID` | `unsloth/gemma-4-12b-it-GGUF` | Giudice secondario (diverso dal primario) |

```bash
pixi run validation-full                                        # generazione (dry-run dei gate)
VALIDATION_COMMIT=1 pixi run validation-full                     # generazione + commit + materializzazione
VALIDATION_DIR=output/semantic_validation/run VALIDATION_ONLY_APPLY=1 VALIDATION_COMMIT=1 \
    pixi run validation-full                                     # solo i gate da CSV esistenti
```

Le fasi LLM riprendono con `--resume` se i CSV esistono già: un run interrotto si rilancia con lo stesso comando.

## Fase 3 — Resolution: `pixi run resolution`

Quattro fasi dopo la validazione: `identity` (profili, alias, clustering dei referenti), `projection` (materializzazione entità e fact canonici), `predicates` (refining dei predicati) ed `events` (eventi canonici e evidenze). Il sottocomando CLI sottostante è `pipeline` (`python -m presskg.cli pipeline`).

```bash
pixi run resolution                               # tutte le fasi, dry-run
pixi run resolution --phase identity              # una fase sola
pixi run resolution --phase all --apply           # applica le scritture a Neo4j
pixi run resolution --phase identity --verbose --save-state
pixi run resolution --out output/resolution      # directory artefatti (default)
pixi run resolution --batch-id <batch>           # batch ValidatedFact specifico
```

| Opzione | Effetto |
|---|---|
| `--phase` | `all` (default), `identity`, `projection`, `predicates`, `events` |
| `--apply` | Scritture su Neo4j (default: dry-run) |
| `--out DIR` | Directory degli artefatti (default `output/resolution`); un run completo per directory |
| `--batch-id` | Batch ValidatedFact da processare (default: derivato dai modelli di `.env`) |
| `--verbose` | Snapshot di debug per step in `DIR/debug/` |
| `--save-state` | Salva `DIR/<fase>_state.json` con i counts di ogni step |

**Semantica dry-run**: `predicates` ed `events` operano sul grafo canonico materializzato e vengono saltate; il dry-run verifica fino al piano di proiezione.

**Riuso: ogni fase capisce se è già completata**. Se gli artefatti finali nel `--out` sono completi e coerenti col batch atteso, la fase si salta riusando i risultati. Il flusso tipico è in due passi sulla stessa directory:

```bash
pixi run resolution --out output/resolution --save-state   # dry-run completo
pixi run resolution --out output/resolution --apply --save-state
# riusa identity e la pianificazione, materializza su Neo4j,
# poi esegue per la prima volta predicates ed events
```

- identity riusata → summary di `readiness_refinement` coerente col batch;
- projection: pianificazione riusata → con `--apply` esegue **solo** i due step di materializzazione (idempotenti); già applicata → fase intera saltata;
- predicates riusata → overlay applicato; events riusata → audit finale presente.

Uno stato parziale (catena interrotta o batch diverso) non è riusabile e la pipeline si ferma subito con un messaggio per fase: usare un `--out` nuovo.

Ogni step restituisce un `StepResult` con esito, conteggi e messaggio d'errore: con `--save-state` il JSON per fase contiene i counts di ogni step (scritture sul grafo, chiamate LLM, record processati), utili per audit e confronto tra run.

## Arricchimenti del grafo

```bash
pixi run enrich                       # arricchimento (dry-run di default)
pixi run enrich --apply
pixi run normalize-predicates         # normalizza predicati OTHER (dry-run di default)
pixi run normalize-predicates --apply
pixi run python -m presskg.cli topics # topic semantici su EventMention (embedding)
```

`topics` supporta `--topics` (catalogo), `--out`, `--method direct|embedding`, `--model` (embedding), `--min-score`, `--min-margin`, `--apply-direct/--apply-auto/--apply-approved`.

## Audit

```bash
pixi run quality                      # suite qualità read-only: audit|semantic|resolution|all
pixi run test                         # suite del codice: unit + regressione, senza Neo4j né LLM
pixi run test-graph                   # integrità del grafo reale (read-only)
```

`quality` supporta `--phase audit|semantic|resolution|all`, `--out`, `--random`/`--diagnostic` (campioni), `--model`, `--seed`.

Benchmark di fase (tempo + RSS di picco, richiede Neo4j):

```bash
pixi run benchmark-identity           # --phase <fase> --out DIR --bench-dir DIR
```

## Script full: `pixi run e2e` — dalla A alla Z

Catena completa in un solo comando: preflight → init-db → vincoli canonico → ingest → validazione (LLM + commit + materializzazione) → resolution pipeline (`--apply`) → arricchimenti (`enrich`, `normalize-predicates`) → audit del grafo (`test-graph`) → statistiche.

```bash
pixi run e2e                                  # TUTTO il dataset
E2E_LIMIT=20 pixi run e2e                     # primi 20 articoli
E2E_SKIP=10 E2E_LIMIT=20 pixi run e2e         # articoli 11-30
E2E_OUT=output/resolution_e2e2 pixi run e2e   # directory pipeline dedicata
```

| Variabile | Default | Effetto |
|---|---|---|
| `E2E_SKIP` | `0` | Articoli saltati dall'ingest (ordinamento fisso) |
| `E2E_LIMIT` | tutti | Tetto articoli da elaborare (vuoto = dataset intero) |
| `E2E_OUT` | `output/resolution_e2e` | `--out` della pipeline |
| `VALIDATION_DIR` | `output/semantic_validation/e2e` | CSV della validazione (riprendibili) |
| `E2E_ALLOW_DIRTY=1` | — | Ammette un DB non vuoto: per riprendere un E2E interrotto |

Note:

- **Richiede un DB vuoto** (o consapevolmente sporco con `E2E_ALLOW_DIRTY=1`): le `ClaimValidation` sono immutabili e una validazione su claim già giudicati da un altro run si ferma con `Conflitto: campo ...`.
- **Riprendibile**: ingest salta gli articoli già presenti, la validazione va in `--resume` sui CSV, la pipeline riusa gli artefatti completi coerenti. Un E2E interrotto si rilancia: `E2E_ALLOW_DIRTY=1 E2E_LIMIT=20 pixi run e2e`.
- Il costo LLM segue le dimensioni: usa il dataset intero solo quando hai tempo; per i test usa subset via `E2E_SKIP`/`E2E_LIMIT`.

## Riferimento rapido dei task

| Task | Descrizione |
|---|---|
| `preflight` | Verifica server LLM e Neo4j |
| `init-db` | Vincoli Neo4j (idempotente) |
| `ingest` | Estrazione claim/entità (vedi Fase 1) |
| `stats` | Conteggi del grafo |
| `validation-primary/corrections/decisions/materialize` | Singole componenti di validazione (default dal modulo) |
| `validation-full` | Catena di validazione 1→8 con `VALIDATION_*` (vedi Fase 2) |
| `resolution` | Resolution pipeline (4 fasi, dry-run/apply, riuso) |
| `e2e` | End-to-end completo con `E2E_*` (vedi sopra) |
| `enrich` / `normalize-predicates` | Arricchimenti del grafo (topics via `python -m presskg.cli topics`) |
| `quality` | Audit qualità read-only |
| `test` / `test-graph` | Suite del codice / integrità del grafo |
| `benchmark-identity` | benchmark di fase (tempo + RSS di picco) |

Le estrazioni vengono salvate in `output/extractions/`. Le directory `Data/` e `output/` non sono incluse nel repository.

## Test

La suite completa (unit + regressione) non richiede Neo4j né un server LLM: le dipendenze esterne vengono stubbate.

```bash
pixi run test
```

Struttura, contratti delle fasi e flusso dei dati sono documentati in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). I test del grafo (`tests/graph/`, `pixi run test-graph`) verificano direttamente l'integrità del grafo reale in Neo4j e vengono deselezionati dalla suite del codice.
