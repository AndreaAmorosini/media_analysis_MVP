# Reputation index Gori — dashboard

Dashboard per l'analisi del sentiment della rassegna stampa (Data Stampa "Evidenze Rassegna").

- `estrazione/` — estrazione degli articoli dai PDF (sommario, OCR delle pagine di giornale, articoli web via URL o euristica + LLM)
- `backend/` — classificazione del sentiment (Laya) e generazione degli indici per la dashboard
- `frontend/` — dashboard (HTML + JS + ECharts, nessuna build)
- `docs/schema_articolo.md` — formato JSON degli articoli

I dati (`dati/`) non sono nel repository. Avvio, dalla cartella principale:

    python3 server.py        # http://localhost:8080/frontend/
