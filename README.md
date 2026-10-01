# Press Reputation

Pipeline locale per l’estrazione strutturata di contenuti da PDF di rassegne stampa. Gestisce testo PDF estraibile e OCR selettivo, conserva dati intermedi e provenance, classifica le regioni e produce bozze di articolo quando dispone di evidenze sufficienti.

**Stato attuale:** la pipeline termina con `PageRecord[]`, link di continuazione e `ArticleDraft[]` candidati. Il modello `ArticleRecord` esiste, ma la CLI non produce ancora record finalizzati. Il modulo di reputation scoring è separato e non costituisce una sentiment analysis già integrata all’estrazione.

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "percorso/rassegna.pdf"
```

Per generare anche gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "percorso/rassegna.pdf" --debug-bbox
```

L’opzione `--output-dir` permette di scegliere la directory dei risultati; il valore predefinito è `results/`.

## Flusso implementato

```text
PDF
  │
  ├─ DocumentProfiler
  │    misura testo PDF e copertura immagini per pagina
  │
  ├─ DoclingParser
  │    conversione del PDF con OCR disattivato
  │    └─ per le sole pagine selezionate:
  │       rasterizzazione della pagina → conversione con OCR
  │
  ├─ PageNormalizer
  │    Docling → PageRecord e Region
  │
  ├─ merge_extraction
  │    deduplicazione e fusione testo PDF/OCR
  │
  ├─ ReviewIndexParser
  │    lettura delle tabelle indice dal raw Docling
  │
  ├─ PageProcessingPipeline
  │    PdfStyleEnricher
  │    DocumentBoilerplateDetector
  │    per ogni pagina:
  │      TechnicalRegionClassifier
  │      MetadataSeedClassifier
  │      HeaderMetadataZoneDetector
  │      ArticleSemanticClassifier
  │      MetadataExtractor
  │      PageClassifier
  │    ReviewIndexMatcher
  │    WebMainContentResolver
  │    WebArticleContinuationResolver
  │    ArticleFlowResolver
  │    per ogni pagina:
  │      BodyContinuationResolver
  │      BodyGroupingResolver
  │
  ├─ ArticleDraftAssembler
  ├─ analisi delle miniature di posizione dell'articolo
  └─ JSON e overlay opzionali
```

L’orchestrazione dell’estrazione e del salvataggio è in `src/press_reputation/cli.py`; quella delle classificazioni e della ricostruzione intermedia è in `src/press_reputation/pipeline.py`.

## 1. Profiling e OCR selettivo

### Decisione per pagina

`profiling/document_profiler.py` usa PyMuPDF per misurare:

- parole presenti nel livello testuale del PDF;
- parole nella fascia centrale della pagina, escludendo header e footer;
- quota di pagina occupata da immagini, stimata su una griglia.

`DocumentProfilingConfig` contiene le soglie. L’OCR viene richiesto quando non c’è testo PDF ma è presente un’immagine significativa, oppure quando il testo nella parte centrale è scarso e la copertura delle immagini è elevata. Una pagina con poche parole **non** viene automaticamente inviata a OCR in assenza di altri indizi.

### Estrazione

`parsers/docling_parser.py` esegue una conversione Docling con `do_ocr=False`. Per ciascuna pagina selezionata crea inoltre un PDF temporaneo di una sola pagina, composto dalla sua immagine rasterizzata, e lo converte con OCR attivo. Il PDF temporaneo mantiene le dimensioni della pagina originale.

`normalization/page_normalizer.py` normalizza separatamente i risultati. Le regioni testuali riportano `extraction_method="pdf_text"` oppure `"ocr"`; il metodo compare anche nella provenance. Per le regioni OCR, la CLI registra la pagina temporanea, rimappa la provenance alla pagina PDF originale e assegna un prefisso agli ID per evitare collisioni con quelli del passaggio nativo.

`profiling/merge.py` evita alcune duplicazioni confrontando sovrapposizione delle bbox e somiglianza del testo. Assegna poi al profilo finale della pagina:

| Valore | Significato |
| --- | --- |
| `native_pdf` | Il testo utilizzato proviene dal livello testuale PDF. |
| `ocr` | Il testo utilizzato proviene soltanto dal passaggio OCR. |
| `mixed` | Viene utilizzato testo di entrambi i passaggi. |
| `image_only` | Sono presenti immagini, ma non testo utilizzabile. |
| `null` | Non sono stati rilevati né testo né immagini utilizzabili. |

**Precisazione:** `pdf_text` indica il canale da cui la pipeline ha letto il testo. Non dimostra che il PDF fosse nato digitale: potrebbe contenere un livello OCR incorporato in precedenza.

## 2. Modelli e provenance

I modelli principali sono in `models/page.py`:

- `PageRecord`: numero di pagina PDF, dimensioni, `page_type`, `extraction_profile`, fonte, dati del ritaglio, sezione e regioni;
- `Region`: tipo semantico, testo, bbox, label Docling originale, `extraction_method`, provenance, stile, flag di esclusione, metadata di classificazione e identificativi intermedi;
- `SourceInfo`: nome della fonte, tipo, data di pubblicazione, pagina originale e URL;
- `ClippingInfo`: foglio corrente/totale e superficie, quando disponibili.

`PageType` descrive la funzione editoriale della pagina (`index`, `clipping`, `web`, `pure_text`, `unknown`); è indipendente da `extraction_profile.kind`, che descrive **come è stato ottenuto il testo**.

La convenzione interna delle bbox è `[x0, y0, x1, y1]`, con origine in alto a sinistra e coordinate in punti PDF. Il normalizer converte le coordinate Docling quando necessario. Conserva inoltre riferimenti come `self_ref`, label originale, `charspan`, bbox raw e pagina. Gli output Docling raw restano disponibili separatamente.

## 3. Classificazione delle regioni e metadata

### Stile e boilerplate

`PdfStyleEnricher`, in `style/pdf_style_enricher.py`, tenta di associare alle regioni informazioni tipografiche ricavate dal PDF: font, dimensioni, bold, italic, colore e opacità. Le informazioni sono opzionali, in particolare sulle pagine OCR.

`DocumentBoilerplateDetector`, in `classification/boilerplate_detector.py`, analizza testi ricorrenti nel documento e annota le regioni riconosciute come boilerplate prima delle classificazioni locali.

### Ordine della classificazione locale

1. **`TechnicalRegionClassifier`** riconosce elementi tecnici come watermark, rights notice, pubblicità e miniature della posizione dell’articolo. Protegge dal testo articolo le regioni escluse.
2. **`MetadataSeedClassifier`** identifica segnali espliciti di `SOURCE_NAME`, `PRESS_REVIEW_PROVIDER`, `PUBLICATION_DATE`, `ORIGINAL_PAGE`, `CLIPPING_SHEET` e `HEADER_METADATA`. Salva il metodo di rilevamento nei metadata della regione.
3. **`HeaderMetadataZoneDetector`** usa i seed situati nella fascia alta per delimitare una zona header. Registra `in_header_metadata_zone`, limite e ruolo della regione. Per impostazione corrente richiede che l’intera bbox della regione sia compresa nella zona prima di bloccarne la promozione editoriale.
4. **`ArticleSemanticClassifier`** classifica il contenuto editoriale, fra cui titolo, sottotitolo, autore, location, body e heading interni. Le regioni metadata e quelle bloccate nella zona header non devono diventare componenti dell’articolo. Il nome `RegionClassifier` resta disponibile nel codice per compatibilità.
5. **`MetadataExtractor`** popola `page.source`, `page.clipping` e `page.section` dai tipi metadata e da candidati circoscritti. Non usa l’intero testo pagina come fallback generale per date o URL. Le date OCR impossibili vengono ignorate senza interrompere l’elaborazione; l’estrazione della sezione considera soltanto label esatte in footer, header metadata o header zone.
6. **`PageClassifier`** assegna il tipo editoriale della pagina in base alle feature aggregate.

La presenza di un flag di zona non implica che tutto il testo in alto sia un metadata certo: i valori raw, le bbox e gli indizi della decisione rimangono disponibili per il debug.

## 4. Indice della rassegna

`review_index/parser.py` legge il **raw Docling**, non il testo delle regioni `PageRecord`. Questo è necessario perché nell’esempio presente nel repository l’indice è una tabella `document_index` i cui contenuti sono in `tables[].data.grid`; la regione tabella normalizzata ha `text=null`.

Il parser attuale considera le prime pagine configurate e riconosce tabelle `document_index` a cinque colonne:

1. data di pubblicazione;
2. fonte;
3. pagina originale seguita dal titolo;
4. autore;
5. pagina iniziale nella rassegna.

Produce `ReviewIndexEntry` con riferimenti a tabella, riga e celle originali. Il campo `category` è previsto, ma il parser attuale lo lascia `null`: non attribuisce a ogni riga un’intestazione generale della rassegna senza un’associazione verificabile.

Dopo la classificazione locale, `ReviewIndexMatcher` confronta le entry con le regioni `ARTICLE_TITLE`. La somiglianza del titolo combina confronto di caratteri e parole; data, fonte e pagina originale forniscono ulteriori evidenze o contraddizioni. I match sufficientemente forti e non ambigui vengono salvati e annotati come `review_index_prior` sulla **regione titolo**.

L’indice è un *prior*, non ground truth: attualmente il match non sovrascrive titolo, autore o fonte estratti dalla pagina e non crea da solo un collegamento multipagina.

## 5. Contenuto web e ricostruzione intermedia

Per le pagine classificate `WEB`, `WebMainContentResolver` distingue contenuto principale e regioni esterne, annotando `content_scope` e altri indizi di layout. `WebArticleContinuationResolver` cerca catene candidate fra pagine web adiacenti, usando titolo, body, fonte, data, URL e geometria. `ArticleFlowResolver` valuta ulteriormente i confini della continuazione: un link può rimanere `candidate`, diventare `accepted` oppure `rejected`.

Successivamente `BodyContinuationResolver` tenta di recuperare frammenti `UNKNOWN` vicini a body già identificati, usando colonna, larghezza, stile e distanza. `BodyGroupingResolver` assegna alle regioni body gruppi, colonne e `body_reading_order` **locali alla pagina**; non fonde le bbox originali.

`ArticleDraftAssembler` raggruppa le regioni che dispongono di `metadata["article_candidate_id"]`, ordina i frammenti body e produce `ArticleDraft` con segmenti, charspan, provenance, link e warning. Non genera una bozza per ogni pagina o per ogni titolo: se manca un candidate ID o un body idoneo, non produce quel draft. I draft sono candidati e non sono approvati automaticamente per lo scoring.

## 6. Output e debug

Per un PDF chiamato `rassegna.pdf`, la CLI scrive sotto `results/rassegna/`:

```text
results/rassegna/
├── profiling/
│   └── document.json
├── raw/
│   ├── document.json
│   └── ocr_page_NNN.json          # solo per pagine OCR elaborate
├── pages/
│   └── page_NNN.json
├── flow/
│   ├── links.json
│   ├── article_drafts.json
│   ├── review_index_entries.json
│   └── review_index_matches.json
└── debug/
    └── page_NNN.png              # solo con --debug-bbox
```

Gli overlay visualizzano bbox, tipo regione e, quando presenti, `content_scope` e informazioni sulla catena candidata. Le provenance complete e i dettagli delle decisioni sono nei JSON, non tutti nelle etichette dell’overlay.

`image_analysis/position_thumbnail.py` arricchisce le miniature tecniche con indizi sulla posizione dell’articolo nella pagina originale; questo arricchimento viene eseguito dalla CLI prima del salvataggio dei `PageRecord`.

## 7. Configurazione

`src/press_reputation/config.py` contiene configurazioni per:

- profiling e OCR (`DocumentProfilingConfig`);
- matching dell’indice (`ReviewIndexConfig`);
- geometria della zona metadata (`HeaderMetadataConfig`);
- alcune soglie di classificazione (`RegionClassificationConfig`).

Alcuni componenti hanno ancora proprie configurazioni o valori interni, per esempio `WebContinuationConfig`, `BodyGroupingConfig` e la soglia del `BodyContinuationResolver`. **Non tutte le soglie sono già centralizzate né ogni campo di `config.py` è necessariamente usato da tutti i componenti.**

## 8. Stato e limiti noti

- `ArticleRecord` è definito in `models/article.py`, ma non esiste ancora un passaggio CLI `ArticleDraft → ArticleRecord`.
- Il collegamento e il clustering sono più sviluppati per i casi web che per i clipping cartacei multipagina. `Region.article_id` e `metadata["article_candidate_id"]` non rappresentano ancora un’identità articolo unificata.
- L’ordine del body prodotto dal raggruppamento è locale alla pagina e prevalentemente geometrico; non costituisce una garanzia generale di reading order su layout complessi.
- Il recupero corrente di frammenti body non è un’espansione iterativa della catena: confronta i candidati con i body disponibili all’inizio del passaggio.
- Il parser dell’indice supporta al momento lo schema tabellare Docling a cinque colonne implementato in `review_index/parser.py`; indici con altri layout possono non produrre entry.
- Il fuzzy matching dell’indice è applicato al **titolo**. La fonte viene confrontata con normalizzazione ed exact match, non con un sistema generale di alias e fuzzy lookup.
- Non è ancora implementata una fase dedicata che produca `body_raw` e `body_clean` con dehyphenation e ricostruzione dei paragrafi.
- Esistono i moduli `reputation/`, ma la CLI di parsing non esegue sentiment analysis target-aware né calcola automaticamente un Media Reputation Score dagli `ArticleDraft`.

**Principio operativo:** conservare raw Docling, profili di estrazione, regioni originali, provenance, indizi e warning. Le decisioni incerte devono restare visibili nei dati intermedi, anziché essere presentate come articoli finali certi.