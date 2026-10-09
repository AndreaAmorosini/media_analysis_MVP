# Press Reputation

Pipeline locale per l’estrazione strutturata di articoli da PDF di rassegne stampa. Combina testo PDF estraibile e OCR selettivo, conserva raw e provenance, classifica le regioni, attribuisce identità articolo e produce bozze e record finalizzati.

**Stato attuale:** la CLI salva `PageRecord`, decisioni e link di continuazione, sequenze di reading order, `ArticleDraft` e `ArticleRecord`. Il record finale può avere stato `ready` oppure `review_required`. La CLI non esegue ancora sentiment analysis o Media Reputation Score sugli articoli estratti.

## Esecuzione

Il progetto usa Pixi e Python 3.12.

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf"
```

Per generare gli overlay delle bounding box:

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli "percorso/rassegna.pdf" --debug-bbox
```

L’opzione `--output-dir` cambia la directory dei risultati; il valore predefinito è `results/`.

## Flusso

```text
PDF
  │
  ├─ DocumentProfiler
  ├─ DoclingParser: estrazione senza OCR + OCR selettivo
  ├─ PageNormalizer e merge_extraction
  ├─ ReviewIndexParser dal raw Docling
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
  ├─ ArticleTextNormalizer
  ├─ ArticleFinalizer
  ├─ arricchimento delle miniature di posizione
  └─ JSON e overlay opzionali
```

`src/press_reputation/cli.py` orchestra estrazione, assemblaggio, normalizzazione, finalizzazione e salvataggio. `src/press_reputation/pipeline.py` definisce l’ordine di classificazione e ricostruzione delle pagine.

## 1. Profiling, OCR e provenance

`DocumentProfiler` usa PyMuPDF per misurare testo estraibile e copertura delle immagini. `DocumentProfilingConfig` determina su quali pagine richiedere OCR: testo breve, da solo, non è motivo sufficiente.

`DoclingParser` converte il PDF con OCR disattivato e converte separatamente una rappresentazione image-only delle sole pagine selezionate. `PageNormalizer` conserva bbox e riferimenti raw; `merge_extraction` confronta bbox e testo prima di fondere le regioni.

| `extraction_profile.kind` | Significato |
| --- | --- |
| `native_pdf` | Testo utilizzato dal livello testuale PDF. |
| `ocr` | Testo utilizzato dal solo passaggio OCR. |
| `mixed` | Testo utilizzato da entrambi. |
| `image_only` | Immagini presenti, nessun testo utilizzabile. |
| `null` | Nessun testo o immagine utile rilevato. |

`Region.extraction_method` distingue `pdf_text`, `ocr` e `unknown`. La provenance conserva, quando disponibili, pagina, bbox raw, `self_ref`, `charspan`, passaggio di estrazione e mappatura fra pagina OCR temporanea e pagina PDF originale.

`pdf_text` descrive il canale letto dalla pipeline; non dimostra che il PDF fosse nato digitale anziché contenere un precedente livello OCR.

## 2. Modelli e dati intermedi

In `models/page.py`:

- `PageRecord` rappresenta pagina, fonte, clipping, profilo di estrazione e regioni;
- `Region` conserva tipo, testo, bbox, label Docling, stile, extraction method, provenance, metadata di classificazione e `article_id` opzionale;
- `PageType` descrive la funzione editoriale della pagina ed è indipendente dal profilo OCR.

Le bbox usano `[x0, y0, x1, y1]` in punti PDF, con origine in alto a sinistra.

Le tabelle Docling sono mappate a `RegionType.TABLE`; `table_shape` e `table_ref` permettono di risalire alle celle nel raw senza trasformarle in un paragrafo body. Il documento Docling originale e gli eventuali passaggi OCR rimangono salvati.

`Region.provenance` documenta l’origine del contenuto; `Region.metadata["article_clustering"]` documenta perché una regione è stata attribuita a un articolo. Entrambi sono conservati.

## 3. Stile, boilerplate e classificazione tecnica

`PdfStyleEnricher` opera prima della classificazione locale. Quando esistono span PDF associabili, arricchisce `Region.style` con font, dimensioni, bold/italic, colore, opacità e qualità dell’evidenza. Font embedded con nomi non informativi non autorizzano a dedurre automaticamente uno stile normale. Gli span del PDF originale non sono attribuiti alle regioni OCR.

`DocumentBoilerplateDetector` annota pattern ricorrenti, posizione relativa e frequenza tra pagine. La ripetizione non elimina automaticamente il testo originale: ruolo ed esclusione sono decisi dai classificatori successivi.

`TechnicalRegionClassifier` riconosce miniature tecniche, `RIGHTS_NOTICE`, `WATERMARK` e `ADVERTISEMENT`. I notice conservano testo, bbox e provenance pur rimanendo fuori dal body. Il watermark usa uno score con marker, opacity, colore, ripetizione, posizione e altre evidenze; la sola bassa opacità non è sufficiente.

`TABLE`, `PULL_QUOTE` e `INFOGRAPHIC`, se già riconosciuti, sono protetti dalle euristiche tecniche. Non tutte le immagini sono infographic e non ogni frase fra virgolette è un pull quote.

## 4. Header metadata ed entity lookup

`MetadataSeedClassifier` identifica `SOURCE_NAME`, `PRESS_REVIEW_PROVIDER`, `PUBLICATION_DATE`, `ORIGINAL_PAGE`, `CLIPPING_SHEET` e `HEADER_METADATA`. `HeaderMetadataZoneDetector` delimita la fascia tecnica superiore prima della classificazione editoriale.

`MetadataExtractor` popola source, clipping, sezione e URL da regioni circoscritte. Non usa tutto il body come fallback indiscriminato per i metadata.

`lookup.resolve_entity()` valuta nell’ordine:

1. exact source;
2. source alias;
3. fuzzy source;
4. exact provider;
5. provider alias;
6. fuzzy provider;
7. exact location;
8. fuzzy location.

Il risultato conserva metodo, similarity, ambiguità e `match_coverage`. La coverage impedisce che la sola presenza di «Napoli» renda `LOCATION` l’intera regione «CRONACHE DI NAPOLI». Le soglie sono configurabili.

I componenti che non usano le entità, come il classificatore tecnico, possono chiamare `RegionFeatureExtractor.extract(..., include_entity=False)` evitando lookup superflui. Ciò non registra un match negativo per i passaggi successivi.

## 5. Review Index

`ReviewIndexParser` legge le tabelle `document_index` nel raw Docling. Il formato attualmente riconosciuto ha cinque colonne: data, fonte, pagina originale e titolo, autore, pagina iniziale della rassegna.

Produce `ReviewIndexEntry` con provenance delle celle. `ReviewIndexMatcher` usa titolo e metadata locali per associare le entry a titoli dell’articolo. L’indice è una **prior**, non ground truth: non sovrascrive automaticamente il contenuto del PDF. La categoria è prevista nel modello ma non viene inferita da heading generali quando l’associazione alla riga non è verificabile.

## 6. Titolo, sottotitolo, autore e section header

`TitleResolver` valuta label Docling, stile relativo al body, bold, geometria, posizione e somiglianza con l’indice. Non impone un titolo unico per pagina: l’unicità del main title può essere applicata per identità articolo disponibile.

`SubtitleResolver` cerca regioni sopra o sotto il titolo, usando prossimità, overlap, stile e posizione rispetto all’inizio del body. Conserva le assegnazioni ambigue.

`AuthorResolver` valuta byline esplicite e nomi impliciti tramite forma del nome, posizione, stile e autore eventualmente indicato da un’entry indice associata al titolo. Il testo dell’indice non crea da solo una regione autore.

`SectionHeaderResolver` cerca heading interni fra body compatibili prima e dopo; conserva score, regioni di riferimento e stato confermato o provvisorio dell’associazione.

## 7. Body seed, clustering e continuazione

La classificazione distingue:

- `ARTICLE_BODY` con `body_role="seed"`: blocco sufficientemente affidabile;
- `UNKNOWN`: blocco non ancora attribuito, anche se breve;
- `ARTICLE_BODY` con `body_role="continuation"`: frammento recuperato tramite continuità.

La soglia indicativa di 18 parole serve ai seed, non vieta che un blocco corto diventi body. `BodyContinuationResolver` richiede un riferimento con `article_id`, confronta colonna, prossimità in avanti, overlap, larghezza e stile, e aggiunge immediatamente ogni frammento promosso ai riferimenti. Continua fino a convergenza, senza fondere le bbox.

`ArticleClusteringResolver` assegna `article_id` quando dispone di evidenza sufficiente da titolo, body, link e geometria. Mantiene score, metodo e alternative nei metadata. In caso ambiguo l’ID può restare assente.

## 8. Continuazione cartacea multipagina

`NewspaperContinuationResolver` confronta pagine PDF adiacenti usando foglio corrente/totale, source, data, pagina originale, titolo e Review Index. `1/2 → 2/2` è una prova forte in assenza di contraddizioni.

Un secondo foglio può contenere body, caption o solo immagini: il resolver non richiede che ripeta il titolo. Un link accettato può attribuire l’`article_id` alle regioni editoriali del foglio successivo senza trasformare le immagini in body.

Link e decisioni, incluse le contraddizioni, restano nei JSON di flow. Una pagina di sole immagini **senza** marker o altre evidenze non viene attribuita automaticamente per mera adiacenza.

## 9. Web main content, intrusion e continuazione multipagina

`WebMainContentResolver` distingue gli scope `main`, `related`, `advertisement`, `navigation`, `non_main` e `unknown`. I moduli related sono delimitati localmente: un elemento correlato non dovrebbe trasformare in related tutto il body sottostante.

`InlineIntrusionDetector` cerca contenuti estranei fra due body della stessa area web. Combina marker, formattazione da link, lunghezza, differenza di stile, gap e discontinuità lessicale. Un’intrusione accettata viene esclusa dal body come `RELATED_CONTENT` o `ADVERTISEMENT`; il body prima e dopo può continuare nello stesso articolo.

`WebArticleContinuationResolver` propone collegamenti fra pagine adiacenti tramite uno score di evidenze:

- stesso URL, fonte e data quando disponibili;
- colonna compatibile;
- body che inizia presto nella pagina;
- continuità sintattica o lessicale;
- eventuale stesso autore.

Metadata **mancanti** non diventano automaticamente contraddizioni. URL e fonti presenti ma differenti, o un nuovo titolo, possono impedire il collegamento. Le proposte, gli score e le contraddizioni sono salvati separatamente.

`ArticleFlowResolver` dovrebbe combinare lo score con l’eventuale continuità forte della frase per produrre link `candidate` o `accepted`. **Nel codice corrente è presente una correzione aperta nel percorso finale del resolver: i link web valutati non vengono aggiunti alla lista restituita.** I risultati web non vanno quindi interpretati come linking confermato finché non viene ripristinata l’aggiunta a `links`.

Inoltre l’identità web candidata può essere attribuita prima che il link sia accettato. Un draft multipagina con collegamento non confermato resta un candidato, non un articolo certificato.

## 10. Reading order e ArticleDraft

`BodyGroupingResolver` produce gruppi e colonne locali alla pagina. `ArticleReadingOrderResolver` lavora per `(document_id, article_id)`: ordina colonne e regioni, inserisce gli `ARTICLE_SECTION_HEADER`, tratta regioni spanning come separatori di bande e produce `ReadingOrderSegment` con tipo, pagina, colonna, ordine, bbox, testo, confidence e provenance.

`ArticleDraftAssembler` costruisce `ArticleDraft.body`, `DraftSegment` e charspan dai segmenti ordinati. Il valore `body` del draft è ancora il **raw assemblato**. Gli section header possono comparire come segmenti tipizzati nella sequenza testuale. Media e caption non fanno ancora parte del reading order multimodale del body.

I link non risolti e le anomalie di ordinamento producono warning. Il draft conserva regioni candidate ed è disponibile come output intermedio di debugging.

## 11. Normalizzazione del testo

`ArticleTextNormalizer` viene eseguito **dopo** `ArticleDraftAssembler`. Non modifica i segmenti originali né i loro `article_charspan`.

| Campo `ArticleDraft` | Significato |
| --- | --- |
| `body` | Testo raw assemblato, mantenuto per compatibilità. |
| `body_raw` | Copia esplicita del raw; i charspan del draft si riferiscono qui. |
| `body_clean` | Testo derivato dai segmenti ordinati dopo normalizzazione. |
| `text_normalization` | Metodo, dimensioni e azioni diagnostiche. |

La normalizzazione gestisce line break, confini di paragrafo, spazi duplicati, alcuni problemi Unicode/OCR, quote, punteggiatura e drop caps quando bbox e stile sono sufficienti.

La dehyphenation è conservativa: un soft hyphen o una parola ricomposta attestata possono perdere il trattino; negli altri casi si può rimuovere il line break **conservando il trattino**. Non viene applicato un `replace("-\n", "")` indiscriminato. URL, email, decimali e alcuni acronimi sono protetti dalle regole di spaziatura.

`body_clean` non ha attualmente una mappatura carattere-per-carattere al PDF. Le azioni registrano il `region_id` e il charspan nel **body raw**.

## 12. Finalizzazione in ArticleRecord

`ArticleFinalizer` viene eseguito dopo la normalizzazione:

```text
ArticleDraft + PageRecord + ReviewIndex
→ ArticleFinalizer
→ ArticleRecord
```

Il finalizer usa il draft per identità, body, link e ordine; regioni attribuite allo stesso articolo per subtitle, autori, location, section header, media e provenance. Metadata di pagina vengono usati quando non appaiono in conflitto con più articoli. Un Review Index matched può fornire un fallback, registrato in `field_origins` come prior.

`ArticleRecord`, in `models/article.py`, contiene:

- source, publication date, title, subtitle, authors e locations;
- original pages, PDF pages e URL;
- `body_raw` e `body` normalizzato;
- section header e media;
- `article_type` (`newspaper`, `web`, `unknown`);
- provenance delle regioni e dei prior dell’indice;
- `extraction_confidence` e `reconstruction_confidence`;
- stato `ready` oppure `review_required`, warning e origine dei campi.

I charspan in `ArticleRecord.provenance` si riferiscono al **body raw**, non al body pulito.

`reconstruction_confidence` è una proxy euristica basata su reading order e clustering, con penalità per link candidati; non è una probabilità calibrata. `extraction_confidence` rimane `null` se non sono disponibili score numerici misurati per le regioni. Non viene attribuita una confidence inventata in base alla sola distinzione OCR/PDF.

Un record incerto viene comunque salvato come `review_required`. Lo stato `ready` richiede titolo, body non vuoto, metadata richiesti dalla configurazione, confidence di ricostruzione sufficiente e assenza di link ancora candidati o contraddizioni rilevate.

**Contratto per il futuro NLP:** consumare `ArticleRecord`, usando `body` come testo normalizzato e mantenendo separati title, subtitle e section header. Non ricostruire l’articolo direttamente da `Region[]`. Applicare il downstream automatico soltanto dopo aver controllato `finalization_status`.

## 13. Tipi di regione non-body

| Tipo | Trattamento |
| --- | --- |
| `WATERMARK`, `RIGHTS_NOTICE` | Testo tecnico escluso dal body; raw e provenance conservati. |
| `ADVERTISEMENT`, `RELATED_CONTENT`, `NAVIGATION` | Contenuti esterni al body principale. |
| `PULL_QUOTE` | Citazione evidenziata, non concatenata automaticamente. |
| `TABLE` | Celle consultabili tramite il raw Docling; non convertita in prosa body. |
| `INFOGRAPHIC` | Media editoriale, distinto da una generica `IMAGE`. |
| `CAPTION` | Può essere associata a media, ma non è un paragrafo body. |

`PULL_QUOTE` e `INFOGRAPHIC` sono tipi disponibili e protetti, non categorie con detector automatici completi.

## 14. Output

Per `rassegna.pdf`, la CLI scrive sotto `results/rassegna/`:

```text
results/rassegna/
├── profiling/
│   └── document.json
├── raw/
│   ├── document.json
│   └── ocr_page_NNN.json                 # solo se elaborata con OCR
├── pages/
│   └── page_NNN.json
├── flow/
│   ├── links.json
│   ├── reading_order.json
│   ├── article_drafts.json
│   ├── article_records.json
│   ├── newspaper_continuation_decisions.json
│   ├── web_continuation_decisions.json
│   ├── review_index_entries.json
│   └── review_index_matches.json
└── debug/
    └── page_NNN.png                     # con --debug-bbox
```

La CLI arricchisce anche le miniature della posizione dell’articolo prima di salvare i `PageRecord`. Score, ID articolo, scope, provenance, alternative e warning restano ispezionabili nei JSON.

## 15. Configurazione

`src/press_reputation/config.py` contiene configurazioni per profiling/OCR, Review Index, metadata header, classificazione, entity lookup, watermark, clustering, body continuation, reading order, continuazione cartacea, normalizzazione testuale e finalizzazione.

`classification/web_content_config.py` configura contenuto web e intrusion inline. `WebContinuationConfig` e `ArticleFlowConfig` configurano proposta e accettazione dei link web. Alcuni componenti mantengono proprie soglie locali. I valori non sono ancora calibrati quantitativamente sull’intero corpus.

## 16. Limiti e correzioni aperte

- **Link web:** nel percorso finale di `ArticleFlowResolver.resolve()` manca l’aggiunta del `FlowLink` valutato a `links`. È una correzione necessaria prima di considerare operativo il web cross-page linking e di affidarsi al relativo stato di finalizzazione.
- **Metadata su pagine multi-articolo:** `ArticleFinalizer.unambiguous_pages()` considera i titoli con ID; un ulteriore titolo senza `article_id` sulla stessa pagina può rendere ambigui i metadata di pagina senza essere rilevato da quel controllo.
- **Location:** vengono inserite nel record soltanto quelle attribuite all’articolo. Una location non clusterizzata non deve essere copiata indiscriminatamente da tutta la pagina.
- **Extraction confidence:** senza confidence OCR/native misurata può restare `null`; la reconstruction confidence è euristica e richiede valutazione sul corpus.
- **OCR e testo:** la pulizia Unicode/OCR è selettiva. Un lessico di dehyphenation vuoto lascia irrisolte alcune parole spezzate per evitare di distruggere composti autentici.
- **Reading order:** non riordina il testo all’interno di bbox multi-colonna indivise e non produce ancora una sequenza multimodale completa con media, tabelle e caption.
- **Web intrusion:** il detector richiede body prima e dopo sulla stessa pagina/area; i brevi `UNKNOWN` web con scope non ancora `main` possono rimanere fuori dal body.
- **Watermark:** `WatermarkDetectionConfig.minimum_editorial_overlap` vale attualmente `2`, mentre l’overlap varia tra `0` e `1`; quel componente dello score non si attiva.
- **Autore:** la lista negativa contiene attualmente `archivio storcio` anziché `archivio storico`.
- La CLI non esegue ancora sentiment analysis target-aware né calcola automaticamente un Media Reputation Score dagli `ArticleRecord`.

**Principio operativo:** conservare raw Docling, profili, bbox e testo originali, provenance, score, indizi e warning. Un ArticleRecord `review_required` rimane disponibile per analisi e debugging, ma non deve essere trattato come un articolo verificato.