# Press Reputation

Pipeline locale per l’estrazione strutturata di articoli da PDF di rassegne stampa. Combina testo PDF estraibile e OCR selettivo, conserva dati raw e provenance, classifica le regioni, attribuisce identità articolo e produce bozze con segmenti ordinati.

**Stato attuale:** la CLI produce `PageRecord`, link di continuazione, sequenze di reading order e `ArticleDraft` candidati. `ArticleRecord` è definito nei modelli, ma non è ancora generato dalla CLI. Le bozze non sono approvate automaticamente per sentiment analysis o reputation scoring.

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf"
```

Per generare anche gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf" --debug-bbox
```

L’opzione `--output-dir` cambia la directory dei risultati; il valore predefinito è `results/`.

## Flusso implementato

```text
PDF
  │
  ├─ DocumentProfiler
  │    misura testo PDF e copertura immagini per pagina
  │
  ├─ DoclingParser
  │    conversione senza OCR
  │    └─ OCR delle pagine selezionate dal profiler
  │
  ├─ PageNormalizer
  │    documento Docling → PageRecord e Region
  │
  ├─ merge_extraction
  │    deduplicazione e fusione di testo PDF/OCR
  │
  ├─ ReviewIndexParser
  │    legge l'indice tabellare dal raw Docling
  │
  ├─ PageProcessingPipeline
  │    PdfStyleEnricher
  │    DocumentBoilerplateDetector
  │
  │    per ogni pagina:
  │      TechnicalRegionClassifier
  │      MetadataSeedClassifier
  │      HeaderMetadataZoneDetector
  │      ArticleSemanticClassifier
  │      MetadataExtractor
  │
  │    TitleResolver
  │    SubtitleResolver
  │    ReviewIndexMatcher
  │    AuthorResolver
  │    PageClassifier
  │
  │    WebMainContentResolver
  │    InlineIntrusionDetector
  │    WebArticleContinuationResolver
  │    ArticleClusteringResolver: attribuzione locale
  │    TitleResolver.consolidate_candidates
  │    ArticleFlowResolver
  │    ArticleClusteringResolver: link web accettati
  │    NewspaperContinuationResolver
  │
  │    per ogni pagina:
  │      BodyContinuationResolver
  │      ArticleClusteringResolver: body recuperati
  │      SectionHeaderResolver
  │      ArticleClusteringResolver: heading e media
  │      BodyGroupingResolver
  │
  │    ArticleReadingOrderResolver
  │
  ├─ ArticleDraftAssembler
  ├─ analisi delle miniature di posizione dell'articolo
  └─ JSON e overlay opzionali
```

`src/press_reputation/cli.py` orchestra estrazione, salvataggio e assemblaggio dei draft. `src/press_reputation/pipeline.py` definisce l’ordine delle classificazioni e dei resolver.

**Attenzione:** la delimitazione locale dei moduli related in `WebMainContentResolver` presenta attualmente un errore di indentazione, documentato nella sezione «Limiti e correzioni aperte».

## 1. Document profiling e OCR selettivo

`profiling/document_profiler.py` usa PyMuPDF per misurare su ogni pagina:

- parole leggibili dal livello testuale PDF;
- parole nella zona centrale, escludendo fasce di header e footer;
- copertura delle immagini.

Le soglie sono in `DocumentProfilingConfig`. L’OCR viene richiesto quando il testo PDF è assente in presenza di immagini significative oppure quando il testo centrale è scarso rispetto al contenuto immagine. Poche parole, da sole, non impongono OCR.

`DoclingParser` converte il PDF con OCR disattivato. Per ogni pagina selezionata converte inoltre una copia temporanea image-only con OCR attivo. I due risultati sono normalizzati separatamente e fusi da `profiling/merge.py`, che confronta bbox e somiglianza testuale per evitare alcune duplicazioni.

| `extraction_profile.kind` | Significato |
| --- | --- |
| `native_pdf` | Testo utilizzato dal livello testuale PDF. |
| `ocr` | Testo utilizzato soltanto dal passaggio OCR. |
| `mixed` | Testo utilizzato da entrambi i passaggi. |
| `image_only` | Immagini presenti, ma nessun testo utilizzabile. |
| `null` | Nessun testo o immagine utile rilevato. |

`Region.extraction_method` e la provenance indicano `pdf_text`, `ocr` o `unknown`. Per il passaggio OCR sono conservati anche la pagina temporanea e la mappatura alla pagina PDF originale.

`pdf_text` descrive il canale letto dalla pipeline, non dimostra che il PDF fosse nato digitale: potrebbe avere un livello OCR preesistente.

## 2. Modelli, bbox e provenance

I modelli principali sono in `models/page.py`:

- `PageRecord`: numero di pagina PDF, dimensioni, tipo editoriale, profilo di estrazione, fonte, clipping, sezione e regioni;
- `Region`: tipo, testo, bbox, label Docling originale, metodo di estrazione, provenance, stile, flag di esclusione, metadata di classificazione e `article_id` opzionale;
- `SourceInfo`: nome della fonte, tipo, data, pagina originale e URL;
- `ClippingInfo`: foglio corrente/totale e superficie, quando disponibili.

`PageType` (`index`, `clipping`, `web`, `pure_text`, `unknown`) descrive il ruolo editoriale della pagina. È distinto da `extraction_profile.kind`, che descrive l’origine del testo.

Le bbox interne usano `[x0, y0, x1, y1]`, origine in alto a sinistra e coordinate in punti PDF. Quando disponibili, sono conservati `self_ref`, label Docling, `charspan`, bbox raw e pagina nella provenance. Gli output Docling raw restano disponibili separatamente.

`PageNormalizer` mappa la collection Docling `tables` a `RegionType.TABLE` e conserva `metadata["table_shape"]` e `metadata["table_ref"]`. Le celle si recuperano dal raw tramite il riferimento alla tabella.

La provenance indica **da dove proviene una regione**; `metadata["article_clustering"]` indica **perché è stata attribuita a un articolo**. Nessuna delle due informazioni sostituisce l’altra.

## 3. Stile PDF e boilerplate documentale

`PdfStyleEnricher` opera prima delle classificazioni locali. Associa, quando possibile, gli span PDF alle regioni e scrive in `Region.style`:

- `font_names` e `dominant_font`;
- `median_font_size` e `max_font_size`;
- `bold_ratio` e `italic_ratio`;
- `dominant_color` e `median_opacity`;
- origine e numero degli span associati;
- quota di evidenza interpretabile per bold e italic.

Un nome di font embedded non informativo non dimostra che il testo sia normale. Lo stile non inferibile resta sconosciuto; gli span del PDF originale non vengono attribuiti alle regioni OCR.

`DocumentBoilerplateDetector` raggruppa regioni dello stesso documento tramite testo o pattern normalizzato, bbox relativa e stile quando disponibile. Gestisce famiglie quali `Data Stampa`, label di sezione e alcuni rights notice. Calcola la ripetizione su pagine PDF distinte e annota `metadata["boilerplate_detection"]`, `boilerplate` e `boilerplate_frequency`.

**Ripetizione non significa esclusione automatica:** il detector non cancella il testo raw né imposta da solo `exclude_from_article_text`. Le classificazioni successive determinano il ruolo della regione.

## 4. Classificazione tecnica, rights notice e watermark

`TechnicalRegionClassifier` riconosce miniature della posizione dell’articolo, `RIGHTS_NOTICE`, `WATERMARK` e `ADVERTISEMENT`. I tipi strutturati `TABLE`, `INFOGRAPHIC` e `PULL_QUOTE`, quando già assegnati, sono protetti dalle euristiche tecniche testuali.

### Rights notice

Il classifier distingue avvisi autonomi — per esempio `© RIPRODUZIONE RISERVATA` o `ARTICOLO NON CEDIBILE` — da blocchi misti che contengono anche testo editoriale. Conserva la decisione in `metadata["rights_notice_detection"]`. Un notice accettato è escluso dal body, ma regione, bbox e provenance restano nei `PageRecord`.

### Watermark

Il percorso attivo usa `watermark_score()` invece della sola opacità. Combina marker come `Data Stampa`, opacity, colore, frequenza fra pagine, posizione, geometria, overlap con testo plausibilmente editoriale ed evidenze negative. Sono richiesti sia uno score sufficiente sia supporto non puramente visivo: testo editoriale chiaro non deve diventare watermark solo perché ha bassa opacity.

Score, componenti ed esito restano in `metadata["watermark_detection"]`. `RIGHTS_NOTICE` ha precedenza rispetto a `WATERMARK`.

**Limite noto:** `WatermarkDetectionConfig.minimum_editorial_overlap` è attualmente `int = 2`, mentre l’overlap calcolato varia tra `0` e `1`. Il relativo componente dello score non può quindi attivarsi finché la soglia non diventa una frazione appropriata. Nel classifier rimane anche il vecchio metodo `looks_like_watermark()`, non chiamato dal percorso attivo.

## 5. Metadata, source, provider e location

`MetadataSeedClassifier` riconosce `SOURCE_NAME`, `PRESS_REVIEW_PROVIDER`, `PUBLICATION_DATE`, `ORIGINAL_PAGE`, `CLIPPING_SHEET` e `HEADER_METADATA`.

`HeaderMetadataZoneDetector` delimita una fascia superiore tramite i seed; le regioni protette nella zona non vengono normalmente promosse a titolo, subtitle, body, section header, autore o location.

`MetadataExtractor` popola fonte, clipping e sezione usando regioni metadata e candidati circoscritti. Non usa l’intera pagina come fallback generale per data o URL.

`lookup.resolve_entity()` restituisce un risultato strutturato con categoria, metodo, nome canonico, similarity, `match_coverage` e ambiguità. Applica gli stage nell’ordine:

1. exact source;
2. source alias;
3. fuzzy source;
4. exact provider;
5. provider alias;
6. fuzzy provider;
7. exact location;
8. fuzzy location.

`match_coverage` misura quanta parte dell’intera regione è spiegata dalla località: «Napoli» non trasforma automaticamente «CRONACHE DI NAPOLI» in `LOCATION`. Le soglie fuzzy dei comuni dipendono dalla lunghezza del nome e sono configurabili.

I componenti che non necessitano di entità possono chiamare `RegionFeatureExtractor.extract(..., include_entity=False)` evitando un lookup superfluo, senza registrare un match negativo.

## 6. Review Index

`ReviewIndexParser` legge il documento Docling raw: nell’esempio presente nel repository le celle dell’indice sono in `tables[].data.grid`, mentre la regione tabella normalizzata può avere `text=null`.

Il parser corrente riconosce nelle prime pagine tabelle `document_index` a cinque colonne: data, fonte, pagina originale e titolo, autore, pagina iniziale della rassegna. Produce `ReviewIndexEntry` con valori e riferimenti alle celle. `category` è prevista nel modello ma non viene attribuita da un’intestazione generale senza associazione verificabile.

`ReviewIndexMatcher` confronta le entry con i titoli proposti. L’indice rimane una **prior**, non ground truth: non sovrascrive automaticamente titolo, fonte o autore. Può essere usato come evidenza dal collegamento multipagina cartaceo quando l’entry è associata al titolo anchor.

## 7. Titolo, subtitle, autore e section header

`TitleResolver` combina label Docling, dimensione font relativa al body, bold, geometria, posizione, vicinanza al body e somiglianza con l’indice. Salva score e componenti in `metadata["title_candidate"]`. Non impone un titolo unico per pagina; quando esiste un’identità articolo può consolidare il main title per quel gruppo.

`SubtitleResolver` cerca candidati sopra **e** sotto ciascun titolo locale. Usa distanza, overlap orizzontale, larghezza, posizione prima del body e stile. Le assegnazioni ambigue restano esplicite in `metadata["subtitle_resolution"]`.

`AuthorResolver` valuta byline esplicite e nomi impliciti tramite forma del nome, posizione rispetto a title/subtitle e body, stile e possibile autore dell’indice già associato al titolo. L’autore indicato dall’indice non crea una regione che non esiste nel PDF. Il risultato è in `metadata["author_resolution"]`.

`SectionHeaderResolver` opera dopo il recupero del body. Cerca heading brevi tra body precedente e successivo compatibili, salvando score, tipo precedente, ID dei body adiacenti e stato dell’associazione in `metadata["section_header_resolution"]`.

## 8. Clustering e articoli cartacei multipagina

`ArticleClusteringResolver` assegna `Region.article_id` senza modificare testo o provenance. Crea anchor locali dai titoli, collega subtitle e author ai rispettivi header e confronta i body con articoli concorrenti. Score, alternativa e metodo sono registrati in `metadata["article_clustering"]`. In mancanza di evidenza sufficiente, `article_id` resta `null`.

Per il web riusa le catene candidate e i link accettati. Per i ritagli cartacei interviene successivamente `NewspaperContinuationResolver`:

- confronta fogli PDF adiacenti dello stesso documento;
- usa `sheet_current/sheet_total`, source, data, pagina originale, Review Index e fingerprint dell’eventuale titolo ripetuto;
- considera `foglio 1/2 → foglio 2/2` un’evidenza molto forte, salvo contraddizioni;
- non richiede title o body sul secondo foglio;
- può attribuire l’`article_id` a immagini e caption del foglio successivo senza trasformarle in body.

I link `candidate` e `accepted` sono in `flow/links.json`. Anche le decisioni rifiutate e le contraddizioni vengono salvate in `flow/newspaper_continuation_decisions.json`, senza far scartare automaticamente l’intero draft a causa di un link cartaceo rejected.

Una pagina di sole immagini senza marker del foglio leggibile **non** viene collegata automaticamente per sola adiacenza. Se un link è accettato ma la pagina non produce regioni utilizzabili, la pagina può comunque comparire in `ArticleDraft.pdf_pages`; non vengono inventati media o testo.

## 9. Web main content e intrusion inline

`WebMainContentResolver` seleziona la colonna principale usando body seed e, quando possibile, titolo, URL e continuità della colonna fra pagine. Mantiene distinti gli scope:

| Scope | Significato |
| --- | --- |
| `main` | Contenuto attribuito all’area principale. |
| `related` | Modulo o contenuto correlato esterno al body principale. |
| `advertisement` | Contenuto pubblicitario. |
| `navigation` | Navigazione riconosciuta semanticamente. |
| `non_main` | Regione fuori area principale o prima del suo inizio. |
| `unknown` | Regione non ancora risolta. |

Un modulo related dovrebbe essere delimitato **localmente**: non dovrebbe rendere `related` tutto ciò che segue fino alla fine della pagina. **Nel codice corrente questa delimitazione richiede ancora la correzione di indentazione indicata nei limiti sotto.**

`InlineIntrusionDetector` è inserito dopo `WebMainContentResolver` e prima della continuazione web. Esamina regioni `UNKNOWN` o `ARTICLE_BODY` nella stessa area, comprese fra due body `main` compatibili. Combina:

- marker di related content o pubblicità;
- lunghezza;
- aspetto da link;
- font, dimensione, bold o italic diversi dai body vicini;
- distanza dai body;
- debole evidenza di discontinuità lessicale.

Un marker esplicito oppure link-like formatting insieme a differenza di stile deve supportare lo score. Un semplice cambio lessicale non basta. Una regione accettata diventa `RELATED_CONTENT` o `ADVERTISEMENT`, riceve lo scope corrispondente ed è esclusa dal body; la valutazione rimane in `metadata["inline_intrusion"]`, con ID dei due body anchor, score ed evidenze.

L’intrusione **non** termina l’articolo: i body prima e dopo possono conservare lo stesso `article_id` e rimanere nella sequenza ordinata. `BodyGroupingResolver` può separarli in gruppi geometrici locali senza interrompere il draft.

Il detector richiede due body già riconosciuti nella stessa pagina e area. Non identifica tutte le intrusion all’inizio o alla fine dell’articolo e non rende automaticamente `main` ogni `UNKNOWN` nella colonna.

## 10. Body seed e recupero iterativo

La classificazione semantica distingue il body affidabile trovato localmente da quello recuperato:

- `ARTICLE_BODY` con `metadata["body_role"]="seed"`: abbastanza testo, stile compatibile quando noto, nessun marker tecnico/editoriale esterno;
- `UNKNOWN`: regione conservata, anche quando è più breve della soglia seed;
- `ARTICLE_BODY` con `body_role="continuation"`: frammento recuperato da `BodyContinuationResolver`.

La soglia indicativa di 18 parole è configurata in `RegionClassificationConfig` per individuare seed, **non** per vietare che un blocco corto diventi body.

`BodyContinuationResolver` richiede un body di riferimento con `article_id`. Valuta candidati `UNKNOWN` tramite articolo, colonna, prossimità in avanti, overlap, larghezza, font dominante, font size, bold/italic e barriere locali di reading order. Usa una frontiera ordinata: ogni candidato promosso diventa immediatamente riferimento per quelli rimasti, fino a convergenza.

Conserva metodo, score, riferimento, round della promozione e componenti in `Region.metadata`; candidature ambigue o insufficienti restano `UNKNOWN`. Le bbox non vengono fuse.

Sulle pagine web, un `UNKNOWN` deve attualmente avere `content_scope="main"` per essere recuperato come continuation. Il filtro delle intrusion non allenta automaticamente questo requisito: brevi frammenti genuini con scope ancora `unknown` possono quindi rimanere fuori dal body.

## 11. Reading order per articolo e ArticleDraft

`BodyGroupingResolver` mantiene gruppi e colonne locali alla pagina, utili anche per il debug. Dopo il grouping, `ArticleReadingOrderResolver` lavora per `(document_id, article_id)`:

1. rileva colonne dai body attribuiti;
2. ordina le colonne da sinistra a destra;
3. ordina le regioni dentro ciascuna colonna;
4. inserisce `ARTICLE_SECTION_HEADER`;
5. tratta regioni che attraversano più colonne come separatori di bande;
6. produce una sequenza ordinata anche su più pagine PDF.

Ogni `ReadingOrderSegment` conserva tipo, pagina PDF, colonna, ordine, bbox, testo, confidence, metodo, ID della regione e provenance. L’ordine è annotato anche in `Region.metadata["article_reading_order"]`.

`ArticleDraftAssembler` usa questi segmenti per generare `body`, charspan e `DraftSegment` tipizzati. Il body del draft può quindi contenere anche gli section header nella posizione ordinata. Rimane testo **raw**: la normalizzazione `body_raw → body_clean` è una fase distinta non ancora implementata.

Il reading order non include ancora immagini, tabelle, pull quote, infographic e caption come segmenti multimodali. Tali regioni possono appartenere all’articolo e restare visibili nei `PageRecord` senza essere concatenate al body testuale.

## 12. Tipi di regione strutturati

| Tipo | Politica rispetto al body |
| --- | --- |
| `ARTICLE_SECTION_HEADER` | Heading interno incluso nella sequenza testuale ordinata. |
| `WATERMARK`, `RIGHTS_NOTICE` | Testo tecnico escluso; regione originale conservata. |
| `ADVERTISEMENT`, `RELATED_CONTENT`, `NAVIGATION` | Fuori dal body dell’articolo principale. |
| `PULL_QUOTE` | Citazione evidenziata non concatenata automaticamente. |
| `TABLE` | Struttura riferita al raw Docling; non trattata come prosa. |
| `INFOGRAPHIC` | Media editoriale distinto; non equivale a ogni `IMAGE`. |

`PULL_QUOTE` e `INFOGRAPHIC` sono tipi disponibili e protetti, non categorie con detector automatici completi.

## 13. Output e debug

Per `rassegna.pdf`, la CLI scrive sotto `results/rassegna/`:

```text
results/rassegna/
├── profiling/
│   └── document.json
├── raw/
│   ├── document.json
│   └── ocr_page_NNN.json
├── pages/
│   └── page_NNN.json
├── flow/
│   ├── links.json
│   ├── reading_order.json
│   ├── article_drafts.json
│   ├── newspaper_continuation_decisions.json
│   ├── review_index_entries.json
│   └── review_index_matches.json
└── debug/
    └── page_NNN.png              # con --debug-bbox
```

`raw/ocr_page_NNN.json` esiste soltanto per le pagine elaborate con OCR. Gli overlay sono opzionali; score, scope, identità articolo, provenance, alternative e decisioni dettagliate sono nei JSON.

La CLI esegue anche `image_analysis/position_thumbnail.py` prima di salvare i `PageRecord`.

## 14. Configurazione

`src/press_reputation/config.py` contiene configurazioni per profiling/OCR, Review Index, header metadata, title/subtitle/author/section header, fuzzy entity lookup, watermark, boilerplate, clustering, body continuation, reading order e continuazione cartacea.

`classification/web_content_config.py` contiene le soglie di selezione del contenuto web e dell’`InlineIntrusionDetector`. Alcuni componenti, fra cui `BodyGroupingResolver` e la continuazione web, mantengono configurazioni locali. Le soglie non sono ancora tutte calibrate sul corpus.

## 15. Limiti e correzioni aperte

- **Correzione necessaria nel contenuto web:** in `classification/web_main_content.py` il ciclo `for region in usable` che dovrebbe delimitare il modulo related è attualmente fuori dal ciclo `for marker in page.regions`. Senza marker può leggere `marker_box` non inizializzato; con più marker considera soltanto l’ultimo. Indentare il blocco all’interno del ciclo sui marker prima di fare affidamento sul nuovo filtro web.
- `ArticleRecord` non viene ancora finalizzato dalla CLI.
- Il collegamento cartaceo è conservativo: richiede un articolo identificabile e prove compatibili. L’assenza di titolo/body sul secondo foglio è supportata; l’assenza di marker e altre evidenze non autorizza un link automatico.
- L’`InlineIntrusionDetector` richiede body prima e dopo sulla stessa pagina e area; brevi `UNKNOWN` web non vengono automaticamente recuperati se non hanno scope `main`.
- Il reading order è costruito da bbox e article ID. Non può riordinare il testo *all’interno* di una bbox multi-colonna indivisa né attribuire un ordine multimodale completo a media e caption.
- `BodyGroupingResolver` produce ancora un ordine locale per pagina; quello autorevole per il draft è la sequenza di `ArticleReadingOrderResolver`.
- Non esiste ancora una fase dedicata per dehyphenation, ricostruzione dei paragrafi e pulizia OCR del body.
- `WatermarkDetectionConfig.minimum_editorial_overlap` è attualmente `2`, incompatibile con un overlap compreso fra `0` e `1`.
- La lista negativa dell’`AuthorResolver` contiene attualmente `archivio storcio` anziché `archivio storico`.
- La CLI di parsing non esegue sentiment analysis target-aware né calcola automaticamente il Media Reputation Score dagli `ArticleDraft`.

**Principio operativo:** conservare raw Docling, profili, testo e bbox originali, provenance, score, indizi, alternative e warning. Una decisione provvisoria o un dato assente non devono essere presentati come un `ArticleRecord` finale certo.