
#!/usr/bin/env bash
set -euo pipefail

ROOT="${VALIDATION_DIR:-output/semantic_validation/qwen38_full}"

# giudici per ruolo: id letti dalla configurazione (.env) oppure override
# espliciti; nessun modello hardcoded nello script.
JUDGE1_ID="${VALIDATION_JUDGE1_ID:-}"
if [[ -z "$JUDGE1_ID" ]]; then
    JUDGE1_ID="$(pixi run python -c 'from presskg.config import CFG; print(CFG.eval_model_id)')"
fi
JUDGE2_ID="${VALIDATION_JUDGE2_ID:-}"
if [[ -z "$JUDGE2_ID" ]]; then
    JUDGE2_ID="$(pixi run python -c 'from presskg.config import CFG; from presskg.validation.batch import DEFAULT_SECONDARY_JUDGE as D; print(CFG.secondary_eval_model_id or D)')"
fi

# batch id derivato dai modelli effettivi della catena
# (ingest da CFG + giudice primario + giudice secondario), vedi
# presskg.validation.batch; override con VALIDATION_BATCH_ID
if [[ -n "${VALIDATION_BATCH_ID:-}" ]]; then
    BATCH_ID="$VALIDATION_BATCH_ID"
else
    BATCH_ID="$(pixi run python -c \
        'import sys; from presskg.validation.batch import derive_batch_id; print(derive_batch_id(judge_model=sys.argv[1], secondary_model=sys.argv[2]))' \
        "$JUDGE1_ID" "$JUDGE2_ID")"
fi

PRIMARY="$ROOT/primary.csv"
SELECTED="$ROOT/secondary_ids.csv"
SECONDARY="$ROOT/secondary.csv"
CANDIDATES="$ROOT/correction_candidates.jsonl"
VERIFIED="$ROOT/correction_verified.jsonl"

mkdir -p "$ROOT"

if [[ "${VALIDATION_ONLY_APPLY:-0}" != "1" ]]; then

    : "${JUDGE1_ID:?Imposta il giudice primario (VALIDATION_JUDGE1_ID o UNSLOTH_EVAL_MODEL)}"
    : "${JUDGE2_ID:?Imposta il giudice secondario (VALIDATION_JUDGE2_ID o UNSLOTH_EVAL_MODEL_BIS)}"

    if [[ "$JUDGE1_ID" == "$JUDGE2_ID" ]]; then
        echo "I due giudici devono essere differenti" >&2
        exit 2
    fi

    echo "=== 1/7 Validazione primaria (primo valutatore) ==="

    opts=()
    [[ -f "$PRIMARY" ]] && opts+=(--resume)

    pixi run python -m presskg.validation primary generate \
        --model "$JUDGE1_ID" \
        --limit 0 \
        --context-chars 600 \
        --max-tokens 1600 \
        --retries 1 \
        --out "$PRIMARY" \
        "${opts[@]}"

    echo "=== 2/7 Preparazione correzioni ==="

    pixi run python -m presskg.validation corrections prepare \
        --primary "$PRIMARY" \
        --out "$CANDIDATES" \
        --context-chars 600 \
        --overwrite

    echo "=== 3/7 Selezione claim secondari ==="

    pixi run python -m presskg.validation decisions \
        select-secondary \
        --primary "$PRIMARY" \
        --out "$SELECTED" \
        --skip-correction-proposals

    echo "=== 4/7 Secondo giudizio (secondo valutatore) ==="

    selected_count=$(($(wc -l < "$SELECTED") - 1))

    if (( selected_count > 0 )); then
        opts=()
        [[ -f "$SECONDARY" ]] && opts+=(--resume)

        pixi run python -m presskg.validation primary generate \
            --model "$JUDGE2_ID" \
            --claim-ids-file "$SELECTED" \
            --limit 0 \
            --context-chars 600 \
            --max-tokens 1600 \
            --retries 1 \
            --out "$SECONDARY" \
            "${opts[@]}"
    else
        if [[ ! -f "$SECONDARY" ]]; then
            pixi run python - "$SECONDARY" <<'PY'
import csv
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from presskg.validation.primary import FIELDS

with Path(sys.argv[1]).open(
    "w", newline="", encoding="utf-8"
) as file:
    csv.writer(file).writerow(FIELDS)
PY
        fi
    fi

    echo "=== 5/7 Riverifica correzioni ==="

    opts=()
    [[ -f "$VERIFIED" ]] && opts+=(--resume)

    if [[ -s "$CANDIDATES" ]]; then
        pixi run python -m presskg.validation corrections verify \
            --candidates "$CANDIDATES" \
            --model "$JUDGE2_ID" \
            --out "$VERIFIED" \
            --context-chars 600 \
            --max-tokens 1600 \
            --timeout 600 \
            --retries 1 \
            "${opts[@]}"
    else
        touch "$VERIFIED"
    fi

else
    echo "=== Modalità ONLY APPLY ==="

    for file in "$PRIMARY" "$SECONDARY" "$CANDIDATES" "$VERIFIED"; do
        [[ -f "$file" ]] || {
            echo "File mancante: $file" >&2
            exit 2
        }
    done
fi

write_opts=()

if [[ "${VALIDATION_COMMIT:-0}" == "1" ]]; then
    write_opts+=(--commit)
    echo "=== COMMIT Neo4j ==="
else
    echo "=== DRY RUN ==="
fi

echo "=== 6/7 Gate automatico ==="

pixi run python -m presskg.validation decisions apply \
    --primary "$PRIMARY" \
    --secondary "$SECONDARY" \
    --batch-id "$BATCH_ID" \
    --context-chars 600 \
    "${write_opts[@]}"

echo "=== 7/7 Gate delle correzioni ==="

pixi run python -m presskg.validation corrections apply \
    --primary "$PRIMARY" \
    --secondary "$SECONDARY" \
    --candidates "$CANDIDATES" \
    --verified "$VERIFIED" \
    --batch-id "$BATCH_ID" \
    --context-chars 600 \
    "${write_opts[@]}"

if [[ "${VALIDATION_COMMIT:-0}" == "1" ]]; then
    echo "=== 8/8 Materializzazione ValidatedFact ==="
    # con COMMIT=1 la validazione completa anche lo stato del grafo che la
    # resolution pipeline legge come baseline: i gate 6-7 persistono solo
    # ClaimValidation/correzioni, il ValidatedFactBatch nasce qui
    # (idempotente, batch-id come sopra)
    pixi run python -m presskg.validation materialize \
        --batch-id "$BATCH_ID" \
        --commit
fi

echo "Pipeline terminata."
echo "Risultati: $ROOT"
