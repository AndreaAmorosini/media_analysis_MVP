# Architettura di PressKG

Questo documento descrive la struttura del codice dopo la ristrutturazione del
branch `knowledge_graph_analysis`: da codice di sperimentazione (script
standalone e moduli CLI indipendenti) a pipeline modulare con contratti
espliciti e test di regressione.

## Mappa dei package

```
src/presskg/
├── config.py               # configurazione da ambiente (.env) — ROOT punta alla repo
├── common/                 # utility condivise, unica fonte (P2)
│   ├── io.py               # read/write json/jsonl, iter_jsonl, write_json_stable,
│   │                       #   latest_records, batches
│   ├── hashing.py          # sha256, sha256_file (a blocchi), canonical_digest,
│   │                       #   normalize, fingerprint, stable_id
│   ├── db.py               # open_driver(): driver Neo4j condiviso, notifiche
│   │                       #   server soppresse (warning su property mancanti)
│   └── __init__.py         # require + re-export;
├── cli.py                  # entry point unica: ingest (--skip/--limit), stats,
│                           #   pipeline, enrich, normalize-predicates, quality, topics
├── ingestion/              # lettura articoli ed estrazione
│   ├── articles.py         # lettura articoli JSON da Data/ ( campo `source`)
│   ├── extraction.py       # estrazione base da articoli
│   ├── extraction_rules.py # regole di estrazione (versionate)
│   ├── resilient_extraction.py  # estrazione con retry e cache LLM
│   └── grounding.py        # grounding delle menzioni
├── taxonomy/               # dominio statico versionato
│   ├── ontology.py         # ontologia delle relazioni (versionata)
│   ├── entity_ontology.py  # tipi e famiglie di entità
│   └── predicate_catalog.py    # catalogo predicati news (versionato)
├── graph/                  # persistenza e mutazioni del grafo
│   ├── graph_store.py      # accesso Neo4j: articoli → Claim/Chunk/EntityMention,
│   │                       #   GlobalEntity, CANDIDATE_GLOBAL; Source/PUBLISHED_BY
│   │                       #   per le testate note (undefined mai materializzato)
│   ├── enrich_graph.py     # arricchimento topici e metadata
│   ├── normalize_predicates.py # normalizzazione predicati OTHER
│   └── topic_semantics.py  # topic semantici su EventMention (embedding)
├── validation/             # validazione semantica dei claim
│   ├── __main__.py         # dispatch CLI: componenti primary/corrections/
│   │                       #   decisions/materialize (build_parser + run per modulo)
│   ├── batch.py            # derive_batch_id/default_batch_id dai modelli
│   ├── primary.py          # giudizio primario e secondario (generate, LLM)
│   ├── corrections.py      # proposta e verifica delle correzioni
│   ├── decisions.py        # gate automatico (ClaimValidation immutabili)
│   └── materialization.py  # ValidatedFact/ValidatedMention/ValidatedFactBatch
├── quality/
│   └── audit.py            # suite di qualità read-only (audit/semantic/resolution/all)
└── resolution/             # pipeline di Entity/Event Resolution
    ├── _common.py          # re-export, step_driver/close_step_driver, debug_dump,
    │                       #   run_steps (fail-fast + override per-step),
    │                       #   phase_reusable (marker di riuso delle fasi)
    ├── context.py          # StepContext / StepResult (Stadio B)
    ├── paths.py            # registry STEP_DIRS: step → {out}/{nome-step}
    ├── pipeline.py         # orchestratore: fasi, fail-fast per fase, riuso
    ├── benchmark.py        # tempo + RSS di picco per fase (richiede Neo4j)
    ├── identity/           # FASE 1 — profili, alias, clustering, gate
    ├── projection/         # FASE 2 — materializzazione grafo canonico
    ├── predicates/         # FASE 3 — refining predicati canonici
    └── events/             # FASE 4 — consolidamento eventi canonici
```

Utility: **nessun modulo ridefinisce** le utility comuni — si importano da
`presskg.common` (imposto da `tests/regression/test_no_duplicate_helpers.py`).
Le varianti semantiche volutamente locali sono documentate nel docstring di
quel test (normalize, fingerprint, stable_id, latest_records con history).

Ogni fase (`identity`, `projection`, `predicates`, `events`) è un package che
contiene degli **step**: moduli puri rispetto all'orchestrazione, che espongono
`run(ctx) -> StepResult` (Stadio B) e si scambiano i dati tramite file
JSON/JSONL nella directory di output (`--out`, default `output/resolution/`).

## Contratti

### Step: `run(ctx) -> StepResult` (Stadio B)

Ogni modulo step espone `run(ctx)` dove `ctx` è un
`presskg.resolution.context.StepContext`, e restituisce un `StepResult`.
Il context incapsula gli argomenti della pipeline:

- `ctx.args`: namespace con `apply` (dry-run di default), `out`
  (directory output), `verbose`, più opzioni specifiche dello step —
  ogni attributo non definito dal context viene forwardato a `args`;
- `ctx.apply` / `ctx.verbose` (accessi tipizzati), `ctx.out_root()`,
  `ctx.step_dir(key)` / `ctx.step_file(key, name)` (registry paths),
  `ctx.driver()` (driver Neo4j condiviso);

Lo step interagisce solo con:

- file letti/scritti nella directory di output, con path derivati da default
  della funzione;
- Neo4j in lettura (e scrittura solo se `ctx.apply`).

`StepResult` (`.ok`, `.error`, `.counts`, `.warnings`, `.checks`) raccoglie
esito e conteggi, entra nello stato della pipeline e nel debug dump. Il
return conclusivo di ogni step usa `StepResult.from_summary(summary)`:
le voci scalari del summary già calcolato dallo step
(`graph_writes`, `llm_calls`, `scanned_facts`, ...) entrano in `.counts`,
mentre checksum, stringhe e strutture annidate restano nel summary.json
su disco. Il path di errore usa `StepResult.fail()` con tipo e messaggio
dell'eccezione catturata. Il runner è fail-fast: il primo step in errore
interrompe la fase (gli step successivi dipendono dagli output correnti),
e con `--save-state` gli esiti di ogni fase includono i counts di ogni
step. Un plain `bool` in uscita è ancora accettato per retrocompatibilità
(`_common.run_steps`).

### Fase: `run(args, state) -> state`

Ogni package di fase espone `run(args, state)`, la lista ordinata `STEPS` e i
propri marker di riuso (`REUSE_MARKERS` e simili). Le voci di `STEPS` sono
tuple `(nome_snapshot, modulo, reads[, overrides])`:

- `reads` dichiara gli artefatti in ingresso (l'ordine è topologico);
- `overrides` (opzionale) è un dict di attributi iniettati nel namespace
  **solo per la durata dello step** e ripristinati subito dopo: la pipeline
  lo usa per fornire ai due step di materializzazione le conferme dedicate
  (`CONFIRM`, stringhe diverse per step) quando gira con `--apply`;
  standalone gli step restano protetti dal proprio `--confirm`.

Ogni fase rileva i propri marker con `_common.phase_reusable`: se gli
artefatti finali esistono, riportano l'esito atteso e sono coerenti col
batch, la fase si salta (riuso); il runner `run_steps` è fail-fast e un
plain `bool` in uscita resta accettato per retrocompatibilità.

### Pipeline: `presskg.resolution.pipeline`

- `PHASES = ["identity", "projection", "predicates", "events"]` — ordine fisso,
  inchiodato dai test di regressione;
- `run(args, state=None)`: inizializza lo stato a `{}`, apre **un driver Neo4j
  condiviso per l'intera esecuzione** e lo attacca ad `args.driver` (chiuso
  in `finally`), esegue le fasi scelte, salva `{fase}_state.json` con
  `--save-state`;
- **fail-fast per fase all'avvio**: se nel `--out` esistono artefatti di una
  fase che però non è riusabile (catena interrotta o batch diverso), la
  pipeline si ferma subito con il messaggio della fase incriminata invece
  di fallire a metà corsa dopo aver speso le chiamate LLM; `predicates` ed
  `events` in dry-run non vengono controllate perché la pipeline non le
  esegue;
- in dry-run `predicates` ed `events` vengono saltate: operano sul grafo
  canonico materializzato e servono `--apply`;
- `main(argv)`: parsing degli argomenti (`--phase`, `--apply`, `--out`,
  `--batch-id`, `--verbose`, `--save-state`).

### Artefatti e path: `presskg.resolution.paths` (Stadio A)

- `paths.STEP_DIRS` è il registry unico delle directory di step
  (`{out}/{nome-step}`); `--out` (default `output/resolution`) è ora
  rispettato da tutti gli step;
- i moduli non contengono più literal `"output/resolution/..."`:
  ogni default usa `paths.step_dir(args, "<step>")`
  (imposto da `tests/code/regression/test_stage_a_paths.py`);
- le voci `STEPS` delle fasi sono tuple
  `(nome, modulo, reads[, overrides])`: `reads` dichiara gli artefatti in
  ingresso e il test verifica che l'ordine sia topologico (nessuno step legge
  qualcosa che non sia già stato prodotto); `overrides`, quarto elemento
  opzionale, è l'override per-step del namespace (vedi il contratto di fase);
- gli step non aprono più driver propri: usano `_common.step_driver(args)`,
  che riusa quello della pipeline (unica eccezione documentata:
  `projection/dry_run.py`, che espone override `--uri/--user/--password`
  ma preferisce comunque il driver condiviso);
- gli step **non chiudono mai il driver condiviso**: chiudono solo un
  driver aperto da soli, via `_common.close_step_driver(args)`
  (ownership tracciata con `args._own_driver`; il driver condiviso viene
  chiuso dal `finally` della pipeline);
- benchmark di una fase: `pixi run benchmark-identity`
  (richiede Neo4j; salva tempo e RSS di picco in `output/benchmarks/`).

## Flusso dei dati

```
Data/*.json → ingest (core) → Neo4j: Article/Chunk/Claim, EntityMention,
                              GlobalEntity/CANDIDATE_GLOBAL, Source/PUBLISHED_BY
            → validation (LLM giudici + gate) → ClaimValidation
                              → materialize → ValidatedFact/ValidatedMention
                              (ValidatedFactBatch: baseline della resolution)
            → resolution.pipeline
               1. identity   : profili menzioni → alias → coppie → cluster
                               → occurrences → gate → readiness
               2. projection : dry-run → safety gate → CanonicalEntity
                               → RESOLVED_AS → CANONICAL_FACT
               3. predicates : audit → grounding → candidate → verifica
                               → overlay dei predicati canonici
               4. events     : consolidation → inventario evidenze
                               → CanonicalEvent → SUPPORTED_BY → audit
```

Dry-run/apply: tutte le scritture su Neo4j avvengono solo con `--apply`.
Il dry-run produce file di output e report ispezionabili senza toccare il DB.

Riuso degli artefatti (`_common.phase_reusable`): ogni fase dichiara i
propri marker (artefatti finali nel registry `paths`); se esistono,
contengono l'esito atteso (campo `complete` per i summary di
materializzazione, `applied` per l'overlay) e sono coerenti col batch
derivato dai modelli (`--batch-id` per un batch storico), la fase si salta
riusando i risultati:

- `identity` → `readiness_refinement/summary.json` (la fase non scrive sul grafo);
- `projection` → pianificazione completa = `fact_projection/summary.json`;
  con `--apply` si eseguono solo i due step di materializzazione
  (idempotenti, MERGE); già applicata = i due `apply_summary.json` con
  `complete: true` (il summary è scritto nel `finally` anche a step
  fallito: il campo esito è quello che distingue);
- `predicates` → `overlay/summary.json` con il campo esito `applied`;
- `events` → `consolidation/summary.json` (batch consolidati) +
  `graph_audit/summary.json`.

Uno stato parziale (fase interrotta o batch incoerente) non è riusabile:
il fail-fast iniziale di `pipeline.run` lo segnala per fase e suggerisce
un `--out` nuovo. `--apply` della pipeline fornisce ai due step di
materializzazione le conferme dedicate (`CONFIRM` di ogni step) tramite
gli override per-step del tuple `STEPS` (quarto elemento); standalone gli
step restano protetti dal proprio `--confirm`.

## Come si esegue

```bash
pixi run resolution                     # tutte le fasi, dry-run
pixi run resolution --phase identity    # una fase sola
pixi run resolution --phase all --apply # applica le scritture
```

Comandi equivalenti via CLI generale:

```bash
python -m presskg.cli pipeline --phase predicates --apply --verbose
```

## Orchestrazione: gli script full

`scripts/run_validation_full.sh` (`pixi run validation-full`) concatena la
catena di validazione 1→8: giudizio primario, preparazione correzioni,
selezione secondari, secondo giudizio, riverifica, gate decisioni, gate
correzioni e — solo con `VALIDATION_COMMIT=1` — materializzazione dei
`ValidatedFact` (il batch letto come baseline dalla resolution). Le fasi
LLM riprendono con `--resume` se i CSV esistono; `VALIDATION_ONLY_APPLY=1`
salta le fasi LLM e rilancia solo i gate; `VALIDATION_BATCH_ID` override
del batch. Le `ClaimValidation` sono immutabili: il gate rifiuta di
sovrascrivere una validazione esistente con campi diversi (`Conflitto:
campo ...`).

`scripts/run_e2e.sh` (`pixi run e2e`) è l'end-to-end completo dal dataset
al grafo canonico arricchito: preflight → init-db → vincoli grafo
canonico (`IF NOT EXISTS`) → `ingest` (`E2E_SKIP`/`E2E_LIMIT`:
di default tutto il dataset) → `validation-full` con commit →
`resolution --apply --save-state` → `enrich` + `normalize-predicates` →
`test-graph` → `stats`. Richiede un DB **vuoto** (guardia al passo 0:
blocca se esistono articoli o validazioni, override consapevole con
`E2E_ALLOW_DIRTY=1` per riprendere un E2E interrotto). Ogni passo è
riprendibile: ingest salta gli articoli presenti, la validazione va in
resume, la pipeline riusa gli artefatti completi.

## Test

```
tests/
├── code/                    # integrità del codice, senza Neo4j né LLM
│   ├── unit/                # 34 file: contratti dei singoli step (identity/,
│   │                       #   projection/, predicates/, events/), validation,
│   │                       #   quality, riuso delle fasi (test_resolution_
│   │                       #   phase_reuse.py), selezione ingest (--skip,
│   │                       #   test_ingest_selection.py), driver comune
│   │                       #   (test_common_db.py)
│   └── regression/          # invarianti architetturali e characterization
│       ├── test_core.py                 # moduli ingestion/graph/taxonomy
│       ├── test_extraction.py           # contratti estrazione
│       ├── test_resolution_characterization.py
│       ├── test_system_invariants.py    # versioni e firme CLI
│       ├── test_structural_invariants.py   # architettura e contratti pipeline
│       ├── test_no_duplicate_helpers.py     # vietate le utility duplicate (P2)
│       ├── test_no_paths_shadowing.py      # nessun shadowing del registry paths
│       ├── test_pipeline_namespace_contract.py  # step: solo attributi del
│       │                                    #   namespace della pipeline
│       ├── test_stage_a_paths.py            # registry path e ordine topologico (P5)
│       ├── test_stage_b_context.py          # StepContext/StepResult/runner (P6)
│       └── test_graph_tests_are_readonly.py  # disciplina read-only dei test/*.graph
└── graph/                   # audit del grafo REALE (read-only, marker `graph`)
    ├── conftest.py                       # skip se Neo4j non raggiungibile
    ├── test_schema_integrity.py          # etichette/relazioni attese e note
    ├── test_projection_integrity.py      # end-point canonici + provenance
    ├── test_event_integrity.py           # EVENT_RESOLVED_AS / SUPPORTED_BY
    └── test_quality_audit.py              # fase 1 di presskg.quality sul DB
```

Suite del codice (nessuna dipendenza da Neo4j o LLM: i test stubbano driver e client):

```bash
pixi run test          # -m "not graph"
```

Audit del grafo reale (richiede Neo4j attivo; altrimenti skip esplicito):

```bash
pixi run test-graph
```

Cosa inchiodano i test strutturali (`test_structural_invariants.py`):

1. ogni modulo di `presskg` è importabile;
2. ogni step espone `run(ctx) -> StepResult` (Stadio B, verificato anche da
   `test_stage_b_context.py`) e popola i counts dal proprio summary via
   `StepResult.from_summary` (P7); ogni fase `run(args, state)` + `STEPS`;
3. `STEPS` dichiara esattamente i moduli presenti nel package (nessuno dimenticato);
4. ordine delle fasi e threading dello stato della pipeline;
5. default dry-run (`--apply` è opt-in);
6. nessun riferimento ai vecchi percorsi flat (`presskg.settings`,
   `presskg.resolution.profiles`, ...) né in src né in scripts né in tests;
7. gli import di `scripts/*.py` e i task `python -m ...` di `pixi.toml`
   puntano a moduli esistenti;
8. i sottocomandi CLI attesi rispondono a `--help`.

## Convenzioni

- Lingua dei docstring e delle stampe: italiano.
- Versioni dei cataloghi/ontologie: costanti `VERSION` nei moduli di `taxonomy`
  (verificate da `test_system_invariants.py`).
- Output diagnostici: sempre sotto `output/` (ignorato da git);
  snapshot di debug con `--verbose` in `{out}/debug/{fase}/{step}.json`.
- Selezione articoli deterministica: `cli.select_article_paths(skip, limit)`
  sull'ordinamento fisso di `Data/` (`--skip`/`--limit` dell'ingest,
  `E2E_SKIP`/`E2E_LIMIT` dell'E2E sullo stesso criterio).
- La suite di qualità vive in `presskg.quality` (P3): si esegue con
  `python -m presskg.cli quality <fase>`, è read-only su Neo4j e fornisce
  le query usate dai test del grafo (`tests/graph/`).
- Gli esperimenti superati (`resolve_entities.py`, `resolve_events.py`,
  `validated_fact_projection_v2.py`) sono stati rimossi: l'implementazione
  corrente vive in `presskg/resolution/`; la history git conserva gli originali.
- **Batch id derivato dai modelli** (`presskg.validation.batch`): il batch
  che identifica i ValidatedFact varia con i modelli effettivi della catena,
  nella forma `<ingest>-<giudice primario>-<giudice secondario>-vN`, con tag
  leggibili (`unsloth/Qwen3.5-4B-MTP-GGUF:UD-Q4_K_XL` → `qwen35-4b-mtp`).
  Default: ingest da `UNSLOTH_MODEL`, giudice primario da `UNSLOTH_EVAL_MODEL`,
  secondario da `UNSLOTH_EVAL_MODEL_BIS` (fallback: secondo giudice di
  default del repo). Niente id congelati nel codice
  (imposto da `test_validation_package.py`); override esplicito con
  `--batch-id` su validation e `pixi run resolution --batch-id <id>` per
  rileggere batch storici già materializzati.
