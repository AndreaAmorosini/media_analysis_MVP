# Press Reputation

Pipeline local-first per l'estrazione strutturata da PDF di rassegne stampa.

L'implementazione attuale trasforma un PDF in una rappresentazione normalizzata per pagina, preservando:

- testo estratto
- bounding box
- provenance
- label originali del parser
- output raw di Docling

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
MetadataExtractor
↓
PageClassifier
↓
PageRecord[]
↓
JSON per pagina
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
├── metadata/
│   ├── __init__.py
│   └── metadata_extractor.py
├── classification/
│   ├── __init__.py
│   └── page_classifier.py
└── visualization/
    ├── __init__.py
    └── bbox_overlay.py
```

## Ordine dei moduli

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

I modelli rappresentano il formato dati interno normalizzato.

### 2. `parsers`

Contiene l'astrazione dei parser e l'adapter Docling.

File principali:

```text
parsers/base.py
parsers/docling_parser.py
```

`DocumentParser` definisce l'interfaccia astratta:

```text
PDF → documento raw
```

`DoclingParser` usa Docling per elaborare il PDF e produrre il documento raw.

Responsabilità principali:

- ricevere il path del PDF
- verificare che il file esista
- eseguire la conversione con Docling
- restituire il documento Docling raw
- salvare il raw output in JSON

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
- usare `unknown` quando il mapping non è sicuro

### 4. `metadata`

Contiene l'estrazione deterministica dei metadata.

File principale:

```text
metadata/metadata_extractor.py
```

Responsabilità principali:

- estrarre la data di pubblicazione
- estrarre la pagina originale della fonte
- estrarre informazioni di foglio
- estrarre la superficie occupata dal ritaglio
- estrarre URL se presenti
- tentare l'estrazione conservativa del nome della fonte

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

Se un valore non viene riconosciuto, resta `null`.

### 5. `classification`

Contiene la classificazione deterministica della pagina.

File principale:

```text
classification/page_classifier.py
```

Valori possibili per `page_type`:

```text
clipping
web
pure_text
unknown
```

La classificazione usa pattern testuali conservativi.

Pattern indicativi per `clipping`:

- `foglio`
- `Superficie`
- `Tiratura`
- `Diffusione`
- `Lettori`
- `Dir. Resp.`
- `Quotidiano`

Pattern indicativi per `web`:

- `http://`
- `https://`
- `newsletter`
- `Condividi`
- `Twitta`
- `ultimi articoli`
- `articoli correlati`

### 6. `visualization`

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

## Convenzione bounding box

La convenzione interna usata nei `PageRecord` è:

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

Nel caso osservato, Docling usa:

```json
{
  "l": 15.75,
  "t": 810.36,
  "r": 90.39,
  "b": 791.55,
  "coord_origin": "BOTTOMLEFT"
}
```

Il `PageNormalizer` converte queste coordinate nella convenzione interna usando l'altezza della pagina PDF.

## Output generato

Gli output generati sono organizzati fuori da `src`.

Struttura consigliata:

```text
data/
└── nome_documento/
    ├── raw/
    │   └── docling.json
    ├── pages/
    │   ├── page_001.json
    │   ├── page_002.json
    │   └── ...
    └── debug/
        ├── page_001_bbox.png
        ├── page_002_bbox.png
        └── ...
```

## Esempio di `PageRecord`

```json
{
  "document_id": "Evidenze Rassegna 17012025.pdf",
  "pdf_page": 2,
  "page_type": "clipping",
  "source": {
    "name": null,
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
      "type": "header_metadata",
      "text": "da pag.  8 / 17-GEN-2025 foglio 1",
      "bbox": [
        15.75,
        31.63,
        90.39,
        50.44
      ],
      "raw_label": "page_header",
      "provenance": [
        {
          "self_ref": "#/texts/10",
          "collection": "texts",
          "docling_label": "page_header",
          "charspan": [
            0,
            33
          ],
          "raw_bbox": {
            "l": 15.75,
            "t": 810.36,
            "r": 90.39,
            "b": 791.55,
            "coord_origin": "BOTTOMLEFT"
          }
        }
      ]
    }
  ]
}
```

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

## Comandi utili

### Verifica import dei modelli

```bash
pixi run python -c "from press_reputation.models.page import PageRecord; print(PageRecord(document_id='test.pdf', pdf_page=1).model_dump())"
```

### Parsing raw con Docling

```bash
pixi run python -c "from pathlib import Path; from press_reputation.parsers.docling_parser import DoclingParser; p=DoclingParser(); d=p.extract(Path('Data/2025/Gennaio/Evidenze Rassegna 17012025.pdf')); p.save_raw_json(d, Path('data/test_doc/raw/docling.json')); print('ok')"
```

### Normalizzazione in `PageRecord`

```bash
pixi run python -c "import json; from pathlib import Path; from press_reputation.normalization import PageNormalizer; doc=json.loads(Path('data/test_doc/raw/docling.json').read_text()); pages=PageNormalizer().normalize(doc, 'Evidenze Rassegna 17012025.pdf'); print(pages[0].model_dump_json(indent=2))"
```

### Salvataggio JSON per pagina

```bash
pixi run python -c "import json; from pathlib import Path; from press_reputation.normalization import PageNormalizer; raw=Path('data/test_doc/raw/docling.json'); doc=json.loads(raw.read_text()); pages=PageNormalizer().normalize(doc, 'Evidenze Rassegna 17012025.pdf'); out=Path('data/test_doc/pages'); out.mkdir(parents=True, exist_ok=True); [Path(out / f'page_{p.pdf_page:03d}.json').write_text(p.model_dump_json(indent=2), encoding='utf-8') for p in pages]; print('ok')"
```

### Generazione immagini debug con bounding box

```bash
pixi run python -c "import json; from pathlib import Path; from press_reputation.normalization import PageNormalizer; from press_reputation.visualization import render_page_bboxes; pdf=Path('Data/2025/Gennaio/Evidenze Rassegna 17012025.pdf'); doc=json.loads(Path('data/test_doc/raw/docling.json').read_text()); pages=PageNormalizer().normalize(doc, pdf.name); out=Path('data/test_doc/debug'); [render_page_bboxes(pdf, p, out / f'page_{p.pdf_page:03d}_bbox.png') for p in pages]; print('ok')"
```

## Stato attuale

Implementato:

- modelli dati Pydantic
- astrazione parser
- parser Docling
- esportazione raw JSON Docling
- normalizzazione in `PageRecord`
- estrazione metadata deterministica
- classificazione pagina deterministica
- visualizzazione bounding box su PNG

Non implementato:

- CLI completa
- test automatici completi
- ricostruzione articoli multipagina
- analisi semantica
- sentiment analysis
- reputation scoring

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