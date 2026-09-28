# Press Reputation

Pipeline local-first per l'estrazione strutturata da PDF di rassegne stampa.

L'implementazione attuale trasforma un PDF in una rappresentazione normalizzata per pagina, preservando:

- testo estratto
- bounding box
- provenance
- label originali Docling
- output raw Docling
- informazioni tipografiche quando disponibili
- classificazione tecnica delle regioni
- classificazione semantica delle regioni
- metadata strutturati
- informazioni di layout editoriale
- debug visuale con bounding box

Questa fase riguarda la preparazione strutturata dei contenuti, non la sentiment analysis o il reputation scoring finale.

---

## Pipeline attuale

```text
PDF
↓
DoclingParser
↓
raw Docling output
↓
PageNormalizer
↓
PageProcessingPipeline
   ├── PdfStyleEnricher
   ├── DocumentBoilerplateDetector
   ├── TechnicalRegionClassifier
   ├── HeaderMetadataZoneDetector
   ├── RegionClassifier
   ├── BodyContinuationResolver
   ├── BodyGroupingResolver
   ├── MetadataExtractor
   └── PageClassifier
↓
Image analysis su article_position_thumbnail
↓
PageRecord[]
↓
JSON per pagina
↓
debug bbox opzionale
```

---

## Struttura del progetto

```text
src/press_reputation/
├── models/
│   ├── __init__.py
│   ├── page.py
│   └── article.py
│
├── parsers/
│   ├── __init__.py
│   ├── base.py
│   └── docling_parser.py
│
├── normalization/
│   ├── __init__.py
│   └── page_normalizer.py
│
├── pipeline.py
│
├── features/
│   ├── __init__.py
│   ├── region_features.py
│   └── page_features.py
│
├── classification/
│   ├── __init__.py
│   ├── region_classifier.py
│   ├── technical_region_classifier.py
│   ├── page_classifier.py
│   ├── header_metadata_zone.py
│   └── boilerplate_detector.py
│
├── reconstruction/
│   ├── __init__.py
│   ├── body_continuation.py
│   └── body_grouping.py
│
├── metadata/
│   ├── __init__.py
│   └── metadata_extractor.py
│
├── style/
│   ├── __init__.py
│   └── pdf_style_enricher.py
│
├── image_analysis/
│   ├── __init__.py
│   └── position_thumbnail.py
│
├── visualization/
│   ├── __init__.py
│   └── bbox_overlay.py
│
├── reputation/
│   ├── __init__.py
│   ├── models.py
│   ├── weights.py
│   ├── scoring.py
│   └── bootstrap.py
│
├── resources/
│   ├── agenzie_rassegna_stampa_italia.json
│   ├── comuni_italiani.json
│   └── newspaper.json
│
├── lookup.py
├── config.py
└── cli.py
```

---

## Ordine logico dei moduli

### 1. `models`

Contiene i modelli dati Pydantic v2.

File principali:

```text
models/page.py
models/article.py
```

Modelli principali:

- `PageRecord`
- `Region`
- `SourceInfo`
- `ClippingInfo`
- `ArticleRecord`

`PageRecord` rappresenta una pagina PDF normalizzata.

Campi principali:

```text
document_id
pdf_page
page_type
section
page_width
page_height
source
clipping
regions
```

Ogni `Region` conserva:

```text
type
text
bbox
raw_label
provenance
metadata
style
boilerplate
boilerplate_frequency
exclude_from_article_text
article_id
```

Tipi regione principali:

```text
header_metadata
source_name
press_review_provider
publication_date
original_page
clipping_sheet
location
article_title
article_subtitle
article_section_header
article_body
author
image
article_position_thumbnail
caption
watermark
rights_notice
footer
sidebar
advertisement
navigation
related_content
unknown
```

---

### 2. `parsers`

Contiene l'astrazione dei parser e l'adapter Docling.

File principali:

```text
parsers/base.py
parsers/docling_parser.py
```

`DocumentParser` definisce l'interfaccia astratta.

`DoclingParser` usa Docling per elaborare il PDF.

Responsabilità:

- ricevere il path del PDF
- verificare che il file esista
- eseguire la conversione con Docling
- restituire il documento Docling raw
- salvare il raw output in JSON

Output:

```text
raw/docling.json
```

---

### 3. `normalization`

File principale:

```text
normalization/page_normalizer.py
```

`PageNormalizer` ora fa solo normalizzazione strutturale:

```text
DoclingDocument
↓
PageRecord[] grezzi
```

Responsabilità:

- inizializzare una `PageRecord` per ogni pagina PDF
- leggere elementi Docling
- estrarre testo
- estrarre bounding box
- normalizzare coordinate bbox
- conservare provenance
- conservare `raw_label`
- popolare `page_width` e `page_height`

Importante:

```text
Docling section_header NON viene convertito automaticamente in article_title.
```

Il raw label viene conservato:

```json
{
  "type": "unknown",
  "raw_label": "section_header"
}
```

La classificazione semantica avviene dopo, nella pipeline.

---

### 4. `pipeline`

File:

```text
pipeline.py
```

Contiene:

```python
PageProcessingPipeline
```

Responsabilità:

```text
PageRecord[] grezzi
↓
PageRecord[] arricchiti e classificati
```

Ordine attuale:

```text
PdfStyleEnricher
DocumentBoilerplateDetector
TechnicalRegionClassifier
HeaderMetadataZoneDetector
RegionClassifier
BodyContinuationResolver
BodyGroupingResolver
MetadataExtractor
PageClassifier
```

La pipeline è separata dal normalizer per rendere testabili i singoli step.

---

### 5. `style`

File principale:

```text
style/pdf_style_enricher.py
```

`PdfStyleEnricher` prova ad arricchire le regioni usando PyMuPDF.

Utilizza informazioni PDF native quando disponibili.

Per ogni regione tenta di popolare:

```json
"style": {
  "font_names": [],
  "dominant_font": null,
  "median_font_size": null,
  "max_font_size": null,
  "bold_ratio": null,
  "italic_ratio": null,
  "dominant_color": null,
  "median_opacity": null
}
```

Le informazioni sono opzionali.

Su PDF scannerizzati o OCR-only la pipeline non deve rompersi.

---

### 6. `features`

File principali:

```text
features/region_features.py
features/page_features.py
```

#### `RegionFeatureExtractor`

Estrae feature da una singola regione:

- lunghezza testo
- numero parole
- uppercase ratio
- URL
- date
- marker clipping
- marker web
- marker autore
- marker advertisement
- marker watermark
- marker rights notice
- bbox e posizione relativa
- riconoscimento testate
- riconoscimento provider rassegna stampa
- riconoscimento comuni italiani

#### `PageFeatureExtractor`

Estrae feature aggregate dalla pagina:

- numero regioni
- numero immagini
- numero regioni body
- numero regioni unknown
- marker clipping
- marker web
- entry indice
- ratio immagini/unknown

---

### 7. `lookup`

File:

```text
lookup.py
```

Carica e indicizza risorse locali.

Risorse:

```text
resources/newspaper.json
resources/comuni_italiani.json
resources/agenzie_rassegna_stampa_italia.json
```

Funzioni principali:

- `get_newspaper`
- `is_known_newspaper`
- `get_press_review_provider`
- `is_press_review_provider`
- `find_municipalities`

Il lookup dei comuni supporta anche nomi composti:

```text
Torre del Greco
Castellammare di Stabia
San Giorgio a Cremano
Sant'Antonio Abate
```

La detection delle testate/provider ha precedenza logica sulla location, per evitare casi come:

```text
CRONACHE DI NAPOLI
```

classificati erroneamente come luogo.

---

### 8. `classification`

Contiene componenti di classificazione separati.

---

#### 8.1 `TechnicalRegionClassifier`

File:

```text
classification/technical_region_classifier.py
```

Classifica regioni tecniche prima della classificazione semantica.

Gestisce:

```text
rights_notice
watermark
advertisement
article_position_thumbnail
caption/image protection
```

Le regioni tecniche vengono escluse dal testo articolo quando necessario:

```json
{
  "exclude_from_article_text": true
}
```

Esempi:

```text
© RIPRODUZIONE RISERVATA
→ rights_notice

Data Stampa 1667-Data Stampa 1667
→ watermark

pubblicità / adv / ads
→ advertisement
```

---

#### 8.2 `HeaderMetadataZoneDetector`

File:

```text
classification/header_metadata_zone.py
```

Identifica una zona iniziale della pagina dedicata ai metadata della rassegna.

Seed principali:

```text
source_name
press_review_provider
publication_date
original_page
clipping_sheet
header_metadata
```

La zona header serve a evitare che i metadata vengano classificati come:

```text
article_title
article_body
location
author
```

Le regioni nella zona possono ricevere:

```json
{
  "metadata": {
    "in_header_metadata_zone": true
  }
}
```

La zona è conservativa e limitata alla parte alta della pagina.

---

#### 8.3 `DocumentBoilerplateDetector`

File:

```text
classification/boilerplate_detector.py
```

Identifica testi ricorrenti nel documento.

Confronta:

```text
normalized text
frequenza su pagine
```

Se una regione compare su una quota significativa di pagine:

```json
{
  "boilerplate": true,
  "boilerplate_frequency": 0.87
}
```

Le regioni boilerplate possono essere escluse dal testo articolo.

---

#### 8.4 `RegionClassifier`

File:

```text
classification/region_classifier.py
```

Classifica semanticamente le regioni non tecniche.

Gestisce:

```text
source_name
press_review_provider
header_metadata
publication_date
original_page
clipping_sheet
location
navigation
related_content
author
article_title
article_subtitle
article_section_header
article_body
```

Punti importanti:

- non gestisce più regioni tecniche già classificate;
- protegge regioni con `exclude_from_article_text`;
- non assume che `section_header` Docling sia titolo principale;
- supporta sottotitoli sopra o sotto il titolo;
- riconosce heading interni come `article_section_header`;
- riconosce autori espliciti e, in modo contestuale, autori impliciti;
- non impone più rigidamente “un titolo per pagina”.

---

#### 8.5 `PageClassifier`

File:

```text
classification/page_classifier.py
```

Classifica la pagina in:

```text
index
clipping
web
pure_text
unknown
```

Usa feature aggregate della pagina.

---

### 9. `reconstruction`

Contiene componenti di ricostruzione logica.

---

#### 9.1 `BodyContinuationResolver`

File:

```text
reconstruction/body_continuation.py
```

Recupera frammenti brevi di body rimasti `unknown`.

Non abbassa semplicemente la soglia del body seed.

Strategia:

```text
body seed affidabili
+
unknown vicini/stilisticamente coerenti
↓
article_body
```

Feature usate:

- stessa colonna
- larghezza simile
- font size simile
- continuità verticale

Scrive:

```json
{
  "metadata": {
    "body_continuation_score": 0.73
  }
}
```

---

#### 9.2 `BodyGroupingResolver`

File:

```text
reconstruction/body_grouping.py
```

Non fonde fisicamente le bbox.

Assegna invece un gruppo logico alle regioni `article_body`.

Esempio:

```json
{
  "type": "article_body",
  "metadata": {
    "body_group_id": "body_001",
    "body_reading_order": 3
  }
}
```

Questo preserva provenance e bounding boxes originali.

---

### 10. `metadata`

File principale:

```text
metadata/metadata_extractor.py
```

Estrae metadata strutturati.

Campi principali:

- source name
- source type
- publication date
- original page
- sheet current / total
- surface percent
- URL
- section

Esempi:

```text
16-SET-2023
→ publication_date = 2023-09-16

da pag. 17
→ original_page = 17

foglio 1 / 2
→ sheet_current = 1
→ sheet_total = 2

Superficie 47 %
→ surface_percent = 47.0
```

Il fallback su tutto il testo pagina è stato evitato.

Ordine preferito:

```text
explicit metadata regions
↓
header metadata zone
↓
fallback molto limitato
```

Questo evita che contenuto articolo venga interpretato come metadata.

---

### 11. `image_analysis`

File principale:

```text
image_analysis/position_thumbnail.py
```

Analizza regioni classificate come:

```text
article_position_thumbnail
```

Queste immagini rappresentano la posizione dell’articolo nella pagina originale del giornale.

Calcola:

- area relativa occupata dall’articolo
- posizione verticale:
  - top
  - middle
  - bottom
- posizione orizzontale:
  - left
  - center
  - right
- zona combinata:
  - top_left
  - top_center
  - top_right
  - middle_left
  - middle_center
  - middle_right
  - bottom_left
  - bottom_center
  - bottom_right
- prominence layout

La prominence usa:

- pagina originale
- area relativa
- posizione verticale

Esempio:

```json
{
  "type": "article_position_thumbnail",
  "metadata": {
    "technical_image": true,
    "exclude_from_article_media": true,
    "detected_article_marker": true,
    "article_marker_bbox_in_thumbnail": [0.62, 0.12, 0.91, 0.38],
    "original_page_relative_area": 0.075,
    "original_page_vertical_area": "top",
    "original_page_horizontal_area": "right",
    "original_page_zone": "top_right",
    "original_page_for_prominence": 8,
    "estimated_prominence": "medium",
    "prominence_score": 0.48
  }
}
```

Questa prominence è una feature di layout editoriale, non un reputation score.

---

### 12. `visualization`

File principale:

```text
visualization/bbox_overlay.py
```

Genera immagini diagnostiche con bounding box e label.

Input:

```text
PDF + PageRecord
```

Output:

```text
PNG con bounding box e label
```

Serve per debug manuale della classificazione.

---

### 13. `reputation`

Contiene il primo motore matematico per Media Reputation Score.

File principali:

```text
reputation/models.py
reputation/weights.py
reputation/scoring.py
reputation/bootstrap.py
```

Il modulo non implementa ancora sentiment analysis.

Riceve conteggi già calcolati:

```text
positive_sentences
negative_sentences
neutral_sentences
```

Calcola:

- sentiment articolo
- prominence
- recency
- audience normalization
- source relevance
- article weight
- Media Reputation Score
- visibility
- coverage distribution
- bootstrap confidence interval

Il Media Reputation Score è una proxy della direzione della copertura mediatica, non una misura diretta dell’opinione pubblica.

---

## Convenzione bounding box

La convenzione interna è:

```text
[x0, y0, x1, y1]
```

Coordinate:

```text
origine: top-left
unità: punti PDF
asse Y: cresce verso il basso
```

Docling può produrre coordinate con origine diversa.

Il normalizer converte le coordinate quando necessario.

---

## Output generato

Gli output sono organizzati fuori da `src`.

Struttura consigliata:

```text
results/
└── nome_documento/
    ├── raw/
    │   └── docling.json
    ├── pages/
    │   ├── page_001.json
    │   ├── page_002.json
    │   └── ...
    └── debug/
        ├── page_001.png
        ├── page_002.png
        └── ...
```

---

## Esempio semplificato di `PageRecord`

```json
{
  "document_id": "Evidenze Rassegna 24042026.pdf",
  "pdf_page": 5,
  "page_type": "clipping",
  "section": "STAMPA_LOCALE",
  "page_width": 595.0,
  "page_height": 842.0,
  "source": {
    "name": "Metropolis",
    "type": "newspaper",
    "publication_date": "2026-04-24",
    "original_page": 7,
    "url": null
  },
  "clipping": {
    "sheet_current": 1,
    "sheet_total": 2,
    "surface_percent": null
  },
  "regions": [
    {
      "type": "source_name",
      "text": "Metropolis",
      "bbox": [250.0, 20.0, 340.0, 42.0],
      "raw_label": "text",
      "metadata": {},
      "style": {},
      "exclude_from_article_text": false
    },
    {
      "type": "article_title",
      "text": "Veleni nel Sarno Stretta della Procura sulle aziende «nere»",
      "bbox": [90.0, 110.0, 510.0, 260.0],
      "raw_label": "section_header"
    },
    {
      "type": "article_subtitle",
      "text": "Blitz della capitaneria: sigilli ad un cantiere nautico...",
      "bbox": [90.0, 265.0, 510.0, 320.0],
      "raw_label": "text"
    },
    {
      "type": "author",
      "text": "Michele De Feo",
      "metadata": {
        "author_detection": "implicit_contextual"
      }
    },
    {
      "type": "article_body",
      "text": "Prosegue senza sosta l'offensiva...",
      "metadata": {
        "body_group_id": "body_001",
        "body_reading_order": 1
      }
    },
    {
      "type": "watermark",
      "text": "Data Stampa 1667-Data Stampa 1667",
      "exclude_from_article_text": true
    },
    {
      "type": "article_position_thumbnail",
      "bbox": [470.0, 700.0, 560.0, 810.0],
      "metadata": {
        "technical_image": true,
        "exclude_from_article_media": true,
        "estimated_prominence": "medium",
        "prominence_score": 0.48
      }
    }
  ]
}
```

---

## Comandi utili

### Parsing completo

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "Data/2026/Aprile/Evidenze Rassegna 24042026.pdf"
```

### Parsing con debug bbox

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "Data/2026/Aprile/Evidenze Rassegna 24042026.pdf" --debug-bbox
```

---

## Dipendenze principali

Il progetto usa Pixi.

Dipendenze principali:

- Python 3.12
- Docling
- Pydantic v2
- Typer
- Rich
- orjson
- python-dateutil
- tqdm
- PyMuPDF
- pytest
- ruff

---

## Stato attuale

Implementato:

- modelli dati Pydantic
- astrazione parser
- parser Docling
- raw JSON export
- normalizzazione strutturale PageRecord
- pipeline post-normalizzazione separata
- style enrichment opzionale
- feature extraction regioni/pagine
- lookup testate/provider/comuni
- classificazione tecnica
- header metadata zone
- boilerplate detection
- classificazione semantica regioni
- body continuation
- body grouping
- metadata extraction conservativa
- page classification
- image analysis per article position thumbnail
- debug bbox
- primo modulo matematico Media Reputation Score

Non implementato completamente:

- article reconstruction multipagina
- clustering articoli
- OCR/logo matching per testate solo grafiche
- sentiment analysis
- entity/company relevance
- benchmark formale con altri parser

---

## Principi attuali

- Il parser resta separato dal normalizer.
- Il normalizer non fa classificazione semantica.
- La pipeline semantica è orchestrata separatamente.
- I modelli dati non dipendono da Docling.
- L'output raw Docling viene conservato.
- Le bounding box vengono mantenute.
- La provenance viene conservata.
- Le classificazioni sono conservative.
- I metadata non vengono inventati.
- I watermark e rights notice sono esclusi dal testo articolo.
- Non si fondono fisicamente bbox distinte del body.
- Il body logico viene rappresentato tramite metadata di gruppo.
- Non vengono usati LLM o API esterne.
- Le feature di layout editoriale non sono sentiment score.