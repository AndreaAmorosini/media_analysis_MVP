# Press Reputation

Pipeline locale per l’estrazione strutturata di articoli da PDF di rassegne stampa. Il sistema combina testo PDF estraibile e OCR selettivo, conserva raw e provenance, classifica le regioni e produce bozze di articolo quando dispone di evidenze sufficienti.

**Stato attuale:** la CLI produce `PageRecord`, link di continuazione e `ArticleDraft` candidati. `ArticleRecord` è definito nei modelli, ma non è ancora generato dalla pipeline CLI. Le bozze non costituiscono input approvati automaticamente per sentiment analysis o reputation scoring.

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "percorso/rassegna.pdf"
```

Per generare anche gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "percorso/rassegna.pdf" --debug-bbox
```

L’opzione `--output-dir` permette di cambiare la directory dei risultati; il valore predefinito è `results/`.

## Flusso implementato

```text
PDF
  │
  ├─ DocumentProfiler
  │    testo PDF e copertura immagini per pagina
  │
  ├─ DoclingParser
  │    conversione senza OCR
  │    └─ OCR delle sole pagine selezionate dal profiler
  │
  ├─ PageNormalizer
  │    documento Docling → PageRecord e Region
  │
  ├─ merge_extraction
  │    deduplicazione e fusione di testo PDF/OCR
  │
  ├─ ReviewIndexParser
  │    lettura dell'indice tabellare dal raw Docling
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
  │    WebArticleContinuationResolver
  │    TitleResolver.consolidate_candidates
  │    ArticleFlowResolver
  │
  │    per ogni pagina:
  │      BodyContinuationResolver
  │      SectionHeaderResolver
  │      BodyGroupingResolver
  │
  ├─ ArticleDraftAssembler
  ├─ analisi delle miniature di posizione dell'articolo
  └─ JSON e overlay opzionali
```

`src/press_reputation/cli.py` orchestra estrazione, salvataggio e assemblaggio dei draft. `src/press_reputation/pipeline.py` definisce l’ordine delle classificazioni e della ricostruzione intermedia.

## 1. Document profiling e OCR selettivo

`profiling/document_profiler.py` usa PyMuPDF per misurare, su ogni pagina:

- parole leggibili dal livello testuale PDF;
- parole nella parte centrale, escludendo fasce di header e footer;
- copertura delle immagini, stimata su una griglia.

Le soglie sono in `DocumentProfilingConfig`. L’OCR viene richiesto quando manca testo PDF e sono presenti immagini significative, oppure quando il testo centrale è scarso rispetto al contenuto immagine. Poche parole, da sole, non impongono OCR.

`DoclingParser` converte prima il PDF con OCR disattivato. Per ogni pagina selezionata crea anche un PDF temporaneo image-only della pagina originale e lo converte con OCR. I risultati vengono normalizzati separatamente e fusi da `profiling/merge.py`, che confronta bbox e somiglianza testuale per evitare alcune duplicazioni.

Il profilo finale della pagina può avere:

| `extraction_profile.kind` | Significato |
| --- | --- |
| `native_pdf` | Testo utilizzato dal livello testuale PDF. |
| `ocr` | Testo utilizzato soltanto dal passaggio OCR. |
| `mixed` | Testo utilizzato da entrambi i passaggi. |
| `image_only` | Immagini presenti, ma nessun testo utilizzabile. |
| `null` | Nessun testo o immagine utile rilevato. |

`Region.extraction_method` e la provenance indicano `pdf_text`, `ocr` o `unknown`. Per il passaggio OCR vengono conservati anche il riferimento alla pagina temporanea e la mappatura alla pagina PDF originale.

**Nota:** `pdf_text` descrive il canale letto dalla pipeline, non certifica che il documento fosse nato digitale. Il PDF potrebbe contenere un livello OCR incorporato in precedenza.

## 2. Modelli, bbox e provenance

I modelli principali sono in `models/page.py`:

- `PageRecord`: numero di pagina PDF, dimensioni, tipo editoriale, profilo di estrazione, fonte, informazioni sul ritaglio, sezione e regioni;
- `Region`: tipo, testo, bbox, label Docling originale, metodo di estrazione, provenance, stile, flag di esclusione, metadata di classificazione e `article_id` opzionale;
- `SourceInfo`: fonte, tipo, data, pagina originale e URL;
- `ClippingInfo`: foglio corrente/totale e superficie, se disponibili.

`PageType` (`index`, `clipping`, `web`, `pure_text`, `unknown`) riguarda il ruolo editoriale della pagina. È distinto da `extraction_profile.kind`, che riguarda l’origine del testo.

Le bbox interne usano `[x0, y0, x1, y1]`, origine in alto a sinistra e coordinate in punti PDF. Quando possibile, il normalizer conserva `self_ref`, label Docling, `charspan`, bbox raw e pagina nell’elenco `Region.provenance`. Gli output Docling raw restano disponibili separatamente.

Il `PageNormalizer` riconosce la collection Docling `tables` come `RegionType.TABLE`. Per ogni tabella conserva `metadata["table_shape"]` e `metadata["table_ref"]`; le celle strutturate sono consultabili nel documento raw tramite il riferimento alla tabella.

## 3. Stile PDF e boilerplate

`PdfStyleEnricher` precede le classificazioni locali. Legge gli span testuali del PDF con PyMuPDF e, quando riesce ad associarli geometricamente a una regione, scrive in `Region.style`:

- `font_names` e `dominant_font`;
- `median_font_size` e `max_font_size`;
- `bold_ratio` e `italic_ratio`;
- `dominant_color` e `median_opacity`;
- origine e numero degli span associati;
- quota di evidenza interpretabile per bold e italic.

Uno stile non inferibile rimane sconosciuto: un nome di font embedded non informativo non dimostra che il testo sia normale. L’enricher non attribuisce alle regioni OCR gli span del PDF originale.

`DocumentBoilerplateDetector` misura la ripetizione di testo tra pagine e annota `boilerplate` e `boilerplate_frequency`. Il testo raw non viene cancellato.

## 4. Classificazione tecnica, metadata e watermark

### Classificazione tecnica

`TechnicalRegionClassifier` riconosce `RIGHTS_NOTICE`, `WATERMARK`, `ADVERTISEMENT` e miniature tecniche della posizione dell’articolo. Le miniature vengono distinte dalle immagini editoriali e possono causare l’esclusione delle regioni contenute al loro interno.

I tipi strutturati `TABLE`, `INFOGRAPHIC` e `PULL_QUOTE`, quando già assegnati, sono protetti dalle euristiche tecniche testuali. Una normale `IMAGE` non viene trasformata automaticamente in infographic.

### Watermark scoring

Il percorso attivo per i watermark usa `watermark_score()`, non la sola opacità. Combina:

- marker forte, in particolare una regione breve che inizia con `Data Stampa`;
- marker più deboli;
- opacità e colore;
- frequenza fra pagine;
- posizione ai margini e geometria verticale;
- overlap con testo potenzialmente editoriale;
- penalità per blocchi lunghi e label Docling da heading.

Per assegnare `WATERMARK` devono essere soddisfatti **sia** la soglia di score **sia** un gate di supporto non puramente visivo. Un testo chiaro non diventa quindi watermark soltanto perché ha bassa opacità. `RIGHTS_NOTICE` ha precedenza nel classificatore tecnico.

Lo score, i componenti, la soglia e l’esito sono registrati in `Region.metadata["watermark_detection"]` quando vi sono indizi valutati.

**Limite implementativo da correggere:** in `WatermarkDetectionConfig`, `minimum_editorial_overlap` è attualmente dichiarato come `int = 2`, mentre `bbox_overlap_fraction()` restituisce un valore tra `0` e `1`. Di conseguenza il componente `editorial_overlap` non può attivarsi con il valore corrente. Il valore previsto dalla proposta era una frazione, ad esempio `float = 0.25`. Il resto del watermark score è presente, ma la sua componente overlap non è operativa finché questa configurazione non viene corretta. Nel file rimane inoltre il vecchio `looks_like_watermark()`; il flusso attivo non lo chiama.

### Metadata e fascia header

`MetadataSeedClassifier` riconosce `SOURCE_NAME`, `PRESS_REVIEW_PROVIDER`, `PUBLICATION_DATE`, `ORIGINAL_PAGE`, `CLIPPING_SHEET` e `HEADER_METADATA`.

`HeaderMetadataZoneDetector` usa i seed nella parte alta della pagina per delimitare la fascia dei metadata. La zona protegge normalmente le sue regioni dalla promozione a titolo, subtitle, body, section header, autore o location. Conserva nei metadata il ruolo e i limiti della decisione.

`MetadataExtractor` popola `page.source`, `page.clipping` e `page.section` da regioni metadata e candidati circoscritti. Non usa l’intera pagina come fallback generale per data o URL. Date OCR impossibili non devono interrompere il parsing.

## 5. Source, provider e location

`lookup.resolve_entity()` restituisce un `EntityMatch` con categoria, metodo, nome canonico, nome confrontato, similarity, `match_coverage` e ambiguità.

Gli stage vengono valutati in quest’ordine:

1. exact source;
2. source alias;
3. fuzzy source;
4. exact provider;
5. provider alias;
6. fuzzy provider;
7. exact location;
8. fuzzy location.

`match_coverage` misura quanta parte dell’intera regione è spiegata dal nome geografico. Per questo «Napoli» non rende automaticamente `LOCATION` la regione «CRONACHE DI NAPOLI». Le soglie fuzzy dei comuni dipendono dalla lunghezza del nome e sono configurate in `EntityLookupConfig`.

`RegionFeatureExtractor` produce un unico esito entity per regione; i classifier applicano poi vincoli relativi al **ruolo** del testo. Quando un componente non usa le entità, come `TechnicalRegionClassifier`, chiama `extract(..., include_entity=False)` per evitare quel lookup: ciò non registra un match negativo e non impedisce ai passaggi successivi di cercare source/provider/location.

Il resolver usa cache del risultato per testo normalizzato e configurazione, un indice per l’exact location e un filtro di lunghezza prima dei confronti fuzzy. Il metodo e la qualità del match accettato restano visibili in `Region.metadata["entity_lookup"]`.

## 6. Indice della rassegna

`ReviewIndexParser` legge il documento Docling raw, non soltanto i `PageRecord`: nell’esempio presente nel repository le celle dell’indice sono in `tables[].data.grid`, mentre la regione tabella normalizzata può avere `text=null`.

Il parser attuale riconosce nelle prime pagine tabelle `document_index` a cinque colonne:

1. data di pubblicazione;
2. fonte;
3. pagina originale e titolo;
4. autore;
5. pagina iniziale nella rassegna.

Produce `ReviewIndexEntry` con valori estratti, riferimento alla tabella, riga e provenance delle celle. `category` è prevista nel modello ma non viene inferita indiscriminatamente da un’intestazione generale.

`ReviewIndexMatcher` confronta le entry con i titoli proposti sulla pagina; somiglianza del titolo e metadata locali costituiscono evidenze o contraddizioni. L’indice è una **prior**, non ground truth: non sovrascrive automaticamente testo, autore o fonte dell’articolo e non crea da solo un link multipagina.

## 7. Titolo, sottotitolo e autore

### `TitleResolver`

`TitleResolver` valuta regioni candidate combinando label Docling, larghezza e posizione, dimensione font relativa al body, evidenza bold, vicinanza al body o ad altri elementi dell’header e somiglianza con i titoli delle entry indice.

Scrive punteggio e componenti in `Region.metadata["title_candidate"]`. Non impone un solo titolo per pagina. Dopo i passaggi web, `consolidate_candidates()` può applicare il vincolo di un main title ai gruppi che possiedono già una candidata identità articolo; senza clustering affidabile conserva l’incertezza.

### `SubtitleResolver`

`SubtitleResolver` cerca candidati **sopra e sotto** ciascun titolo locale. Usa distanza, overlap orizzontale, larghezza, posizione prima di un body anchor, stile rispetto al body e indizi bold/italic. Se una regione può riferirsi con punteggi simili a titoli diversi, l’assegnazione resta ambigua.

Un heading situato dopo l’inizio del body non dovrebbe diventare subtitle soltanto per vicinanza geometrica. Il risultato e l’anchor sono conservati in `Region.metadata["subtitle_resolution"]`.

### `AuthorResolver`

`AuthorResolver` gestisce byline esplicite quali `di Mario Rossi`, `da Mario Rossi`, `a cura di ...`, e firme implicite name-like quali `Michele De Feo`. Valuta token, capitalizzazione, vicinanza a titolo/subtitle, posizione rispetto al body, stile e un eventuale autore proveniente da una entry dell’indice già matched al titolo.

Conserva metodo, punteggio, componenti e prior in `Region.metadata["author_resolution"]`. L’autore dell’indice non crea una regione `AUTHOR` se non esiste un candidato nel PDF. La lista negativa dei heading editoriali è configurabile.

## 8. Section header e tipi di regione estesi

`SectionHeaderResolver` viene eseguito dopo il recupero del body e prima del raggruppamento. Cerca una regione breve fra un body precedente e uno successivo geometricamente compatibili, con indizi Docling o tipografici. Se i due body condividono un’identità articolo, può riutilizzarla; altrimenti registra un’associazione locale provvisoria senza inventare `article_id`.

Il resolver salva score, corpo precedente/successivo, tipo precedente e stato dell’associazione in `Region.metadata["section_header_resolution"]`. È più circoscritto della vecchia euristica `body_seen` valida per tutta la pagina.

I principali ruoli non-body sono:

| Tipo | Politica corrente rispetto al body |
| --- | --- |
| `ARTICLE_SECTION_HEADER` | Heading editoriale separato dal normale paragrafo body. |
| `WATERMARK`, `RIGHTS_NOTICE` | Testo tecnico escluso. |
| `ADVERTISEMENT`, `RELATED_CONTENT` | Contenuto esterno al body dell’articolo principale. |
| `PULL_QUOTE` | Citazione evidenziata; non concatenarla automaticamente, anche se appartiene all’articolo. |
| `TABLE` | Struttura tabellare conservata tramite riferimento al raw Docling; non concatenata come prosa. |
| `INFOGRAPHIC` | Media editoriale distinto; non equivale a ogni `IMAGE`. |

`PULL_QUOTE` e `INFOGRAPHIC` sono tipi disponibili e protetti, **non** categorie per le quali esista già un detector automatico completo. Le tabelle Docling sono invece mappate esplicitamente a `TABLE`.

`BodyGroupingResolver` considera heading e alcuni oggetti strutturati come possibili separatori locali quando cadono geometricamente fra due regioni body. Watermark e rights notice non sono separatori: un timbro sovrapposto non deve spezzare la lettura.

## 9. Contenuto web e ricostruzione intermedia

Per pagine classificate `WEB`, `WebMainContentResolver` distingue contenuto principale, moduli esterni e aree non risolte tramite `content_scope` e geometria. `WebArticleContinuationResolver` propone catene fra pagine adiacenti sulla base delle evidenze disponibili; `ArticleFlowResolver` valuta i confini dei link, che possono restare `candidate` o diventare `accepted`/`rejected`.

`BodyContinuationResolver` tenta di recuperare frammenti `UNKNOWN` compatibili con il body. `BodyGroupingResolver` assegna ai body gruppi, colonne e `body_reading_order` **locali alla pagina**. Il suo ordinamento non garantisce da solo la ricostruzione corretta di ogni layout multi-colonna o multipagina.

`ArticleDraftAssembler` raggruppa regioni con `metadata["article_candidate_id"]`. Costruisce il campo `body` e i `DraftSegment` **soltanto da regioni `ARTICLE_BODY`** e conserva link, charspan, bbox, provenance e warning. Una pagina o un titolo senza candidate ID/body sufficiente non produce necessariamente un draft. Section header, autore, pull quote, tabella e infographic rimangono nei `PageRecord`, ma non sono ancora segmenti ordinati del draft.

## 10. Output e debug

Per `rassegna.pdf`, la CLI scrive sotto `results/rassegna/`:

```text
results/rassegna/
├── profiling/
│   └── document.json
├── raw/
│   ├── document.json
│   └── ocr_page_NNN.json          # se la pagina è stata elaborata con OCR
├── pages/
│   └── page_NNN.json
├── flow/
│   ├── links.json
│   ├── article_drafts.json
│   ├── review_index_entries.json
│   └── review_index_matches.json
└── debug/
    └── page_NNN.png              # con --debug-bbox
```

Gli overlay mostrano bbox, tipo e alcune informazioni di scope/catena. Score dettagliati, metodi di classificazione, style, extraction method e provenance si consultano nei JSON delle pagine.

La CLI esegue inoltre `image_analysis/position_thumbnail.py` prima del salvataggio dei `PageRecord`, per arricchire le miniature tecniche della posizione dell’articolo.

## 11. Configurazione

`src/press_reputation/config.py` contiene configurazioni per:

- document profiling e OCR;
- Review Index;
- header metadata;
- title, subtitle, author e section header resolution;
- fuzzy entity lookup;
- watermark detection;
- alcune soglie generali di classificazione.

Altri componenti mantengono configurazioni locali, fra cui i resolver web e il body grouping. Non tutte le soglie sono già centralizzate.

**Residui da ripulire:** `RegionClassificationConfig` contiene ancora `watermark_opacity_threshold`, mentre il percorso watermark attivo usa `WatermarkDetectionConfig.low_opacity_threshold`. La vecchia soglia non va interpretata come configurazione effettiva del nuovo score. I parametri del watermark score devono essere verificati sul corpus prima della calibrazione fine.

## 12. Limiti e lavori successivi

- `ArticleRecord` non viene ancora finalizzato dalla CLI a partire da `ArticleDraft`.
- L’identità articolo non è uniforme: `Region.article_id` e `metadata["article_candidate_id"]` convivono; il clustering dei clipping cartacei multipagina non è ancora generale.
- Reading order e body grouping sono soprattutto locali alla pagina. Section header, tabelle, pull quote, infographic e caption non formano ancora una sequenza mista di segmenti dentro `ArticleDraft`.
- Non è ancora disponibile una fase dedicata `body_raw → body_clean` per dehyphenation, paragrafi e pulizia OCR.
- Il parser dell’indice copre il formato tabellare a cinque colonne implementato; altri layout o indici OCR possono richiedere adattamenti.
- La componente `editorial_overlap` del watermark score non è attualmente raggiungibile con `minimum_editorial_overlap=2`, perché l’overlap è una frazione fra `0` e `1`.
- La ripetizione boilerplate attuale confronta testo normalizzato identico: numeri o timestamp variabili possono impedire di riconoscere watermark ripetuti senza marker forte.
- La lista di negative evidence dell’`AuthorResolver` contiene attualmente `archivio storcio` invece di `archivio storico`: finché non viene corretto, quel caso specifico non beneficia dell’esclusione lessicale.
- La CLI di parsing non esegue sentiment analysis target-aware né calcola automaticamente il Media Reputation Score dagli `ArticleDraft`.

**Principio operativo:** conservare documento raw, profili, testo e bbox originali, provenance, score, indizi e warning. Un’associazione provvisoria o un dato assente non devono essere presentati come un articolo finale certo.