# Audit Report — media_analysis_MVP

Audit completo del codice della repository `AndreaAmorosini/media_analysis_MVP`, including tutti i branch remoti. Analisi effettuata script per script (non limitata al README), più scansioni automatiche (pattern di sicurezza, `compileall`, ispezione non invasiva dei file `.env*`).

- Verifica sintattica: `python3 -m compileall src scripts tests` → **OK** (nessun errore).
- Suite pytest non eseguibile nell'ambiente di audit (nessun pixi/pytest disponibile): la verifica runtime resta a livello di compile + code review.
- Data audit: branch `main` @ `99abd67` ("Initial Refactoring plus Initial Test Suite").

---

## 1. Panoramica repository e branch

| Branch | Head | Contenuto |
|---|---|---|
| `main` | `99abd67` | Pipeline `press_reputation` completa (~19.600 righe Python in 60+ moduli), 2 script, 9 file di test (~106 funzioni test) |
| `dashboard` | `e912715` | Dashboard web (frontend statico + `server.py` + backend sentiment/adattatore), 1 commit; base vecchia (manca ~104 file di main) |
| `knowledge_graph_analysis` | `60ab47b` | Stack `presskg` completo (~218 file, +51k righe): estrazione LLM, validazione, entity/event resolution, Neo4j via docker-compose |
| `reddit` | `982c5d4` | Modulo `subreddit_recommender/` (embedding e5-large-instruct, ranking con peso attività) + pacchetto `reputation/` |

Storico `main`: 20+ commit di refactoring iterativo guidato da taratura su corpus reale (metriche Gori_4→Gori_6 documentate nel README §17).

---

## 2. Architettura di `main`

**Entry point:** `src/press_reputation/cli.py` (Typer) — comando `parse <pdf>` con `--output-dir`, `--debug-bbox`, `--ocr-policy {auto,aggressive,all}`, `--web-first`, `--allow-untitled-records`.

Catena reale (dal codice):

1. `profiling/document_profiler.py` — PyMuPDF: parole native, copertura immagini su griglia 32×32, decide OCR per pagina.
2. `parsers/docling_parser.py` — due `DocumentConverter`: native (`do_ocr=False`) + OCR selettivo su pagine rasterizzate a 200 DPI (RapidOCR). Fail-closed se il backend OCR manca.
3. `normalization/page_normalizer.py` — DoclingDocument → `PageRecord`/`Region` con provenance, conversione bbox BOTTOMLEFT, `TextFragmentRecovery` per charspan (frammenti ambigui marcati `auto_classification_allowed=False`).
4. `profiling/merge.py` — dedup regioni pdf/ocr (testo casefold + overlap bbox ≥ 0,60), calcola `extraction_profile.kind`.
5. `review_index/parser.py` (tabelle Docling) con fallback `review_index/layout_parser.py` (righe fuse `FUSED_ROW`, limiti `START_PAGE_MAX=60`, `ORIG_PAGE_MAX=120`).
6. `pipeline.py` — `PageProcessingPipeline.process()` orchestra ~30 resolver in ordine fisso.
7. `reconstruction/article_draft_assembler.py` → `article_text_normalizer.py` → `article_finalizer.py` → (opzionale, `--web-first`) `web_retrieval/article_enricher.py` → `metadata/article_date_backfill.py`.
8. Output JSON strutturato: `pages/`, `flow/`, `articles/`, `profiling/`, `raw/`, `debug/`.

**Modelli** (`models/`): pydantic v2. `PageRecord`/`Region` conservano provenance raw (`self_ref`, `charspan`, `bbox`); `ArticleRecord` ha `finalization_status` (`ready|review_required`), `provenance`, `field_origins`, `extraction_completeness`. Approccio "conserva raw e provenance, decidi dopo".

**Configurazione:** `config.py` (344 righe) — tutte le soglie in modelli pydantic (`DocumentProfilingConfig`, `ReviewIndexConfig`, `TitleResolutionConfig`, `ArticleClusteringConfig`, `ExtractionCompletenessConfig`, ecc.).

---

## 3. Audit script per script (`main`)

### 3.1 Profiling, parsing, normalizzazione
- `profiling/document_profiler.py` — selección OCR per pagina su testo sparso + copertura immagini; policy `auto/aggressive/all`.
- `profiling/merge.py` — dedup OCR/native; **code smell**: doppia import di `PageRecord, Region` (righe 2 e 4).
- `parsers/docling_parser.py` — fail-closed (`RuntimeError`) se RapidOCR non disponibile; salva raw JSON Docling. `parsers/base.py` ABC minimale.
- `normalization/page_normalizer.py` — mapping label Docling → `RegionType`; unmapped intervals → regioni `UNKNOWN` con `ambiguous`.
- `normalization/text_fragment.py` — allineamento charspan con tolleranza overlap 3 char (`TextFragmentRecoveryConfig`).
- `normalization/watermark_scrub.py` — token di registro (DS####, DATASTAMPA####) attestati in regioni non editoriali rimossi dal testo anche fusi nelle parole; `scrub_rights_phrase` per varianti "riproduzione riservata". Azioni tracciate in metadata.
- `normalization/title_cleanup.py` — `clean_article_title`: codici registro fusi (prefisso/coda/cifre incuneate), token duplicati; Levenshtein + dizionario comuni per varianti OCR.

### 3.2 Features e lookup
- `features/region_features.py`, `features/page_features.py` — feature testuali/layout (marker, URL, maiuscolo, font, bbox relativi); `include_entity=False` evita lookup superflui.
- `lookup.py` — entity resolution su `resources/` (`newspaper.json` 295 KB, `agenzie_rassegna_stampa_italia.json`, `comuni_italiani.json` 740 KB) con `lru_cache`; catena exact→alias→fuzzy con soglie per lunghezza e `match_coverage` anti-falso-positivo; flag `ambiguous` su margine stretto. Nota: `normalize_key` (riga 267) e `normalize_entity_key` coesistono — inconsistenza di naming, non un bug.

### 3.3 Classification (15 resolver)
- `technical_region_classifier.py` — watermark score a componenti; notice diritti; ads; miniature tecniche con esclusione regioni interne. **Confermato:** `WatermarkDetectionConfig.minimum_editorial_overlap = 2` (`config.py`) ma l'overlap è 0..1 → componente `editorial_overlap` mai attiva (ammesso anche dal README §16).
- `page_classifier.py` — `PageType` (CLIPPING/WEB/PURE_TEXT/INDEX/UNKNOWN); marker strutturali solo su regioni non editoriali; `refine_document` propaga WEB a pagine UNKNOWN adiacenti.
- `region_classifier.py` (`ArticleSemanticClassifier`, alias `RegionClassifier`) — seed body (≥18 parole, font ratio 0,75–1,30, veto bold/uppercase/metadata). **Code smell:** controlli duplicati (CAPTION/IMAGE/thumbnail in set e in if separati), `import re` dentro 3 metodi statici.
- `metadata_seed_classifier.py`, `header_metadata_zone.py`, `metadata/metadata_extractor.py` — metadata solo da regioni circoscritte, no fallback sul body. `#TODO: Da rivedere` aperto a `metadata_extractor.py:25`.
- `title_resolver.py` (613 righe) — scoring a componenti + veti timbro registro (`is_stamp_code_text`) e frasi chrome web; `consolidate_candidates`.
- `subtitle_resolver.py` — veti breadcrumb `Home > …` e chrome web (glifi, contatori, date, byline); protezione body seed.
- `author_resolver.py` — byline "di/da/a cura di", name-like, prior dall'indice. **Confermato:** `config.py` contiene `"archivio storcio"` invece di `"archivio storico"`.
- `section_header_resolver.py`, `editorial_header_resolver.py`, `editorial_header_arbitrator.py` — stack occhiello/titolo/deck, banner ≥55% larghezza, soppressione titoli non-main.
- `editorial_preview_boundary.py`, `editorial_preview_relation.py`, `web_chrome_resolver.py`, `url_harvester.py` (rifiuta URL con credenziali), `web_article_zone_resolver.py`, `web_main_content.py` (scope main/related/advertisement/navigation/non_main), `inline_intrusion.py`, `pull_quote_resolver.py`, `watermark_text_cleaner.py`, `boilerplate_detector.py` (fingerprint cross-pagina).

### 3.4 Reconstruction (18 moduli — il cuore)
- `article_clustering.py` (1170 righe, il più grande) — assegnazione `article_id` con score/evidenze/alternative in metadata; fusione headline duplicate (`duplicate_headline_v1`); bootstrap multicolonna; prior dall'indice.
- `body_continuation.py`, `body_grouping.py`, `body_membership_refinement.py` (655 righe), `column_membership_propagation.py`, `global_article_membership.py`, `editorial_fragment_refinement.py` (recupero "colonna interpolata"), `orphan_proximity_fallback.py` (ultima risorsa, marcata `proximity_fallback_v1` → review).
- `web_article_continuation.py` — score a componenti per pagine web adiacenti, incl. "fogli WEB sequenziali" (`WEB n`→`n+1`).
- `article_flow.py` — link cross-page con boundary sintattico. **Il README §9/§16 dice che manca `links.append(link)`: nel codice corrente il bug NON c'è più** — l'append è presente su tutti e 4 i percorsi (righe 100, 110, 117, 156). Documentazione superata. Resta un code smell: `from_region_id`/`to_region_id` assegnati due volte.
- `newspaper_continuation.py` (`1/2 → 2/2` prova forte), `anchored_web_continuation.py`, `general_continuation.py`, `cross_page_article_reconciler.py`, `article_integrity.py` (downgrade link fantasma).
- `article_reading_order.py`, `article_draft_assembler.py`, `article_text_normalizer.py` (dehyphenation conservativa, drop cap, protezione URL/email/decimali), `article_finalizer.py` (881 righe: draft+pagine+indice → `ArticleRecord`, soglie `ready` calibrate, recupero untitled, tail attachment, dedup shingle), `extraction_completeness.py` (coverage body ≥0,90; retention OCR ≥0,94, 0,92 se tutto OCR).

### 3.5 Web retrieval, review index, altri moduli
- `web_retrieval/article_fetcher.py` — **sicurezza SSRF eccellente**: rifiuta IP literal, host locali/`.internal`/`.test`, URL con credenziali, porte non standard; DNS re-check `is_global`; redirect max 3 same-site, no downgrade HTTPS→HTTP; cap 4 MB; timeout 12 s; `trust_env=False`; rilevamento paywall (JSON-LD/microdata); trafilatura `favor_precision`. `article_enricher.py` usa HTML pubblico solo se titolo simile (≥0,62) e testo ≥0,82× del PDF, preservando `body_pdf_raw`.
- `review_index/matcher.py` — similarità 0,65 char + 0,35 token + forma "squeezed" anti-OCR; override a evidenza forte (fonte+data+pagina) con pavimento 0,50.
- `style/pdf_style_enricher.py` — span PyMuPDF → stile regioni (non attribuisce span native a regioni OCR); `image_analysis/position_thumbnail.py`; `visualization/bbox_overlay.py`; `metadata/article_date_backfill.py`.

### 3.6 Script e test
- `scripts/batch_parse.py` (628 righe) — runner batch robusto: subprocess isolato con argv a lista (no `shell=True`), env controllato, staging + replace atomico con backup, manifest con fingerprint sha256 del codice per resume, CSV (`documents.csv`, `articles.csv`, `audit_template.csv`), sommari mese/anno.
- `scripts/evaluate_audit.py` — valuta audit manuale (6 campi) con disclaimer metodologico corretto (precision sui soli artículos reviewerati, non recall).
- `tests/` — 9 file con fixture sintetiche (`conftest.py`, pagine A4-like senza PDF/Docling): classification, reconstruction, normalization, lookup, models, features, review_index, pipeline smoke; `test_corpus.py` opzionale via `--data-root`. Nessun test usa dati reali di default.

---

## 4. Audit dei branch alternativi

### 4.1 `dashboard`
`server.py` su **127.0.0.1** (locale, OK); `backend/sentiment.py` client stdlib verso server Laya su `127.0.0.1:8010` (batch ≤64); `backend/adattatore_evidenze.py` auto-dichiarato **provvisorio** (regex sul sommario "Evidenze"); vari script in `estrazione/`. Base vecchia rispetto a main.

### 4.2 `knowledge_graph_analysis` ⚠️ — finding di sicurezza
Stack `src/presskg/` (~50k righe): estrazione LLM (Unsloth), `validation/`, `resolution/` (~30 moduli: evidence matching, constrained clustering, canonical graph consolidation, integrity gates), `graph_store.py` (Neo4j), `docker-compose.yaml`, `profiles/*.env`.

1. 🔴 **`.env.example` contiene valori non-placeholder**: `NEO4J_PASSWORD` (42 char, 19 caratteri distinti — compatibile con una password reale), `NEO4J_URI`, `UNSLOTH_BASE_URL` (24 char, plausibile host interno). Probabili **credenziali reali committate**. Azione: ruotare le credenziali, ripulire lo storico git (`git filter-repo`/BFG), aggiungere `.env*` a `.gitignore`. (I valori non sono stati stampati né loggati in questo audit.)
2. 🔴 `docker-compose.yaml` di default: `NEO4J_server_default__listen__address: 0.0.0.0` e porte pubblicate su `${NEO4J_BIND_ADDRESS:-0.0.0.0}` → **Neo4j esposto su tutte le interfacce di default**. Vincolare a `127.0.0.1`/rete privata.
3. `profiles/4070.env` / `5090.env` contengono solo tuning `PRESSKG_*` (verificato: nessun secret).

### 4.3 `reddit`
`subreddit_recommender/` standalone (numpy/pandas/sentence-transformers): pulizia dump Reddit (`01_analisi/`), confronto modelli + `suggerisci.py` (top-N subreddit per notizia, score similarità + `alfa`×attività, default 0,2), `src/candidati.py`, `src/sorgenti.py`, `src/valutazione.py` (metriche@k), CSV in `data/`. Codice pulito; branch datato rispetto a main.

---

## 5. Sicurezza e qualità transverse (`main`)

- ✅ Nessun `eval`/`exec`/`pickle`/`shell=True`; nessuna bare `except`; 2 soli `except Exception`, entrambi con log/raise.
- ✅ Web fetch con guard SSRF solide; URL con user:password rifiutati anche in `url_harvester.py`.
- ✅ `.gitignore` copre `Data/`, `results/`, `.pixi/`, `__pycache__/`; nessun secret in `main`.
- ✅ Dipendenze pin in `pixi.toml` (docling-slim 2.126.0, onnxruntime-gpu 1.31.0, ecc.), Python 3.12.
- ⚠️ `ruff` è in dipendenze ma **manca qualsiasi file di config** (no `pyproject.toml`/`.ruff.toml` in main) → lint non configurato.
- ⚠️ Code smell minori: import duplicati (`merge.py`; `classification/__init__.py` importa `RegionClassifier` due volte; `pipeline.py` importa `FlowLink` due volte); `import re` dentro metodi in `region_classifier.py`; controlli ridondanti in `classify()`; doppio assegnamento in `article_flow.py`; alias `RegionClassifier = ArticleSemanticClassifier`.
- ⚠️ Limiti noti confermati nel codice (README §16): `minimum_editorial_overlap=2` (componente morta), "archivio storcio" (typo), ~22 titoli con codici residui, ~95 voci indice senza clipping estratto, `extraction_confidence` spesso null, nessun sentiment/MRS in CLI. La nota sul bug `article_flow` è **superata** (il codice ora aggiunge i link su tutti i percorsi).

---

## 6. Raccomandazioni (ordine di priorità)

1. 🔴 Branch `knowledge_graph_analysis`: ruotare subito le credenziali presenti in `.env.example` (e ovvio riutilizzate), ripulire lo storico git, ignorare `.env*`, cambiare default docker-compose da `0.0.0.0` a `127.0.0.1`.
2. Aggiornare README §9/§16 (bug `article_flow` risolto nel codice); correggere `minimum_editorial_overlap` (2 → frazione 0,02–0,05) e "archivio storcio" → "archivio storico" in `config.py`.
3. Aggiungere config ruff + pre-commit; ripulire import duplicati e il TODO in `metadata_extractor.py:25`.
4. Eseguire `pixi run pytest` in ambiente con dipendenze (non possibile nell'audit) e aggiungere test per `scripts/`.
5. Mantenere il contratto downstream: consumare `ArticleRecord` (campo `body`), non ricostruire da `Region[]`; preservare raw + provenance.

---

*Audit in sola lettura: nessuna modifica al codice sorgente. Questo file è l'unico cambiamento introdotto, su branch dedicato.*
