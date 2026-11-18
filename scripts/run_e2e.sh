#!/usr/bin/env bash
# E2E completo PressKG: dal dataset al grafo canonico arricchito.
#
# Concatena: preflight -> init-db -> vincoli canonici -> ingest
# -> validazione (LLM giudici, commit) -> resolution pipeline (apply)
# -> arricchimenti -> audit di integrita' del grafo.
#
# Di default elabora TUTTO il dataset; i parametri via env restringono
# la selezione (stesso criterio di `python -m presskg.cli ingest`):
#   E2E_SKIP=10      salta i primi N articoli (default: nessuno)
#   E2E_LIMIT=20     elabora al massimo N articoli (default: tutti)
#   E2E_OUT=...      --out della pipeline (default output/resolution_e2e)
#   VALIDATION_DIR   file CSV della validazione (default output/semantic_validation/e2e)
#
# Uso:
#   pixi run e2e                        # dataset completo
#   E2E_SKIP=10 E2E_LIMIT=20 pixi run e2e   # articoli 11-30
#
# NOTA: la pipeline gira con --apply e scrive sul DB puntato da .env
# (NEO4J_URI); la validazione materializza i ValidatedFact (VALIDATION_COMMIT=1).
# Il preflight blocca subito se DB o server LLM non sono raggiungibili.

set -euo pipefail

SKIP="${E2E_SKIP:-0}"
LIMIT="${E2E_LIMIT:-}"
OUT="${E2E_OUT:-output/resolution_e2e}"

export VALIDATION_DIR="${VALIDATION_DIR:-output/semantic_validation/e2e}"
# la validazione materializza i ValidatedFact (senza commit la pipeline
# non troverebbe il batch)
export VALIDATION_COMMIT=1

echo "=== E2E PressKG: skip=$SKIP limit=${LIMIT:-tutti} out=$OUT ==="
echo "=== DB: $(pixi run python -c 'from presskg.config import CFG; print(CFG.neo4j_uri)') ==="

echo "=== 0/8 Preflight ==="
pixi run preflight

# l'E2E e' pensato per un DB pulito: il gate di validazione rifiuta di
# sovrascrivere ClaimValidation esistenti (Conflitto: campo ...) e la
# pipeline fallirebbe su artefatti/batch gia' presenti. Con DB sporco
# serve esplicitamente E2E_ALLOW_DIRTY=1 (consapevolezza del rischio).
if [[ "${E2E_ALLOW_DIRTY:-0}" != "1" ]]; then
    counts="$(pixi run python - <<'PY'
from presskg.common.db import open_driver

driver = open_driver()
try:
    with driver.session() as s:
        print(
            s.run(
                "MATCH (a:Article) WITH count(a) AS articles "
                "OPTIONAL MATCH (v:ClaimValidation) "
                "RETURN articles, count(v) AS validations"
            ).single()
        )
finally:
    driver.close()
PY
)"
    articles="$(echo "$counts" | grep -oP "articles=\K[0-9]+")"
    validations="$(echo "$counts" | grep -oP "validations=\K[0-9]+")"
    if [[ "${articles:-0}" != "0" || "${validations:-0}" != "0" ]]; then
        echo "ERRORE: il DB NON e' vuoto ($articles Article, $validations ClaimValidation)." >&2
        echo "L'E2E da stato sporco produce conflitti nel gate di validazione." >&2
        echo "Svuotare il DB (MATCH (n) DETACH DELETE n) o rilanciare con E2E_ALLOW_DIRTY=1." >&2
        exit 2
    fi
fi

echo "=== 1/8 Init DB (vincoli ingest) ==="
pixi run init-db

echo "=== 2/8 Vincoli grafo canonico (idempotenti) ==="
pixi run python - <<'PY'
from presskg.common.db import open_driver

driver = open_driver()
try:
    with driver.session() as s:
        s.run(
            "CREATE CONSTRAINT canonical_entity_projection_key IF NOT EXISTS "
            "FOR (c:CanonicalEntity) REQUIRE c.projection_key IS UNIQUE"
        )
        s.run(
            "CREATE CONSTRAINT canonical_fact_projection_key IF NOT EXISTS "
            "FOR ()-[r:CANONICAL_FACT]-() REQUIRE r.projection_key IS UNIQUE"
        )
finally:
    driver.close()
PY

# opzioni di selezione articoli: passate all'ingest solo se impostate
ingest_opts=()
[[ "$SKIP" != "0" ]] && ingest_opts+=(--skip "$SKIP")
[[ -n "$LIMIT" ]] && ingest_opts+=(--limit "$LIMIT")

echo "=== 3/8 Ingest ${LIMIT:+(max $LIMIT articoli, skip $SKIP)} ==="
pixi run python -m presskg.cli ingest "${ingest_opts[@]}"

echo "=== 4/8 Validazione claim (LLM giudici + commit + ValidatedFact) ==="
# run_validation_full.sh con VALIDATION_COMMIT=1 include anche la
# materializzazione del ValidatedFactBatch (baseline della resolution)
pixi run validation-full

echo "=== 5/8 Resolution pipeline (identity -> projection -> predicates -> events) ==="
pixi run resolution --out "$OUT" --apply --save-state

echo "=== 6/8 Arricchimenti (enrich + normalize-predicates) ==="
pixi run python -m presskg.cli enrich --apply
pixi run python -m presskg.cli normalize-predicates --apply

echo "=== 7/8 Audit integrita' grafo ==="
pixi run test-graph

echo "=== 8/8 Statistiche finali ==="
pixi run stats

echo ""
echo "E2E completato."
echo "Artifatti pipeline: $OUT"
echo "Validazione:       $VALIDATION_DIR"
