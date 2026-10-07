# Press Reputation

Pipeline locale per l’estrazione strutturata di articoli da PDF di rassegne stampa. Il sistema combina testo PDF estraibile e OCR selettivo, conserva raw e provenance, classifica le regioni e produce bozze di articolo quando dispone di evidenze sufficienti.

**Stato attuale:** la CLI produce `PageRecord`, link di continuazione e `ArticleDraft` candidati. `ArticleRecord` è definito nei modelli, ma non è ancora generato dalla pipeline CLI. Le bozze non costituiscono input approvati automaticamente per sentiment analysis o reputation scoring.

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf"
```

Per generare anche gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf" --debug-bbox
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
  │    ArticleClusteringResolver: attribuzione locale
  │    TitleResolver.consolidate_candidates
  │    ArticleFlowResolver
  │    ArticleClusteringResolver: link accepted
  │
  │    per ogni pagina:
  │      BodyContinuationResolver
  │      ArticleClusteringResolver: body recuperati
  │      SectionHeaderResolver
  │      ArticleClusteringResolver: heading e media
  │      BodyGroupingResolver
  │
  ├─ ArticleDraftAssembler
  ├─ analisi delle miniature di posizione dell'articolo
  └─ JSON e overlay opzionali
```

`src/press_reputation/cli.py` orchestra estrazione, salvataggio e assemblaggio dei draft. `src/press_reputation/pipeline.py` definisce l’ordine delle classificazioni e della ricostruzione intermedia.

## 1. Document profiling e OCR selettivo

`profiling/document_profiler.py` usa PyMuPDF per misurare, su ogni pagina:

- parole leggibili dal livello testuale del PDF;
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

Le bbox interne usano `[x0, y0, x1, y1]`, origine in alto a sinistra e coordinate in punti PDF. Quando possibile, il normalizer conserva `self_ref`, label Docling, `charspan`, bbox raw e pagina in `Region.provenance`. Gli output Docling raw restano disponibili separatamente.

Il `PageNormalizer` riconosce la collection Docling `tables` come `RegionType.TABLE`. Per ogni tabella conserva `metadata["table_shape"]` e `metadata["table_ref"]`; le celle strutturate sono consultabili nel documento raw tramite il riferimento alla tabella.

**Distinzione importante:** la provenance descrive *da dove proviene la regione*. `metadata["article_clustering"]` descrive invece *perché è stata attribuita a un articolo*. Nessuno dei due dati deve sostituire l’altro.

## 3. Stile PDF e boilerplate documentale

`PdfStyleEnricher` precede le classificazioni locali. Legge gli span testuali del PDF con PyMuPDF e, quando riesce ad associarli geometricamente a una regione, scrive in `Region.style`:

- `font_names` e `dominant_font`;
- `median_font_size` e `max_font_size`;
- `bold_ratio` e `italic_ratio`;
- `dominant_color` e `median_opacity`;
- origine e numero degli span associati;
- quota di evidenza interpretabile per bold e italic.

Uno stile non inferibile rimane sconosciuto: un nome di font embedded non informativo non dimostra che il testo sia normale. L’enricher non attribuisce alle regioni OCR gli span del PDF originale.

### Fingerprint boilerplate

`DocumentBoilerplateDetector` raggruppa le regioni per documento usando un fingerprint composto da:

- testo normalizzato o pattern testuale;
- bbox relativa alla pagina;
- font dominante e dimensione mediana, quando disponibili.

Sono previste famiglie per `Data Stampa`, `WEB`, `STAMPA LOCALE`/`STAMPA NAZIONALE` e candidati rights notice quali `ARTICOLO NON CEDIBILE` e `USO ESCLUSIVO`. Per alcune famiglie tecniche i numeri variabili vengono normalizzati; per il testo generico resta il confronto testuale esatto.

La frequenza è calcolata sulle **pagine PDF distinte dello stesso documento**. Il detector registra il fingerprint e le evidenze in `Region.metadata["boilerplate_detection"]`; quando il gruppo supera le soglie imposta `boilerplate` e `boilerplate_frequency`.

**Ripetizione non significa esclusione automatica:** il detector non imposta `exclude_from_article_text`. I classifier successivi devono stabilire se la regione sia watermark, rights notice, metadata, contenuto web esterno o testo editoriale. Il testo raw resta invariato. Su un documento di una sola pagina non viene dedotta una ripetizione fra pagine.

## 4. Classificazione tecnica, rights notice e watermark

### Classificazione tecnica

`TechnicalRegionClassifier` riconosce `RIGHTS_NOTICE`, `WATERMARK`, `ADVERTISEMENT` e miniature tecniche della posizione dell’articolo. Le miniature vengono distinte dalle immagini editoriali e possono causare l’esclusione delle regioni contenute al loro interno.

I tipi strutturati `TABLE`, `INFOGRAPHIC` e `PULL_QUOTE`, quando già assegnati, sono protetti dalle euristiche tecniche testuali. Una normale `IMAGE` non viene trasformata automaticamente in infographic.

### Rights notice

Il classifier tecnico esamina formule quali:

- `© RIPRODUZIONE RISERVATA`;
- `TUTTI I DIRITTI RISERVATI`;
- `ARTICOLO NON CEDIBILE`;
- `USO ESCLUSIVO`.

`classify_rights_notice()` normalizza il testo **solo per il confronto** e distingue notice autonomi accettati da blocchi ambigui che contengono anche altro testo. Per un notice accettato assegna `RegionType.RIGHTS_NOTICE` e `exclude_from_article_text=True`, conservando la regione, il testo originale, la bbox e la provenance nei `PageRecord`.

Il risultato è annotato in `Region.metadata["rights_notice_detection"]`. Un blocco misto ambiguo non deve far escludere indiscriminatamente l’intero paragrafo. La classificazione rights notice ha precedenza sul watermark score.

### Watermark scoring

Il percorso attivo per i watermark usa `watermark_score()`, non la sola opacità. Combina:

- marker forte, in particolare una regione breve che inizia con `Data Stampa`;
- marker più deboli;
- opacità e colore;
- frequenza fra pagine dal boilerplate detector;
- posizione ai margini e geometria verticale;
- overlap con testo potenzialmente editoriale;
- penalità per blocchi lunghi e label Docling da heading.

Per assegnare `WATERMARK` devono essere soddisfatti **sia** la soglia di score **sia** un gate di supporto non puramente visivo. Un testo chiaro non diventa quindi watermark soltanto perché ha bassa opacità.

Score, componenti, soglia ed esito sono registrati in `Region.metadata["watermark_detection"]` quando vi sono indizi valutati.

**Limite implementativo:** in `WatermarkDetectionConfig`, `minimum_editorial_overlap` è attualmente `int = 2`, mentre `bbox_overlap_fraction()` restituisce valori tra `0` e `1`. Il componente `editorial_overlap` non può quindi attivarsi con tale configurazione. Va corretto in una frazione configurabile prima di considerarlo un segnale operativo. In `TechnicalRegionClassifier` rimane inoltre il vecchio `looks_like_watermark()`, non chiamato dal percorso attivo.

## 5. Metadata, source, provider e location

`MetadataSeedClassifier` riconosce `SOURCE_NAME`, `PRESS_REVIEW_PROVIDER`, `PUBLICATION_DATE`, `ORIGINAL_PAGE`, `CLIPPING_SHEET` e `HEADER_METADATA`.

`HeaderMetadataZoneDetector` usa i seed nella parte alta della pagina per delimitare la fascia dei metadata. La zona protegge normalmente le sue regioni dalla promozione a titolo, subtitle, body, section header, autore o location. Conserva nei metadata il ruolo e i limiti della decisione.

`MetadataExtractor` popola `page.source`, `page.clipping` e `page.section` da regioni metadata e candidati circoscritti. Non usa l’intera pagina come fallback generale per data o URL.

`lookup.resolve_entity()` restituisce un `EntityMatch` con categoria, metodo, nome canonico, nome confrontato, similarity, `match_coverage` e ambiguità. Gli stage vengono valutati nell’ordine:

1. exact source;
2. source alias;
3. fuzzy source;
4. exact provider;
5. provider alias;
6. fuzzy provider;
7. exact location;
8. fuzzy location.

`match_coverage` misura quanta parte dell’intera regione è spiegata dal nome geografico. «Napoli» non rende automaticamente `LOCATION` la regione «CRONACHE DI NAPOLI». Le soglie fuzzy dei comuni dipendono dalla lunghezza del nome e sono configurate in `EntityLookupConfig`.

`RegionFeatureExtractor` produce un unico esito entity per regione; i classifier applicano poi vincoli relativi al **ruolo** del testo. Componenti che non usano le entità, come `TechnicalRegionClassifier`, chiamano `extract(..., include_entity=False)` per evitare un lookup superfluo: questo non registra un match negativo.

## 6. Review Index

`ReviewIndexParser` legge il documento Docling raw, non soltanto i `PageRecord`: nell’esempio presente nel repository le celle dell’indice sono in `tables[].data.grid`, mentre la regione tabella normalizzata può avere `text=null`.

Il parser attuale riconosce nelle prime pagine tabelle `document_index` a cinque colonne:

1. data di pubblicazione;
2. fonte;
3. pagina originale e titolo;
4. autore;
5. pagina iniziale nella rassegna.

Produce `ReviewIndexEntry` con valori estratti, riferimento alla tabella, riga e provenance delle celle. `category` è prevista nel modello ma non viene inferita indiscriminatamente da un’intestazione generale.

`ReviewIndexMatcher` confronta le entry con i titoli proposti sulla pagina; somiglianza del titolo e metadata locali costituiscono evidenze o contraddizioni. L’indice è una **prior**, non ground truth: non sovrascrive automaticamente testo, autore o fonte dell’articolo e non crea da solo un link multipagina.

## 7. Titolo, sottotitolo, autore e section header

### Titolo

`TitleResolver` valuta regioni candidate combinando label Docling, larghezza e posizione, dimensione font relativa al body, evidenza bold, vicinanza al body o ad altri elementi dell’header e somiglianza con i titoli delle entry indice.

Scrive score e componenti in `Region.metadata["title_candidate"]`. Non impone un solo titolo per pagina. Dopo il clustering locale, `consolidate_candidates()` applica il vincolo ai gruppi che possiedono un’identità articolo; negli altri casi conserva l’incertezza.

### Sottotitolo

`SubtitleResolver` cerca candidati **sopra e sotto** ciascun titolo locale. Usa distanza, overlap orizzontale, larghezza, posizione prima di un body anchor, stile rispetto al body e indizi bold/italic. Se una regione può riferirsi con punteggi simili a titoli diversi, l’assegnazione resta ambigua.

Il risultato e il riferimento al titolo sono conservati in `Region.metadata["subtitle_resolution"]`.

### Autore

`AuthorResolver` gestisce byline esplicite e firme implicite name-like. Valuta token, capitalizzazione, vicinanza a titolo/subtitle, posizione rispetto al body, stile e un eventuale autore proveniente da una entry dell’indice già matched al titolo.

Conserva metodo, score, componenti e prior in `Region.metadata["author_resolution"]`. L’autore dell’indice non crea una regione `AUTHOR` se non esiste un candidato nel PDF. La lista negativa dei heading editoriali è configurabile.

### Section header

`SectionHeaderResolver` viene eseguito **dopo** il recupero del body e prima del raggruppamento. Cerca una regione breve fra un body precedente e uno successivo geometricamente compatibili, con indizi Docling o tipografici. Usa l’identità articolo dei body quando disponibile; altrimenti registra un’associazione locale provvisoria.

Conserva score, ID dei body adiacenti, tipo precedente e stato dell’associazione in `Region.metadata["section_header_resolution"]`.

## 8. Article clustering

`ArticleClusteringResolver` attribuisce `Region.article_id` senza cambiare `RegionType`, testo o provenance. L’identità canonica deriva da un anchor titolo con `region_id` stabile oppure, per le catene web già identificate, riusa il relativo `article_candidate_id`.

Il passaggio `assign_local()`:

1. crea anchor separati per i titoli ammissibili, permettendo **più articoli sulla stessa pagina**;
2. assegna subtitle e author quando i rispettivi resolver forniscono un riferimento al titolo/header;
3. conserva le identità già proposte dal resolver di continuazione web per i body `main`;
4. valuta i body locali rispetto ai cluster concorrenti usando overlap, distanza, colonna, stile, posizione rispetto al titolo, barriera di titoli concorrenti e match con l’indice;
5. attribuisce `article_id` soltanto se score e margine rispetto al secondo cluster sono sufficienti.

Le assegnazioni e le alternative sono registrate in `Region.metadata["article_clustering"]`. I casi senza evidenza o con titoli concorrenti restano irrisolti: `article_id=None` è preferibile a un’associazione sbagliata.

Dopo `ArticleFlowResolver`, `link_accepted_flows()` usa soltanto link `accepted` con regioni di confine individuabili. ID contraddittori producono una contraddizione anziché essere fusi silenziosamente. Il componente attuale di flow è soprattutto orientato alle catene web: questo passaggio **non introduce ancora il linking automatico dei clipping cartacei multipagina**.

Dopo il recupero body, `assign_recovered_body()` annota i body rimasti senza ID. `assign_section_headers_and_media()` può attribuire:

- section header quando i body precedente e successivo condividono `article_id`;
- immagini, infographic, tabelle e pull quote vicini a body già attribuiti, se un cluster prevale;
- caption vicine a media già attribuiti.

Il clustering attuale **non assegna ancora sistematicamente `LOCATION`** né risolve le pagine composte soltanto da immagini prive di un collegamento multipagina affidabile. Source, data e URL di `PageRecord` sono dati di pagina: su una pagina multi-articolo non costituiscono automaticamente metadata di ogni cluster.

## 9. Body continuation e reading order

`BodyContinuationResolver` considera soltanto body già dotati di `article_id`. Per ogni `UNKNOWN` calcola il miglior punteggio **per articolo**, richiede soglia e margine rispetto ai concorrenti e, quando lo promuove a `ARTICLE_BODY`, gli assegna lo stesso ID. Il frammento recuperato diventa un nuovo riferimento, permettendo un’espansione iterativa.

Le alternative non sufficientemente distinte restano in `metadata["body_continuation_candidates"]`. Il margine è attualmente un valore interno (`0.12`): andrebbe centralizzato prima della calibrazione sul corpus.

`BodyGroupingResolver` considera solo body con `article_id` e non unisce nella stessa colonna regioni con ID differenti. Assegna `body_group_id`, `body_column_id` e `body_reading_order` **locali alla pagina**. Heading e alcuni oggetti strutturati possono fungere da separatori geometrici; watermark e rights notice non devono spezzare il flusso di lettura.

Questo ordinamento non garantisce ancora la ricostruzione generale di segmenti cross-column o di una sequenza unica multipagina.

## 10. Tipi strutturati e ArticleDraft

| Tipo | Politica rispetto al body |
| --- | --- |
| `ARTICLE_SECTION_HEADER` | Heading interno: appartiene potenzialmente all’articolo, ma non è un paragrafo body. |
| `WATERMARK`, `RIGHTS_NOTICE` | Testo tecnico escluso dal body; regione originale conservata. |
| `ADVERTISEMENT`, `RELATED_CONTENT` | Fuori dal body dell’articolo principale. |
| `PULL_QUOTE` | Citazione evidenziata: non concatenata automaticamente per evitare duplicazioni. |
| `TABLE` | Struttura riferita al raw Docling; non concatenata come prosa. |
| `INFOGRAPHIC` | Media editoriale distinto; non equivale a ogni `IMAGE`. |

`PULL_QUOTE` e `INFOGRAPHIC` sono tipi disponibili e protetti, non categorie con detector automatici completi. Le tabelle Docling sono invece mappate esplicitamente a `TABLE`.

`ArticleDraftAssembler` raggruppa le regioni usando `Region.article_id`, con fallback compatibile a `metadata["article_candidate_id"]` quando l’ID canonico manca. Rifiuta regioni in cui i due ID sono discordanti. Costruisce `body` e `DraftSegment` **soltanto da regioni `ARTICLE_BODY`** ammesse e ordinate; conserva bbox, charspan, provenance, link e warning.

Un articolo senza body assegnato non produce un draft. Section header, author e media attribuiti restano nei `PageRecord`, ma non sono ancora segmenti tipizzati e ordinati all’interno di `ArticleDraft`.

## 11. Output e debug

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

I JSON delle pagine sono la fonte principale per score, provenance, stato del clustering e alternative scartate. Gli overlay mostrano bbox e tipo, con annotazioni su `article_id`, clustering, scope e catena web quando disponibili.

Il colore della bbox viene selezionato tramite il **tipo originale** (`region.type.value`), non tramite l’etichetta estesa che include l’ID articolo. I tipi non presenti in `REGION_COLORS` usano il colore di fallback.

**Limite attuale dell’etichetta overlay:** nel ramo che aggiunge `content_scope`, il codice ricostruisce `label` a partire da `original_label`; ciò può nascondere nel testo dell’etichetta `article_id` e score già aggiunti. Non altera il colore né i dati JSON.

La CLI esegue anche `image_analysis/position_thumbnail.py` prima del salvataggio dei `PageRecord`, per arricchire le miniature tecniche della posizione dell’articolo.

## 12. Configurazione

`src/press_reputation/config.py` contiene configurazioni per:

- document profiling e OCR;
- Review Index;
- header metadata;
- title, subtitle, author e section header resolution;
- fuzzy entity lookup;
- watermark detection;
- fingerprint boilerplate documentale;
- article clustering;
- alcune soglie generali di classificazione.

Altri componenti mantengono configurazioni locali, fra cui i resolver web, `BodyGroupingResolver` e parte del recupero body. Non tutte le soglie sono già centralizzate o calibrate sul corpus.

## 13. Limiti e lavori successivi

- `ArticleRecord` non viene ancora finalizzato dalla CLI a partire da `ArticleDraft`.
- Il clustering è locale alla pagina, con riuso delle catene web e dei link accettati disponibili. Non collega ancora autonomamente i clipping `foglio 1/2 → foglio 2/2`.
- `Region.article_id` è l’identità canonica del nuovo clustering; `metadata["article_candidate_id"]` resta un dato intermedio del percorso web. Non tutte le regioni pertinenti ricevono necessariamente un ID: i casi ambigui restano visibili.
- L’attribuzione di `LOCATION`, URL e metadata per singolo articolo su pagine multi-articolo non è ancora completa.
- Reading order e body grouping sono principalmente locali alla pagina. Section header, tabelle, pull quote, infographic e caption non formano ancora una sequenza mista di segmenti in `ArticleDraft`.
- Non esiste ancora una fase dedicata `body_raw → body_clean` per dehyphenation, paragrafi e pulizia OCR.
- Il parser dell’indice copre il formato tabellare a cinque colonne implementato; altri layout o indici OCR possono richiedere adattamenti.
- Il fingerprint boilerplate normalizza numeri variabili soltanto per alcune famiglie note. La ripetizione non dimostra che una regione debba essere esclusa dall’articolo.
- `WatermarkDetectionConfig.minimum_editorial_overlap` è attualmente `2`, mentre l’overlap calcolato è fra `0` e `1`: questa componente dello score richiede una correzione della configurazione.
- La lista negativa dell’`AuthorResolver` contiene attualmente `archivio storcio` anziché `archivio storico`; quel caso non beneficia dell’esclusione lessicale finché non viene corretto.
- La CLI di parsing non esegue sentiment analysis target-aware né calcola automaticamente il Media Reputation Score dagli `ArticleDraft`.

**Principio operativo:** conservare raw Docling, profili, testo e bbox originali, provenance, score, indizi, alternative e warning. Un’assegnazione provvisoria o un dato assente non devono essere presentati come un articolo finale certo.