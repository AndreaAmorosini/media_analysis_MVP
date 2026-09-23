# Press Reputation

Pipeline local-first per l'estrazione strutturata da PDF di rassegne stampa.

L'implementazione attuale trasforma un PDF in una rappresentazione normalizzata per pagina, preservando:

- testo estratto
- bounding box
- provenance
- label originali del parser
- output raw di Docling
- classificazione delle regioni
- metadata strutturati
- informazioni di layout editoriale quando disponibili

Questa fase riguarda esclusivamente il primo stadio della pipeline di estrazione.

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
RegionFeatureExtractor
↓
RegionClassifier
↓
MetadataExtractor
↓
PageClassifier
↓
Image analysis su article_position_thumbnail
↓
PageRecord[]
↓
JSON per pagina
↓
debug bbox opzionale
```

## Struttura del progetto

```text
src/press_reputation/
├── models/
│   ├── __init__.py
│   ├── page.py
│   └── article.py
├── parsers/
│   ├── __init__.py
│   ├── base.py
│   └── docling_parser.py
├── normalization/
│   ├── __init__.py
│   └── page_normalizer.py
├── features/
│   ├── __init__.py
│   ├── region_features.py
│   └── page_features.py
├── classification/
│   ├── __init__.py
│   ├── region_classifier.py
│   └── page_classifier.py
├── metadata/
│   ├── __init__.py
│   └── metadata_extractor.py
├── image_analysis/
│   ├── __init__.py
│   └── position_thumbnail.py
├── visualization/
│   ├── __init__.py
│   └── bbox_overlay.py
├── resources/
│   ├── agenzie_rassegna_stampa_italia.json
│   ├── comuni_italiani.json
│   └── newspaper.json
├── lookup.py
└── cli.py
```

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

Enum principali:

- `PageType`
- `SourceType`
- `RegionType`

`PageRecord` rappresenta una pagina PDF normalizzata.

Contiene:

- `document_id`
- `pdf_page`
- `page_type`
- `section`
- `page_width`
- `page_height`
- `source`
- `clipping`
- `regions`

Ogni `Region` conserva:

- `type`
- `text`
- `bbox`
- `raw_label`
- `provenance`
- `metadata`

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

Responsabilità principali:

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

Contiene la conversione dal formato raw del parser al formato interno `PageRecord`.

File principale:

```text
normalization/page_normalizer.py
```

Responsabilità principali:

- inizializzare una `PageRecord` per ogni pagina PDF
- leggere gli elementi Docling
- estrarre testo
- estrarre bounding box
- convertire le coordinate delle bounding box
- conservare provenance
- conservare il label originale Docling
- mappare i label Docling in `RegionType`
- popolare `page_width` e `page_height`

Convenzione interna bbox:

```text
[x0, y0, x1, y1]
origine: top-left
unità: punti PDF
asse Y: cresce verso il basso
```

---

### 4. `features`

Contiene feature extractor per regioni e pagine.

File principali:

```text
features/region_features.py
features/page_features.py
```

#### `RegionFeatureExtractor`

Estrae feature da una singola regione:

- lunghezza testo
- numero parole
- rapporto maiuscole
- presenza URL
- presenza data
- marker clipping:
  - `foglio`
  - `superficie`
  - `tiratura`
  - `diffusione`
  - `lettori`
  - `dir. resp.`
  - `quotidiano`
- marker web:
  - `newsletter`
  - `condividi`
  - `twitta`
  - `articoli correlati`
  - `ultimi articoli`
  - `cookie`
- bbox e posizione relativa
- riconoscimento testate giornalistiche
- riconoscimento agenzie di rassegna stampa
- riconoscimento comuni italiani

#### `PageFeatureExtractor`

Estrae feature aggregate dalla pagina:

- numero regioni
- numero immagini
- numero regioni unknown
- numero titoli
- numero body
- marker clipping
- marker web
- possibili entry indice

---

### 5. `lookup`

File:

```text
lookup.py
```

Carica e indicizza risorse locali per lookup veloci.

Risorse usate:

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

I lookup sono ottimizzati tramite:

- `lru_cache`
- dizionari normalizzati
- indici per primo token per i comuni

La ricerca dei comuni supporta anche nomi composti, per esempio:

```text
Torre del Greco
Castellammare di Stabia
San Giorgio a Cremano
Sant'Antonio Abate
```

---

### 6. `classification`

Contiene la classificazione deterministica di regioni e pagine.

File principali:

```text
classification/region_classifier.py
classification/page_classifier.py
```

#### `RegionClassifier`

Classifica singole regioni usando:

```text
RegionFeatures + contesto pagina
```

Tipi regione attuali:

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
article_body
author
image
article_position_thumbnail
caption
footer
sidebar
advertisement
navigation
related_content
unknown
```

Esempi:

```text
Metropolis
→ source_name

Data Stampa
→ press_review_provider

art Gragnano
→ location
```

Nel caso del luogo, il testo originale resta invariato:

```json
"text": "art Gragnano"
```

ma nei metadata viene salvato il comune riconosciuto:

```json
"metadata": {
  "location_name": "Gragnano",
  "municipalities": [...]
}
```

#### `PageClassifier`

Classifica il tipo pagina usando `PageFeatures`.

Tipi pagina attuali:

```text
index
clipping
web
pure_text
unknown
```

La pagina indice viene riconosciuta con euristiche conservative, per esempio:

- prima pagina
- assenza marker clipping
- molte righe brevi o riferimenti pagina
- basso contenuto testuale narrativo

---

### 7. `metadata`

Contiene l'estrazione deterministica dei metadata.

File principale:

```text
metadata/metadata_extractor.py
```

Responsabilità principali:

- estrarre la data di pubblicazione
- estrarre la pagina originale della fonte
- estrarre informazioni di foglio
- estrarre superficie occupata dal ritaglio
- estrarre URL
- estrarre sezione della rassegna
- estrarre nome fonte se riconosciuto

Esempi gestiti:

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

Quando Docling restituisce un blocco composito, per esempio:

```text
da pag. 8 / 17-GEN-2025 foglio 1
```

la regione viene trattata come metadata composito:

```json
{
  "type": "header_metadata",
  "metadata": {
    "original_page": 8,
    "publication_date": "2025-01-17",
    "sheet_current": 1,
    "sheet_total": null
  }
}
```

---

### 8. `image_analysis`

Contiene analisi leggere sulle immagini tecniche estratte dal PDF.

File principale:

```text
image_analysis/position_thumbnail.py
```

Analizza regioni classificate come:

```text
article_position_thumbnail
```

Queste miniature rappresentano di solito la posizione dell'articolo nella pagina originale del giornale.

L'analisi calcola:

- area relativa occupata dall'articolo nella miniatura
- posizione verticale:
  - `top`
  - `middle`
  - `bottom`
- posizione orizzontale:
  - `left`
  - `center`
  - `right`
- zona combinata:
  - `top_left`
  - `top_center`
  - `top_right`
  - `middle_left`
  - `middle_center`
  - `middle_right`
  - `bottom_left`
  - `bottom_center`
  - `bottom_right`
- prominence stimata

La prominence usa:

- numero pagina originale
- area relativa occupata
- posizione verticale nella pagina

Esempio metadata:

```json
{
  "technical_image": true,
  "exclude_from_article_media": true,
  "thumbnail_detection_method": "dark_pixel_bbox",
  "detected_article_marker": true,
  "article_marker_bbox_in_thumbnail": [
    0.62,
    0.12,
    0.91,
    0.38
  ],
  "original_page_relative_area": 0.075,
  "original_page_vertical_area": "top",
  "original_page_horizontal_area": "right",
  "original_page_zone": "top_right",
  "original_page_coarse_vertical_area": "top_area",
  "original_page_coarse_horizontal_area": "right",
  "original_page_for_prominence": 8,
  "estimated_prominence": "medium",
  "prominence_score": 0.48
}
```

Questa prominence non è un reputation score.

È solo una feature di layout editoriale.

---

### 9. `visualization`

Contiene strumenti di debug visuale.

File principale:

```text
visualization/bbox_overlay.py
```

Responsabilità principali:

- renderizzare una pagina PDF come immagine
- disegnare le bounding box delle regioni
- mostrare il tag assegnato a ogni regione
- salvare un'immagine PNG per ispezione manuale

Input:

```text
PDF + PageRecord
```

Output:

```text
PNG con bounding box e label
```

---

## Output generato

Gli output generati sono organizzati fuori da `src`.

Struttura consigliata:

```text
result/
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

## Esempio `PageRecord`

```json
{
  "document_id": "Evidenze Rassegna 17012025.pdf",
  "pdf_page": 2,
  "page_type": "clipping",
  "section": "STAMPA_LOCALE",
  "page_width": 595.0,
  "page_height": 842.0,
  "source": {
    "name": "Metropolis",
    "type": "newspaper",
    "publication_date": "2025-01-17",
    "original_page": 8,
    "url": null
  },
  "clipping": {
    "sheet_current": 1,
    "sheet_total": null,
    "surface_percent": null
  },
  "regions": [
    {
      "type": "location",
      "text": "art Gragnano",
      "bbox": [
        124.0,
        58.33,
        191.66,
        74.33
      ],
      "raw_label": "text",
      "metadata": {
        "location_name": "Gragnano",
        "municipalities": [
          {
            "comune": "Gragnano",
            "provincia": "Napoli",
            "regione": "Campania",
            "matched_name": "gragnano"
          }
        ]
      }
    },
    {
      "type": "header_metadata",
      "text": "da pag. 8 / 17-GEN-2025 foglio 1",
      "metadata": {
        "original_page": 8,
        "publication_date": "2025-01-17",
        "sheet_current": 1,
        "sheet_total": null
      }
    },
    {
      "type": "article_position_thumbnail",
      "bbox": [
        420.0,
        690.0,
        560.0,
        810.0
      ],
      "metadata": {
        "technical_image": true,
        "exclude_from_article_media": true,
        "detected_article_marker": true,
        "original_page_relative_area": 0.075,
        "original_page_zone": "top_right",
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
PYTHONPATH=src pixi run python -m press_reputation.cli parse "Data/2025/Gennaio/Evidenze Rassegna 17012025.pdf"
```

### Parsing con immagini debug

```bash
PYTHONPATH=src pixi run python -m press_reputation.cli parse "Data/2025/Gennaio/Evidenze Rassegna 17012025.pdf" --debug-bbox
```

### Output atteso

```text
result/
└── Evidenze_Rassegna_17012025/
    ├── raw/
    │   └── docling.json
    ├── pages/
    │   ├── page_001.json
    │   └── page_002.json
    └── debug/
        ├── page_001.png
        └── page_002.png
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
- esportazione raw JSON Docling
- normalizzazione in `PageRecord`
- feature extraction per regioni
- feature extraction per pagine
- classificazione regioni
- classificazione pagina
- lookup locali ottimizzati
- riconoscimento testate giornalistiche
- riconoscimento agenzie di rassegna stampa
- riconoscimento comuni italiani
- estrazione metadata deterministica
- riconoscimento pagina indice
- riconoscimento location sopra titolo
- riconoscimento sottotitolo contestuale
- riconoscimento miniatura posizione articolo
- analisi leggera della miniatura posizione articolo
- stima prominence editoriale basata su layout
- visualizzazione bounding box su PNG

Non implementato:

- test automatici completi
- ricostruzione articoli multipagina
- benchmark con altri parser
- analisi semantica
- sentiment analysis
- reputation scoring

---

## Principi attuali

- Il parser resta separato dal normalizer.
- I modelli dati non dipendono da Docling.
- L'output raw di Docling viene conservato.
- Le bounding box vengono mantenute.
- La provenance viene conservata.
- Le classificazioni sono conservative.
- Se un elemento non è riconosciuto, viene marcato come `unknown`.
- I metadata non vengono inventati.
- Non vengono usati LLM o API esterne.
- Le informazioni di layout editoriale non sono sentiment score.