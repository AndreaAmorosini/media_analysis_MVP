# Press Reputation

Pipeline locale per l'estrazione strutturata di articoli da PDF di rassegne stampa. Combina testo PDF estraibile e OCR selettivo, conserva raw e provenance, classifica le regioni, attribuisce identità articolo e produce bozze e record finalizzati.

**Stato attuale:** la CLI salva `PageRecord`, decisioni e link di continuazione, sequenze di reading order, `ArticleDraft` e `ArticleRecord`. Il record finale può avere stato `ready` oppure `review_required`. Le euristiche e gli score di ogni fase sono documentati in dettaglio in [`EURISTICHE.md`](EURISTICHE.md).

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf"
```

Per generare gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf" --debug-bbox
```

L'opzione `--output-dir` cambia la directory dei risultati; il valore predefinito è `results/`.

### Batch su intere cartelle

```bash
PYTHONPATH=src pixi run python scripts/batch_parse.py \
  --input-root Data --output-root Risultati_Gori_6 \
  --web-first --allow-untitled-records --force
```

Il runner parallelizza i PDF (worker ridotti in base alla VRAM libera, OCR su GPU via onnxruntime-gpu consolidato in `pixi.toml`), scrive `documents.csv`, `articles.csv` e i sommari per anno, e riprende i PDF già riusciti senza `--force`.

### Export dei soli articoli

```bash
PYTHONPATH=src pixi run python scripts/export_articles.py \
  --input-root Risultati_Gori_6 --output-root Export_Gori_6
```

Copia solo i `article_*.json` finali preservando la struttura `anno/mese/[giorno]/documento/`.

## Struttura del progetto

Il codice è organizzato **per fase della pipeline**, non per tecnologia:

```text
src/press_reputation/
├── core/                     # fondamenta condivise
│   ├── models/               #   PageRecord, Region, ArticleRecord
│   ├── config.py             #   tutte le Config con le soglie
│   ├── lookup.py             #   entity lookup + text_similarity
│   └── resources/            #   JSON: testate, comuni, agenzie
│
├── ingestion/                # PDF → PageRecord normalizzati
│   ├── parsers/              #   DoclingParser (testo + OCR selettivo)
│   ├── profiling/            #   DocumentProfiler, merge_extraction
│   ├── normalization/        #   PageNormalizer, title_cleanup, watermark_scrub
│   ├── style/                #   PdfStyleEnricher (font, bold, colori dagli span PDF)
│   └── image_analysis/       #   miniature di posizione articolo
│
├── features/                 # estrazione feature di pagina e regione (geometria, testo)
│
├── classification/           # classificazione pagine e regioni
│   ├── page_classifier.py    #   PageType: INDEX / CLIPPING / WEB / UNKNOWN
│   ├── region_classifier.py  #   ArticleSemanticClassifier (body seed, ruoli)
│   ├── technical_region_classifier.py  # watermark, rights notice, advertisement
│   ├── metadata_seed_classifier.py     # source, data, provider, header metadata
│   ├── boilerplate_detector.py         # pattern ricorrenti cross-pagina
│   ├── header_metadata_zone.py         # fascia tecnica superiore
│   ├── scopes.py             #   BLOCKED_SCOPES + is_blocked_scope (condivisi)
│   └── web/                  # tutto ciò che è specifico layout web
│       ├── web_chrome_resolver.py
│       ├── web_main_content.py         # scope main/related/navigation/...
│       ├── web_article_zone_resolver.py
│       ├── web_content_config.py
│       ├── url_harvester.py
│       └── inline_intrusion.py
│
├── enrichment/               # risoluzione semantica di testata
│   ├── title_resolver.py           # score titolo + veto timbri/chrome web
│   ├── subtitle_resolver.py        # veto breadcrumb e chrome
│   ├── author_resolver.py
│   ├── section_header_resolver.py
│   ├── pull_quote_resolver.py
│   ├── editorial_header_resolver.py
│   ├── editorial_header_arbitrator.py
│   ├── editorial_preview_boundary.py
│   ├── editorial_preview_relation.py
│   └── watermark_text_cleaner.py
│
├── reconstruction/           # assemblaggio degli articoli
│   ├── clustering/           # nucleo di attribuzione article_id
│   │   ├── clustering_core.py       # helper condivisi + score_body
│   │   ├── clustering_columns.py    # colonne, multicolonna, bootstrap
│   │   ├── clustering_assignment.py # assegnazione locale body/titoli/media
│   │   ├── clustering_flow.py       # link cross-pagina web
│   │   └── facade.py                # ArticleClusteringResolver (interfaccia)
│   ├── continuation/         # continuazione cross-pagina
│   │   ├── body_continuation.py
│   │   ├── newspaper_continuation.py
│   │   ├── web_article_continuation.py
│   │   ├── anchored_web_continuation.py
│   │   ├── general_continuation.py
│   │   └── cross_page_article_reconciler.py
│   ├── membership/           # assegnazione body agli articoli
│   │   ├── body_membership_refinement.py
│   │   ├── column_membership_propagation.py
│   │   ├── global_article_membership.py
│   │   ├── orphan_proximity_fallback.py
│   │   └── editorial_fragment_refinement.py
│   └── assembly/             # bozza → record finale
│       ├── article_draft_assembler.py
│       ├── article_finalizer.py
│       ├── article_flow.py
│       ├── article_integrity.py
│       ├── article_reading_order.py
│       ├── article_text_normalizer.py
│       ├── body_grouping.py
│       ├── extraction_completeness.py
│       └── flow_models.py
│
├── review_index/             # parsing indice rassegna e matching titoli
├── web_retrieval/            # fetch e arricchimento articoli web (trafilatura)
├── metadata/                 # estrazione metadata + backfill data pubblicazione
├── utils/                    # overlay bbox di debug (--debug-bbox)
├── pipeline.py               # orchestratore: PageProcessingPipeline
└── cli.py                    # entry point
```

**Principi strutturali:**

- ogni cartella corrisponde a una **fase della pipeline**, così il flusso di `pipeline.py` si legge specchiandosi nell'albero delle cartelle;
- `core/` raccoglie modelli, config e helper condivisi: nessun modulo di fase importa da un'altra fase "a caso", ma può attingere a `core/` e `features/`;
- `reconstruction/` è diviso per **responsabilità** (clustering / continuation / membership / assembly), non per file storico;
- `classification/web/` separa le euristiche specifiche del layout web dal resto della classificazione;
- `enrichment/` separa la risoluzione semantica di testata (titolo, sottotitolo, autore, header) dalla classificazione tecnica delle regioni.

## Flusso

```text
PDF
  │
  ├─ core: DocumentProfiler (profiling/ in ingestion/)
  ├─ ingestion/parsers: DoclingParser — estrazione senza OCR + OCR selettivo
  ├─ ingestion/normalization: PageNormalizer e merge_extraction
  ├─ review_index: ReviewIndexParser dal raw Docling
  ├─ review_index: LayoutReviewIndexParser (fallback regioni)
  │
  ├─ PageProcessingPipeline (pipeline.py)
  │    ingestion/style: PdfStyleEnricher
  │    classification: DocumentBoilerplateDetector
  │
  │    per ogni pagina:
  │      classification: TechnicalRegionClassifier
  │      classification: MetadataSeedClassifier
  │      classification: HeaderMetadataZoneDetector
  │      classification: ArticleSemanticClassifier (region_classifier)
  │      metadata: MetadataExtractor
  │
  │    enrichment: TitleResolver (veto timbri/frasi web)
  │    enrichment: SubtitleResolver (veto breadcrumb/chrome)
  │    enrichment: EditorialHeaderArbitrator (banner + deck)
  │    review_index: ReviewIndexMatcher (similarità robusta OCR)
  │    enrichment: AuthorResolver
  │    classification: PageClassifier
  │
  │    classification/web: WebMainContentResolver, InlineIntrusionDetector
  │    reconstruction/continuation: WebArticleContinuationResolver
  │    reconstruction/clustering: attribuzione locale, headline duplicate
  │    enrichment: TitleResolver.consolidate_candidates
  │    reconstruction/assembly: ArticleFlowResolver
  │    reconstruction/clustering: link web accettati
  │    reconstruction/continuation: NewspaperContinuationResolver
  │
  │    per ogni pagina:
  │      reconstruction/continuation: BodyContinuationResolver
  │      reconstruction/clustering: body recuperati
  │      reconstruction/membership: BodyMembershipRefinement / ColumnMembershipPropagation
  │      reconstruction/membership: GlobalArticleMembershipResolver
  │      enrichment: SectionHeaderResolver
  │      reconstruction/clustering: heading e media
  │      reconstruction/assembly: BodyGroupingResolver
  │
  │    reconstruction/continuation: CrossPageArticleReconciler
  │    reconstruction/continuation: AnchoredWebContinuationResolver
  │    reconstruction/continuation: GeneralContinuationResolver
  │    reconstruction/membership: OrphanProximityFallbackResolver
  │    reconstruction/membership: EditorialFragmentRefinementResolver
  │    reconstruction/assembly: ArticleIntegrityReconciler
  │    ingestion/normalization: scrub_watermark_residue
  │    reconstruction/assembly: ArticleReadingOrderResolver
  │
  ├─ reconstruction/assembly: ArticleDraftAssembler
  ├─ reconstruction/assembly: ArticleTextNormalizer
  ├─ reconstruction/assembly: ArticleFinalizer
  ├─ web_retrieval: WebArticleEnricher
  ├─ ingestion/image_analysis: miniature di posizione
  └─ JSON e overlay opzionali
```

`cli.py` orchestra estrazione, assemblaggio, normalizzazione, finalizzazione e salvataggio. `pipeline.py` definisce l'ordine di classificazione e ricostruzione delle pagine.

## Concetti chiave per fase

### 1. Profiling, OCR e provenance (`ingestion/`)

`DocumentProfiler` usa PyMuPDF per misurare testo estraibile e copertura delle immagini; `DocumentProfilingConfig` decide su quali pagine richiedere OCR (testo breve da solo non basta). `PageNormalizer` conserva bbox e riferimenti raw; `merge_extraction` confronta bbox e testo prima di fondere.

| `extraction_profile.kind` | Significato |
| --- | --- |
| `native_pdf` | Testo del livello testuale PDF. |
| `ocr` | Testo del solo passaggio OCR. |
| `mixed` | Testo di entrambi. |
| `image_only` | Immagini presenti, nessun testo utilizzabile. |
| `null` | Nessun testo o immagine utile. |

`Region.extraction_method` distingue `pdf_text`, `ocr`, `unknown`; la provenance conserva pagina, bbox raw, `self_ref`, `charspan` e mappatura pagina OCR → pagina PDF.

### 2. Modelli (`core/models/`)

- `PageRecord`: pagina, fonte, clipping, profilo di estrazione, regioni;
- `Region`: tipo, testo, bbox `[x0,y0,x1,y1]` in punti PDF (origine in alto a sinistra), label Docling, stile, extraction method, provenance, metadata di classificazione, `article_id` opzionale;
- `PageType`: funzione editoriale della pagina, indipendente dal profilo OCR.

`Region.metadata["article_clustering"]` documenta perché una regione è stata attribuita a un articolo; `Region.provenance` l'origine del contenuto.

### 3. Classificazione (`classification/`)

`PdfStyleEnricher` arricchisce `Region.style` dagli span PDF (font, dimensione, bold/italic, colore, opacità). `DocumentBoilerplateDetector` annota pattern ricorrenti per posizione e frequenza. `TechnicalRegionClassifier` riconosce miniature, `RIGHTS_NOTICE`, `WATERMARK`, `ADVERTISEMENT` con score su marker, opacity, colore, ripetizione e posizione. `MetadataSeedClassifier` identifica `SOURCE_NAME`, provider, data, pagina originale, `CLIPPING_SHEET`, `HEADER_METADATA`.

`lookup.resolve_entity()` (`core/lookup.py`) valuta nell'ordine: exact source → alias → fuzzy source → exact provider → alias → fuzzy provider → exact location → fuzzy location, conservando metodo, similarity, ambiguità e `match_coverage`.

### 4. Review Index (`review_index/`)

`ReviewIndexParser` legge le tabelle `document_index` (indici dei contenuti nella raccolta) nel raw Docling (5 colonne: data, fonte, pagina originale + titolo, autore, pagina iniziale), se presente. Le pagine indice riconosciute vengono tipizzate `INDEX` e le righe escluse dal testo editoriale.

`ReviewIndexMatcher` associa le entry ai titoli con similarità robusta all'OCR (confronto normalizzato + compattato) e override a evidenza forte (fonte + data + pagina originale coincidenti). L'indice è una **prior**, non ground truth.

### 5. Enrichment di testata (`enrichment/`)

`TitleResolver` valuta label Docling, stile relativo al body, bold, geometria, posizione e similarità con l'indice, con due veti: timbri di registro («DS1667») e frasi chrome web («Clicca qui sotto…»). `EditorialHeaderArbitrator` risolve gli stack di headline prima del clustering con la regola banner ≥ 55% larghezza pagina + deck stretto sotto. `SubtitleResolver` esclude breadcrumb e chrome web. `clean_article_title` (`ingestion/normalization/title_cleanup.py`) rimuove i codici di registro fusi nel titolo in ogni posizione. `AuthorResolver` valuta byline esplicite e nomi impliciti. `SectionHeaderResolver` cerca heading interni fra body compatibili.

### 6. Ricostruzione (`reconstruction/`)

- **clustering/**: `ArticleClusteringResolver` (facade in `facade.py`) compone `clustering_core` (helper + `score_body`), `clustering_columns` (colonne, multicolonna, bootstrap), `clustering_assignment` (assegnazione locale), `clustering_flow` (link web). Assegna `article_id` con score, metodo ed evidenze nei metadata; fonde le headline duplicate quasi identiche (similarità ≥ 0,90).
- **continuation/**: `BodyContinuationResolver` (continuità in avanti per colonna/prossimità/stile), `NewspaperContinuationResolver` (fogli `1/2 → 2/2`), `WebArticleContinuationResolver` (score di evidenze + fogli WEB sequenziali), `AnchoredWebContinuationResolver` (wrapper web → stampa), `GeneralContinuationResolver` (second-chance multi-segnale), `CrossPageArticleReconciler`.
- **membership/**: `BodyMembershipRefinementResolver`, `ColumnMembershipPropagationResolver`, `GlobalArticleMembershipResolver`, `OrphanProximityFallbackResolver`, `EditorialFragmentRefinementResolver` (frammenti racchiusi e colonna interpolata).
- **assembly/**: `BodyGroupingResolver` → `ArticleReadingOrderResolver` → `ArticleDraftAssembler` → `ArticleTextNormalizer` → `ArticleFinalizer` → `ArticleRecord`.

### 7. Web (`classification/web/`, `web_retrieval/`)

`WebMainContentResolver` distingue gli scope `main`, `related`, `advertisement`, `navigation`, `non_main`, `unknown`. `InlineIntrusionDetector` trova contenuti estranei fra due body della stessa area. `WebArticleEnricher` preferisce il testo HTML pubblico (trafilatura) e assorbe i frammenti web duplicati.

### 8. Normalizzazione e finalizzazione

`ArticleTextNormalizer` produce `body_clean` da `body_raw` (dehyphenation conservativa, line break, Unicode/OCR selettivo, drop cap). `scrub_watermark_residue` rimuove dai testi i token di registro attestati nelle regioni non editoriali, anche fusi dentro le parole. `ArticleFinalizer` produce `ArticleRecord` con stato `ready` o `review_required` in base a titolo, body, metadata, confidence, completezza e link non risolti.

## Output

Per `rassegna.pdf`, la CLI scrive sotto `results/rassegna/`:

```text
results/rassegna/
├── profiling/document.json
├── raw/document.json (+ ocr_page_NNN.json se OCR)
├── pages/page_NNN.json
├── flow/                  # links, reading_order, drafts, records, decisioni di continuazione, review index
├── articles/article_NNNN.json
├── document_report.json
└── debug/page_NNN.png     # con --debug-bbox
```

Score, ID articolo, scope, provenance, alternative e warning restano ispezionabili nei JSON.

## Configurazione

`core/config.py` contiene le configurazioni per profiling/OCR, Review Index, metadata header, classificazione, entity lookup, watermark, clustering, body continuation, reading order, continuazione cartacea, normalizzazione e finalizzazione. `classification/web/web_content_config.py` configura contenuto web e intrusion. Le soglie di `ExtractionCompletenessConfig` e del matcher dell'indice sono state calibrate quantitativamente sull'intero corpus; le altre restano da tarare. Vedi [`EURISTICHE.md`](EURISTICHE.md) per il dettaglio di ogni score.

**Principio operativo:** conservare raw Docling, profili, bbox, testo originale, provenance, score, indizi e warning. Un `ArticleRecord` `review_required` resta disponibile per analisi e debugging, ma non va trattato come articolo verificato.
