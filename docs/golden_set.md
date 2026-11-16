# Golden set pubblico per la valutazione

`scripts/build_golden_set.py` costruisce un golden set da **fonti pubbliche e aperte**,
progettato per le due anime della valutazione del progetto:

1. **tagging dei risultati (OCR/PDF) e ricostruzione dell'articolo**: PDF sintetici in
   formato "rassegna stampa" (pagina indice + clipping + watermark/codice di registro,
   con pagine rasterizzate opzionali per forzare il percorso OCR) con ground truth per
   regione (`gt/regions.json`: pagina, bbox, ruolo, testo) e per articolo
   (`gt/articles.json`: titolo, sottotitolo, autori, data, testata, pagine, body atteso).
2. **ingestione in knowledge graph + entity/event resolution**: annotazioni di entità ed
   eventi con riferimenti canonici (`gt/entities.json`, `gt/events.json`) dal corpus
   NewsReader MEANTIME (sezione italiana WItaC), più un gazetteer CC0 da Wikidata
   (`entities/gazetteer.json`) thought per estendere `src/press_reputation/resources/`.

La qualità intrinseca dell'OCR non è l'obiettivo: le pagine rasterizzate servono solo a
garantire che il *tagging* delle regioni funzioni anche quando il testo arriva da OCR.

## Sorgenti e licenze

| Sorgente | Uso | Licenza | Come viene usato |
|---|---|---|---|
| [NewsReader MEANTIME / WItaC](http://www.newsreader-project.eu/results/data/wikinews/) | articoli + entità/eventi con coreferenza (NAF) | CC BY | caso `meantime_it` con `entities.json`/`events.json` |
| [ARTeLab/ilpost](https://huggingface.co/datasets/ARTeLab/ilpost) (HF datasets-server) | articoli de Il Post | CC BY 4.0 | PDF sintetici `rassegna_*` |
| [ARTeLab/fanpage](https://huggingface.co/datasets/ARTeLab/fanpage) (HF datasets-server) | articoli Fanpage.it | CC BY 4.0 | PDF sintetici `rassegna_*` |
| [Wikidata](https://query.wikidata.org/) SPARQL + `wbsearchentities` | gazetteer testate italiane (nome, alias, QID) | CC0 1.0 | `entities/gazetteer.json` |
| [Internet Archive — italian newspapers](https://archive.org/details/italian-newspapers) | scans PD + `_djvu.xml` (bbox per parola) | public domain | `scans/*.json` (opzionale, `--sources scans`) |
| [GDELT 2.0](https://gdeltproject.org/data.html) | eventi Italia (`ActionGeo_CountryCode=IT`) con `SOURCEURL` | aperto, citazione obbligatoria | `events_gdelt_YYYYMMDD.json` (opzionale, `--gdelt-date`) |

Non usare corpus scraped da testate commerciali come golden set redistribuibile: i contenuti
restano protetti. Per il linking web bastano gli URL sorgente (come fa GDELT).

## Uso

```bash
# costruisce golden_set/ (richiede rete; PyMuPDF per i PDF sintetici)
PYTHONPATH=src pixi run python scripts/build_golden_set.py \
    --sources meantime wikidata ilpost --limit 60 --articles-per-pdf 6 --out golden_set

# fuerza il percorso OCR su ogni 5a pagina dei PDF sintetici
PYTHONPATH=src pixi run python scripts/build_golden_set.py --ocr-every 5 --out golden_set

# solo ground truth JSON, senza sintesi PDF (utile in CI/offline)
PYTHONPATH=src pixi run python scripts/build_golden_set.py --no-render --out golden_set

# valida uno golden set esistente
PYTHONPATH=src pixi run python scripts/build_golden_set.py --validate-only --out golden_set

# valuta la pipeline sul golden set
PYTHONPATH=src pixi run python scripts/evaluate_golden.py \
    --golden-root golden_set --output golden_eval_report.json
```

## Struttura dello golden set

```text
golden_set/
  manifest.json                 # casi, sorgenti, licenze, attese (pagine/articoli)
  raw/                          # download originali (gitignored)
  entities/gazetteer.json       # testate da Wikidata: {nome, aliases, qid, ...} (CC0)
  events_gdelt_YYYYMMDD.json    # eventi Italia da GDELT (opzionale)
  scans/<id>.json               # djvu.xml words con bbox + pdf_url (opzionale)
  cases/<case_id>/
    input.pdf                   # PDF sintetico tipo rassegna stampa
    audit_template.csv          # stesse colonne di scripts/evaluate_audit.py
    gt/articles.json            # articoli attesi (schema ArticleRecord subset)
    gt/regions.json             # regioni attese: {page, bbox, role, text}
    gt/entities.json            # menzioni + ref canonici (solo caso meantime)
    gt/events.json              # eventi + menzioni + ref (solo caso meantime)
```

Ruoli regione (`gt/regions.json`): `header`, `index_row`, `title`, `subtitle`, `byline`,
`body`, `continuation`, `watermark`, `rights_notice`. Il mapping verso i `RegionType` della
pipeline è `ROLE_TO_REGION_TYPE` in `scripts/build_golden_set.py` (`index_row` deve restare
fuori dal testo degli articoli).

## Collegamento agli strumenti esistenti

- **Audit manuale**: ogni caso ha `audit_template.csv` con le stesse colonne di
  `batch_parse.py` (`ARTICLE_COLUMNS + AUDIT_FIELDS`); si valuta con
  `scripts/evaluate_audit.py --audit ... --output ...`.
- **Regressione corpus**: i casi `cases/*/input.pdf` possono essere aggiunti a
  `tests/corpus_manifest.json` (via `tests/regenerate_corpus_baselines.py --extra`) per
  komplettezza/conteggi automatici; il golden set aggiunge invece l'accuracy sui campi.
- **Entity lookup**: `entities/gazetteer.json` segue lo schema di
  `src/press_reputation/resources/newspaper.json` (`nome`, `aliases`, `fonte_dataset`);
  può essere fuso lì per arricchire `lookup.resolve_entity()`.
- **KG**: `gt/entities.json`/`gt/events.json` sono la GT per entity linking (menzione →
  QID/DBpedia) ed event resolution (coreferenza cross-documento); `evaluate_golden.py`
  riporta già `lookup_hit_rate`, `canonical_accuracy` e `gazetteer_hit_rate`.

## Metriche di `evaluate_golden.py`

- `article_recall`: articoli GT matchati a un `ArticleRecord` per similarità titolo ≥ 0.5.
- `title_exact_rate`, `date_match_rate`, `subtitle_match_rate`, `body_f1_mean` sui matchati.
- `region_iou_match_rate` e `region_tagging[role].role_accuracy`: matching bbox IoU ≥ 0.3
  tra regioni GT e regioni della pipeline sulla stessa pagina.
- `kg.lookup_hit_rate` / `canonical_accuracy` / `gazetteer_hit_rate` sulle menzioni GT.
