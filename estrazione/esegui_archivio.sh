#!/usr/bin/env bash
# Estrazione dell'intero archivio, tutte le fasi in sequenza. Riprendibile: rilanciato, salta il lavoro già fatto.
# Va lanciato in tmux, così continua anche chiudendo la sessione SSH:
#     tmux new -d -s estrazione ~/Documenti/autentica/Dashboard/estrazione/esegui_archivio.sh
# Avanzamento:  tail -f ~/Documenti/autentica/Dashboard/dati/archivio/log.txt
# Fasi: piano -> OCR con layout (due GPU) -> articoli (due GPU) -> sentiment (Laya) -> resoconto.
set -u
cd "$(dirname "$0")"
D=../dati/archivio
mkdir -p "$D"
exec >> "$D/log.txt" 2>&1
PY="env -u PYTHONPATH ../.venv/bin/python -u"
PP="env -u PYTHONPATH ../.venv-paddle/bin/python -u"

ollama_avvia() {  # $1 GPU, $2 porta
    CUDA_VISIBLE_DEVICES=$1 OLLAMA_MODELS=$HOME/.ollama/models OLLAMA_HOST=127.0.0.1:$2 \
        OLLAMA_KEEP_ALIVE=60m OLLAMA_NUM_PARALLEL=2 ollama serve >> "$D/ollama_$2.log" 2>&1 &
    echo $!
}
ollama_ferma() { kill "$@" 2>/dev/null; wait "$@" 2>/dev/null; }

echo "== $(date '+%F %T') inizio"
docker stop laya >/dev/null 2>&1   # Laya occupa la GPU 0: serve solo alla fine

echo "== $(date '+%F %T') fase 1: piano (sommari, pagine da OCR, download URL)"
if [ ! -f "$D/piano.json" ]; then
    $PY estrai_archivio.py piano || { echo "piano fallito"; exit 1; }
fi

echo "== $(date '+%F %T') fase 2: OCR con layout su due GPU"
O0=$(ollama_avvia 0 11435); O1=$(ollama_avvia 1 11436); sleep 5
CUDA_VISIBLE_DEVICES=0 $PP ocr_layout.py "$D/lavori_ocr_0.jsonl" "$D/ocr" http://127.0.0.1:11435 >> "$D/ocr_0.log" 2>&1 &
W0=$!
CUDA_VISIBLE_DEVICES=1 $PP ocr_layout.py "$D/lavori_ocr_1.jsonl" "$D/ocr" http://127.0.0.1:11436 >> "$D/ocr_1.log" 2>&1 &
W1=$!
wait $W0 $W1
ollama_ferma $O0 $O1
echo "OCR: $(find "$D/ocr" -name 'page_*.json' | wc -l) pagine in cache"

echo "== $(date '+%F %T') fase 3: articoli su due GPU (URL verificato, altrimenti PDF; modello web gemma4)"
O0=$(ollama_avvia 0 11435); O1=$(ollama_avvia 1 11436); sleep 5
OLLAMA_URL=http://127.0.0.1:11435/api/chat $PY estrai_archivio.py articoli 0 2 >> "$D/articoli_0.log" 2>&1 &
A0=$!
OLLAMA_URL=http://127.0.0.1:11436/api/chat $PY estrai_archivio.py articoli 1 2 >> "$D/articoli_1.log" 2>&1 &
A1=$!
wait $A0 $A1
ollama_ferma $O0 $O1

echo "== $(date '+%F %T') fase 3b: immagini delle pagine con i riquadri"
$PY disegna_riquadri.py >> "$D/riquadri.log" 2>&1
tail -1 "$D/riquadri.log"

echo "== $(date '+%F %T') fase 4: sentiment con Laya"
docker start laya >/dev/null
for i in $(seq 1 60); do curl -s localhost:8010/health | grep -q '"status":"ok"' && break; sleep 2; done
find ../dati -mindepth 2 -maxdepth 2 -name rassegna.json -print0 \
    | xargs -0 python3 ../backend/classifica_rassegna.py > "$D/sentiment.log" 2>&1
tail -1 "$D/sentiment.log"
docker stop laya >/dev/null

echo "== $(date '+%F %T') fase 5: resoconto"
$PY estrai_archivio.py resoconto
echo "== $(date '+%F %T') fine"
