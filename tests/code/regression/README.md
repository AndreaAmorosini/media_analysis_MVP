# Regression Test Suite

Suite di test di regressione per il refactoring di PressKG.

## Struttura

```
tests/code/regression/
├── __init__.py
├── conftest.py                          # fixture condivise + stub dipendenze
├── test_core.py                         # test unitari moduli core (articles, ontology, entity_ontology, ...)
├── test_extraction.py                   # test per extraction (windows, parse_json, validate, grounded_records)
├── test_resolution_characterization.py  # characterization tests per step resolution
├── test_system_invariants.py            # invarianti architetturali (import, versioni, CLI signature, ...)
├── test_structural_invariants.py        # contratti pipeline: run(args)->bool, fasi, STEPS, no percorsi legacy
├── test_no_duplicate_helpers.py          # vietate utility duplicate fuori da presskg.common
└── test_graph_tests_are_readonly.py     # i test del grafo devono essere read-only
```

## Esecuzione

```bash
# Tutti i test di regressione
pixi run python -m pytest tests/code/regression/ -v

# Tutti i test del progetto (regressione + esistenti)
pixi run python -m pytest tests/code -v

# Solo un file
pixi run python -m pytest tests/code/regression/test_core.py -v

# Solo una classe
pixi run python -m pytest tests/code/regression/test_core.py::TestArticles -v
```

## Scopo

Questi test catturano il **comportamento attuale** del codice prima del refactoring.
Se un test fallisce dopo una modifica, significa che il comportamento è cambiato:
valutare se il cambiamento è intenzionale (aggiornare il test) o una regressione (correggere il codice).

`test_structural_invariants.py` in più inchioda il **contratto architetturale**
della pipeline di resolution dopo la ristrutturazione in package (`core/`,
`resolution/{identity,projection,predicates,events}/`):

- ogni step espone `run(args) -> bool`, ogni fase `run(args, state)` + `STEPS`;
- ordine delle fasi e threading dello stato della pipeline;
- default dry-run (`--apply` sempre opt-in);
- nessun riferimento ai vecchi percorsi flat dei moduli;
- import di `scripts/*.py` e task `python -m ...` di `pixi.toml` risolti;
- sottocomandi CLI attesi disponibili con `--help`.

## Esecuzione veloce

```bash
pixi run test                     # suite completa
pixi run python -m pytest tests/code/regression/test_structural_invariants.py -v
```

## Dipendenze

I test non richiedono Neo4j o un server LLM attivo. Le dipendenze esterne
(`neo4j`, `openai`, `tqdm`, `json_repair`) vengono stubbate automaticamente
in `conftest.py` se non sono installate.

## Audit del grafo reale

Oltre a questa suite (codice), esiste `tests/graph/`: audit read-only del
grafo Neo4j reale, eseguiti con `pixi run test-graph`. Se Neo4j non è
raggiungibile vengono saltati con messaggio esplicito. La suite del codice
(`pixi run test`) li esclude automaticamente (`-m "not graph"`).
