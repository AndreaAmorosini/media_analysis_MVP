
#!/usr/bin/env bash
set -euo pipefail

ROOT="${VALIDATION_DIR:-output/semantic_validation/qwen38_full}"
BATCH_ID="${VALIDATION_BATCH_ID:-qwen38-gemma31-gemma12-v1}"

PRIMARY="$ROOT/gemma31_primary.csv"
SELECTED="$ROOT/secondary_ids.csv"
SECONDARY="$ROOT/gemma12_secondary.csv"
CANDIDATES="$ROOT/correction_candidates.jsonl"
VERIFIED="$ROOT/correction_verified.jsonl"

GEMMA31_ID="${VALIDATION_GEMMA31_ID:-unsloth/gemma-4-31B-it-GGUF}"
GEMMA12_ID="${VALIDATION_GEMMA12_ID:-unsloth/gemma-4-12b-it-GGUF}"

mkdir -p "$ROOT"

if [[ "${VALIDATION_ONLY_APPLY:-0}" != "1" ]]; then

    : "${GEMMA31_ID:?Imposta GEMMA31_ID}"
    : "${GEMMA12_ID:?Imposta GEMMA12_ID}"

    if [[ "$GEMMA31_ID" == "$GEMMA12_ID" ]]; then
        echo "I due giudici devono essere differenti" >&2
        exit 2
    fi

    echo "=== 1/7 Validazione primaria Gemma 31B ==="

    opts=()
    [[ -f "$PRIMARY" ]] && opts+=(--resume)

    pixi run python -m presskg.validation primary generate \
        --model "$GEMMA31_ID" \
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

    echo "=== 4/7 Secondo giudizio originali ==="

    selected_count=$(($(wc -l < "$SELECTED") - 1))

    if (( selected_count > 0 )); then
        opts=()
        [[ -f "$SECONDARY" ]] && opts+=(--resume)

        pixi run python -m presskg.validation primary generate \
            --model "$GEMMA12_ID" \
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
            --model "$GEMMA12_ID" \
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

echo "Pipeline terminata."
echo "Risultati: $ROOT"
