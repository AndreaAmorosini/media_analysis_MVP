# Press Reputation

Pipeline locale per l’estrazione strutturata di articoli da PDF di rassegne stampa. Combina testo PDF estraibile e OCR selettivo, conserva raw e provenance, classifica le regioni, attribuisce identità articolo e produce bozze e record finalizzati.

**Stato attuale:** la CLI salva `PageRecord`, decisioni e link di continuazione, sequenze di reading order, `ArticleDraft` e `ArticleRecord`. Il record finale può avere stato `ready` oppure `review_required`. La CLI non esegue ancora sentiment analysis o Media Reputation Score sugli articoli estratti. Il pipeline ha subito diversi cicli di taratura sull'intero corpus (vedi §17): deduplica e fusione di articoli, recupero dell'indice rassegna, pulizia dei residui di watermark e ricalibrazione delle soglie di `ready`.

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

Copia solo i `article_*.json` finali preservando la struttura `anno/mese/[giorno]/documento/` (file direttamente nella cartella del documento, senza sottocartella `articles/`; i documenti senza articoli vengono saltati; nessun CSV di riepilogo).

## Flusso

```text
PDF
  │
  ├─ DocumentProfiler
  ├─ DoclingParser: estrazione senza OCR + OCR selettivo
  ├─ PageNormalizer e merge_extraction
  ├─ ReviewIndexParser dal raw Docling
  ├─ LayoutReviewIndexParser (fallback regioni quando manca la tabella)
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
  │    TitleResolver (con veto timbri/frasi web)
  │    SubtitleResolver (con veto breadcrumb/chrome)
  │    EditorialHeaderArbitrator (banner + deck)
  │    ReviewIndexMatcher (similarita' robusta OCR)
  │    AuthorResolver
  │    PageClassifier (marker strutturali solo non editoriali)
  │
  │    WebMainContentResolver
  │    InlineIntrusionDetector
  │    WebArticleContinuationResolver (fogli WEB sequenziali)
  │    ArticleClusteringResolver: attribuzione locale, headline duplicate
  │    TitleResolver.consolidate_candidates
  │    ArticleFlowResolver
  │    ArticleClusteringResolver: link web accettati
  │    NewspaperContinuationResolver
  │
  │    per ogni pagina:
  │      BodyContinuationResolver
  │      ArticleClusteringResolver: body recuperati
  │      BodyMembershipRefinement / ColumnMembershipPropagation
  │      GlobalArticleMembershipResolver
  │      SectionHeaderResolver
  │      ArticleClusteringResolver: heading e media
  │      BodyGroupingResolver
  │
  │    CrossPageArticleReconciler
  │    AnchoredWebContinuationResolver (wrapper web -> stampa)
  │    GeneralContinuationResolver
  │    OrphanProximityFallbackResolver
  │    EditorialFragmentRefinementResolver (frammenti di colonna interpolata)
  │    ArticleIntegrityReconciler
  │    scrub_watermark_residue (residui watermark/registro)
  │    ArticleReadingOrderResolver
  │
  ├─ ArticleDraftAssembler
  ├─ ArticleTextNormalizer
  ├─ ArticleFinalizer (rete di sicurezza residui + untitled recovery)
  ├─ WebArticleEnricher (preferenza testo HTML pubblico, assorbimento frammenti)
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

Quando la tabella Docling manca, `LayoutReviewIndexParser` recupera le righe direttamente dalle regioni di pagina. Sono supportati due formati:

- **tre colonne distinte** (data+fonte a sinistra, pagina+titolo al centro, pagina di inizio a destra), allineate per centro verticale della riga;
- **riga intera fusa in una sola regione** (`FUSED_ROW`): `data fonte pagina titolo [rimando] pagina-inizio`. Il rimando a puntini può mancare o essere letto male dall’OCR («ab», «ao»): il separatore è opzionale e i numeri di pagina sono vincolati da limiti di sanità (`START_PAGE_MAX=60`, `ORIG_PAGE_MAX=120`).

La copertina («Rassegna del GG/MM/AAAA») è prova sufficiente: anche una sola riga ben formata produce l’indice (le rassegne con un unico clipping producevano altrimenti zero voci).

**Gatekeeping della pagina indice:** quando le righe vengono riconosciute, le regioni riga vengono escluse dal testo editoriale (`exclude_from_article_text`), le etichette di sezione («STAMPA LOCALE», «WEB», fino a 24 caratteri) non diventano titoli e la pagina viene tipizzata `INDEX`. Senza questo passaggio l’intero indice veniva ricostruito come un singolo articolo-titolo-etichetta con le righe come body.

`ReviewIndexMatcher` usa titolo e metadata locali per associare le entry a titoli dell’articolo:

- la similarità del titolo combina confronto normalizzato e confronto **compattato** (senza spazi/punteggiatura), robusto alle fusioni OCR («Scafatiy il Sindaco Alibertialla»);
- lo score somma similarità (max 0,60) e bonus per data/fonte/pagina originale uguali;
- **override a evidenza forte**: se fonte, data e pagina originale coincidono tutte, il match si accetta anche sotto la soglia di similarità (pavimento 0,50), perché la tripla esclude altri articoli.

Produce `ReviewIndexEntry` con provenance delle celle. L’indice è una **prior**, non ground truth: non sovrascrive automaticamente il contenuto del PDF. La categoria è prevista nel modello ma non viene inferita da heading generali quando l’associazione alla riga non è verificabile.

## 6. Titolo, sottotitolo, autore e section header

`TitleResolver` valuta label Docling, stile relativo al body, bold, geometria, posizione e somiglianza con l’indice. Non impone un titolo unico per pagina: l’unicità del main title può essere applicata per identità articolo disponibile.

Due veti impediscono che testo non editoriale diventi title anchor:

- **veto timbro di registro** (`is_stamp_code_text`): regioni di soli codici/timbri del provider («DS1667 DS1667», «DATASTAMPA1667») non sono mai titoli; un timbro promosso a titolo catturava le colonne sottostanti e produceva articoli fantasma. Il timbro viene degradato a `WATERMARK` ed escluso dal testo. Un testo con almeno una parola editoriale («Miasmi DS1667 dal Sarno…») resta un titolo legittimo.
- **veto frasi web** («Clicca qui sotto per andare all’articolo originale», «leggi l’articolo originale», «read more»): chrome della stampa browser→PDF, degradato a `UNKNOWN` ed escluso.

`EditorialHeaderArbitrator` risolve gli stack di headline **prima** del clustering: occhiello sopra/sotto il titolo principale, e la regola **banner a tutta larghezza** — un titolo che copre almeno il 55% della larghezza pagina con un titolo più stretto immediatamente sotto è la headline, quello stretto è il suo deck (sottotitolo). La guardia di sovrapposizione verticale tollera piccoli sconfinamenti (banner che sfiora il deck) ma esclude i titoli affiancati in colonne diverse.

`SubtitleResolver` cerca regioni sopra o sotto il titolo, usando prossimità, overlap, stile e posizione rispetto all’inizio del body. Conserva le assegnazioni ambigue. Non sono mai sottotitoli:

- i **breadcrumb web** («Home > Sezione > … > Titolo articolo»): contengono un richiamo al titolo ma sono navigazione;
- il **chrome web** in generale: glifi di icone OCRizzati, etichette corte («art»), contatori «Visitatori unici giornalieri», date di pubblicazione, byline tecniche («AUTHOR: …», «min. read»).

`clean_article_title` (normalizzazione/titolo, `normalization/title_cleanup.py`) ripulisce il titolo assegnato al record: token identici consecutivi dedotti, codici di registro rimossi anche quando l’OCR li ha **fusi** dentro le parole — prefisso («DS16Tornano»), cifre spurious in testa («6Imprenditore»), codice in coda («ImprenditoreS1667»), cifre incuneate («FOSSOIMPERATORE667RESIDENTI») e cifre dopo virgola («fognaria,67»). Le azioni sono registrate nei warning del record.

`AuthorResolver` valuta byline esplicite e nomi impliciti tramite forma del nome, posizione, stile e autore eventualmente indicato da un’entry indice associata al titolo. Il testo dell’indice non crea da solo una regione autore.

`SectionHeaderResolver` cerca heading interni fra body compatibili prima e dopo; conserva score, regioni di riferimento e stato confermato o provvisorio dell’associazione.

## 7. Body seed, clustering e continuazione

La classificazione distingue:

- `ARTICLE_BODY` con `body_role="seed"`: blocco sufficientemente affidabile;
- `UNKNOWN`: blocco non ancora attribuito, anche se breve;
- `ARTICLE_BODY` con `body_role="continuation"`: frammento recuperato tramite continuità.

La soglia indicativa di 18 parole serve ai seed, non vieta che un blocco corto diventi body. `BodyContinuationResolver` richiede un riferimento con `article_id`, confronta colonna, prossimità in avanti, overlap, larghezza e stile, e aggiunge immediatamente ogni frammento promosso ai riferimenti. Continua fino a convergenza, senza fondere le bbox.

`EditorialFragmentRefinementResolver` recupera frammenti `UNKNOWN` non assegnati con due regole ad alta precisione: frammenti **racchiusi** da body coerenti dello stesso proprietario (continuano una frase o sono compresi fra due segmenti) e frammenti in **colonna interpolata** — adiacenti in orizzontale (≤ 18 pt) a una colonna già posseduta da un solo articolo, senza body concorrenti nella stessa fascia orizzontale con overlap verticale e dentro l’estensione verticale dell’articolo. È il caso delle colonne mai «possedute» perché l’OCR le ha frammentate in unknown (colonna 3 tra la 2 e la 4 di un clipping).

`ArticleClusteringResolver` assegna `article_id` quando dispone di evidenza sufficiente da titolo, body, link e geometria. Mantiene score, metodo e alternative nei metadata. In caso ambiguo l’ID può restare assente. La **fusione di headline duplicate** (`duplicate_headline_v1`) unisce alla stessa identità i titoli ripetuti sulla stessa pagina quasi identici (normalizzazione, suffisso del sito rimosso, similarità ≥ 0,90, overlap di colonna ≥ 0,25): evita che lo stesso clipping diventi due articoli. Le righe URL e le righe di menu del sito non agiscono da barrier di titolo.

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
- eventuale stesso autore;
- **fogli web sequenziali**: le pagine web della rassegna portano un footer «WEB» con numero di foglio progressivo; due pagine con numeri consecutivi («WEB 1» → «WEB 2») appartengono con ogni probabilità allo stesso articolo multipagina (componente `sequential_web_sheet`).

Il classificatore di pagina conta i marker strutturali (foglio, tiratura, diffusione, lettori…) **solo su regioni non editoriali**: una parola come «diffusione» dentro il body di un articolo web non trasforma la pagina in un clipping di quotidiano (un solo marker accidentale nel body spezzava la catena di continuazione).

Metadata **mancanti** non diventano automaticamente contraddizioni. URL e fonti presenti ma differenti, o un nuovo titolo, possono impedire il collegamento. Le proposte, gli score e le contraddizioni sono salvati separatamente.

`AnchoredWebContinuationResolver` collega una **pagina web wrapper** (titolo con slug dell’URL ≥ 0,70 e nessun body proprio, tipica «Clicca qui sotto…» + headline + link) alle pagine seguenti: o la prima pagina ripete il titolo nella sua lane principale, oppure — fallback «untitled body lane» — la pagina successiva non ha titoli propri e ha un blocco body dominante non assegnato. In questo caso la stampa dell’articolo (puro contenuto, senza titolo) viene legata al titolo del wrapper e produce un record titolato invece di un untitled duplicato del fetch web.

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

### Residui di watermark e registro di stampa

`scrub_watermark_residue` (normalizzazione/watermark_scrub.py) viene eseguito sulle pagine **dopo** la classificazione e **prima** del reading order, così i segmenti copiano testo già pulito. I provider di rassegna (Data Stampa) applicano watermark e codici di registrazione (DS1667, DATASTAMPA1667) che l’OCR fonde nei token del testo:

1. raccoglie i token di registro **attestati** in regioni non editoriali (watermark, rights notice, header, footer);
2. li rimuove dal testo di body, titoli e regioni unknown, anche fusi dentro le parole («futuroDS1667 con», titoli «…DS1667 DS1667 Gori…»);
3. rimuove le varianti di «riproduzione riservata» (pulita, prefissata `@`/`©`, fusa senza spazi, con underscore).

Ogni azione è registrata in `region.metadata["watermark_scrub"]`; il testo raw resta nei JSON raw e nella provenance. I titoli non vengono mai svuotati dalla pulizia; le regioni che contenevano solo la dicitura dei diritti diventano vuote. Una seconda passata (`scrub_rights_phrase`) copre i body costruiti dopo lo scrub: testo web estratto con trafilatura (footer del sito) e record finalizzati.

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

Un record incerto viene comunque salvato come `review_required`. Lo stato `ready` richiede titolo, body non vuoto, metadata richiesti dalla configurazione, confidence di ricostruzione sufficiente, completezza senza motivi di squalifica e assenza di link ancora candidati o contraddizioni rilevate.

**Soglie di completezza calibrate** (`ExtractionCompletenessConfig`): coverage del body ≥ 0,90, retention OCR ≥ 0,94 (0,92 per body interamente OCR), residui ammessi fino a 150 char non assegnati / 100 char non mappati / 200 char editoriali irrisolti, con una franchigia relativa del 10% dei caratteri assegnati. Con le soglie precedenti (0,95/0,97 e 20/0/40 char) il 91% dei record era `review_required` e il flag non filtrava più nulla. Il warning «Draft body includes candidate regions» viene emesso solo se esistono davvero segmenti candidati (prima era incondizionato su ogni draft).

Il finalizer esegue anche una **rete di sicurezza sui residui di copyright**: le diciture «riproduzione riservata» (con varianti `@`, `©`, fuse o con underscore) vengono rimosse da `body` e `body_clean` di ogni record, qualunque sia la via con cui il body è stato costruito (PDF, web, frammenti assorbiti). `body_raw` resta intatto perché i charspan dei segmenti PDF si riferiscono a quella stringa.

La **recupero di record untitled** crea un record senza titolo per gruppi di body orfani non attribuibili; i frammenti duplicati di un clipping già coperto da un record titolato vengono ora assorbiti o eliminati, e il warning del resolver `WebArticleEnricher` registra le fusioni di frammenti web (`absorb_web_fragment`) con le condizioni usate.

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
│   ├── article_integrity.json
│   ├── global_membership.json
│   ├── general_continuation.json
│   ├── editorial_fragments.json
│   ├── orphan_proximity.json
│   ├── newspaper_continuation_decisions.json
│   ├── web_continuation_decisions.json
│   ├── anchored_web_links.json
│   ├── web_retrieval.json
│   ├── review_index_entries.json
│   └── review_index_matches.json
├── articles/
│   └── article_NNNN.json                 # record finali (title, date, body)
├── document_report.json
└── debug/
    └── page_NNN.png                     # con --debug-bbox
```

La CLI arricchisce anche le miniature della posizione dell’articolo prima di salvare i `PageRecord`. Score, ID articolo, scope, provenance, alternative e warning restano ispezionabili nei JSON.

## 15. Configurazione

`src/press_reputation/config.py` contiene configurazioni per profiling/OCR, Review Index, metadata header, classificazione, entity lookup, watermark, clustering, body continuation, reading order, continuazione cartacea, normalizzazione testuale e finalizzazione.

`classification/web_content_config.py` configura contenuto web e intrusion inline. `WebContinuationConfig` e `ArticleFlowConfig` configurano proposta e accettazione dei link web. Alcuni componenti mantengono proprie soglie locali.

Le soglie di completezza di `ExtractionCompletenessConfig` e le soglie del matcher dell’indice (`ReviewIndexConfig`) sono state **calibrate quantitativamente** sull’intero corpus (vedi §17): i valori correnti sono quelli riportati in §5 e §12. Le restanti soglie locali restano da tarare.

## 16. Limiti e correzioni aperte

- **Link web:** nel percorso finale di `ArticleFlowResolver.resolve()` manca l’aggiunta del `FlowLink` valutato a `links`. È una correzione necessaria prima di considerare operativo il web cross-page linking e di affidarsi al relativo stato di finalizzazione.
- **Metadata su pagine multi-articolo:** `ArticleFinalizer.unambiguous_pages()` considera i titoli con ID; un ulteriore titolo senza `article_id` sulla stessa pagina può rendere ambigui i metadata di pagina senza essere rilevato da quel controllo.
- **Location:** vengono inserite nel record soltanto quelle attribuite all’articolo. Una location non clusterizzata non deve essere copiata indiscriminatamente da tutta la pagina.
- **Extraction confidence:** senza confidence OCR/native misurata può restare `null`; la reconstruction confidence è euristica e richiede valutazione sul corpus.
- **OCR e testo:** la pulizia Unicode/OCR è selettiva. Un lessico di dehyphenation vuoto lascia irrisolte alcune parole spezzate per evitare di distruggere composti autentici. Le strisce di immagini a bassa risoluzione (es. rassegne su carta termica) restano in OCR inutilizzabile anche alzando i DPI.
- **Reading order:** non riordina il testo all’interno di bbox multi-colonna indivise e non produce ancora una sequenza multimodale completa con media, tabelle e caption.
- **Web intrusion:** il detector richiede body prima e dopo sulla stessa pagina/area; i brevi `UNKNOWN` web con scope non ancora `main` possono rimanere fuori dal body.
- **Watermark:** `WatermarkDetectionConfig.minimum_editorial_overlap` vale attualmente `2`, mentre l’overlap varia tra `0` e `1`; quel componente dello score non si attiva.
- **Autore:** la lista negativa contiene attualmente `archivio storcio` anziché `archivio storico`.
- **Titoli con codici residui:** ~22 titoli del corpus conservano frammenti di codice di registro non intercettati dalle regex attuali (codici spezzati in più punti o prefissi atipici).
- **Record boilerplate:** un titolo junk («Sostieni la Provinciaonline», sezione del sito) può catturare contenuto editoriale orfano: il testo viene preservato ma resta sotto un titolo non editoriale.
- **Voci indice senza clipping:** ~95 documenti hanno voci d’indice pulite il cui articolo non viene affatto estratto dal PDF (clipping assente o non riconoscibile): è un gap di copertura dell’estrazione, non di matching.
- **Subhead di colonna:** i blocchi in maiuscolo fra le colonne (occhielli interni) non vengono promossi a section header e restano orfani.
- La CLI non esegue ancora sentiment analysis target-aware né calcola automaticamente un Media Reputation Score dagli `ArticleRecord`.

**Principio operativo:** conservare raw Docling, profili, bbox e testo originali, provenance, score, indizi e warning. Un ArticleRecord `review_required` rimane disponibile per analisi e debugging, ma non deve essere trattato come un articolo verificato.

## 17. Storico delle modifiche e fix

Cicli di taratura sull’intero corpus (Risultati Gori 2→6) con test di regressione a campione (49 PDF stratificati) a ogni giro. Effetti complessivi corpus (Gori_4 → Gori_6):

| Metrica | Gori_4 | Gori_6 |
| --- | --- | --- |
| Articoli | 5.872 | 5.415 (duplicati e phantom rimossi) |
| Con titolo | 4.588 (78,1%) | 4.764 (88,0%) |
| Titolati con body ≥ 400 char | 4.098 (74%) | 4.442 (82,0%) |
| Record `ready` | 468 | 1.379 |
| Voci indice / matchate | 3.306 / 2.266 | 3.597 / 2.673 (74,3%) |
| Body inquinati da watermark | 18 | 1 |
| Record untitled | — | 651 (da 1.017 in G5) |

### Indice rassegna

- `layout_parser`: accettazione di un’unica riga (la copertina è già una prova); fallback **FUSED_ROW** per righe fuse in una regione, con rimando opzionale e letture OCR dei puntini; limiti di sanità sui numeri di pagina.
- Gatekeeping: righe indice escluse dal testo editoriale, etichette «STAMPA LOCALE»/«WEB» non promosse a titolo, pagina tipizzata `INDEX` (l’intero indice non diventa più un articolo).
- `ReviewIndexMatcher`: similarità compattata robusta all’OCR + override a evidenza forte (fonte+data+pagina). Matched 68,5% → 74,3%, +146 documenti con indice recuperato.

### Titoli e sottotitoli

- Veto titoli-timbro («DATASTAMPA1667») con demozione a `WATERMARK`: eliminati gli articoli fantasma che catturavano colonne intere.
- Veto frasi chrome («Clicca qui sotto…»): da 15 titoli junk a 0.
- `EditorialHeaderArbitrator`: regola banner largo + deck (titolo stretto sotto un banner ≥ 55% pagina → sottotitolo, non articolo separato) e sconfinamenti verticali piccoli tollerati.
- `clean_article_title`: rimozione codici fusi in ogni posizione (prefisso, cifre in testa, coda, cifre incuneate, cifre dopo virgola).
- `SubtitleResolver`: veto breadcrumb («Home > …») e chrome web (glifi, contatori visitatori, date, byline tecniche). Subtitle junk da 366 a 4.

### Body e ricostruzione

- `EditorialFragmentRefinementResolver`: nuova regola **colonna interpolata** per frammenti unknown in colonne mai possedute (~720 frammenti recuperati sul corpus, +8,7k char di body).
- `ArticleClusteringResolver`: fusione di headline duplicate sulla stessa pagina (`duplicate_headline_v1`); righe URL e menu non fungono da barrier; il testo frammentato di un clipping non produce più due articoli (es. multipagina web divisa in 3).
- `WebArticleEnricher._absorb_web_fragments`: assorbimento dei frammenti web duplicati nel record selezionato (pagine adiacenti, overlap di shingle, relazione di titolo).
- Marker strutturali contati solo su regioni non editoriali: una parola nel body («delega alla diffusione») non flippa più la pagina a clipping, spezzando la continuazione web.

### Continuazione multipagina

- `WebArticleContinuationResolver`: componente **fogli WEB sequenziali** (footer «WEB n» → «n+1»); catene web multipagina ora complete (es. articoli stampati su due fogli fusi in un unico record).
- `AnchoredWebContinuationResolver`: fallback **untitled body lane** — wrapper web (titolo+URL, no body) legato alla stampa senza titolo della pagina dopo (~60 link accettati, record titolati invece di untitled duplicati).
- Soglia OCR su testo rado: `min_image_coverage_with_sparse_text` 0,10 → 0,05 (+28 pagine OCR sul corpus).

### Watermark e testo

- Nuovo `watermark_scrub`: token di registro attestati rimossi da body/titoli/unknown anche fusi nelle parole; varianti «riproduzione riservata» (@/©/fuse/underscore); scrub del testo trafilatura; rete di sicurezza sui record finalizzati. Body inquinati 18 → 1.
- Warning «candidate regions» emesso solo se esistono segmenti candidati (prima: incondizionato su 3.077 record).

### Finalizzazione

- Soglie di completezza ricalibrate (§12): `ready` da 9% a 25,5% dei record, con 1.346 record pronti con titolo e body pieno; `review_required` da 91% a 74,5%.

### Strumenti

- `scripts/batch_parse.py`: parallelizzazione GPU (worker dinamici sulla VRAM), `--force`, sommari per anno; onnxruntime-gpu consolidato in `pixi.toml`.
- `scripts/export_articles.py`: export dei soli `article_*.json` con struttura preservata (§ Esecuzione).