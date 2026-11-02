# Avvio ingestion
pxi run ingest

#Testy di validazione del grafo
pixi run python tests/kg_quality.py all --data-dir Data --expected-articles 5415 --random 300 --diagnostid 100 --retires 1 --seed 42 --out output/evaluations/run....