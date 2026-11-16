# Euristiche e score della pipeline

Questo documento descrive **come vengono calcolati gli score** in ogni fase della pipeline e quali soglie li governano. Tutti i valori predefiniti vivono in `core/config.py` (e in `classification/web/web_content_config.py` per il web); sono sovrascrivibili via Pydantic.

Convenzione: gli score sono normalizzati in `[0, 1]` salvo diversa indicazione. Una decisione è quasi sempre della forma `score >= soglia AND margine >= margine_minimo`, dove il margine è `score_vincitore - score_secondo`. Ogni decisione registrata conserva score, metodo, evidenze e alternative nei `metadata` della regione o nei JSON di flow.

---

## 1. Profiling e OCR selettivo (`ingestion/profiling/`, `ingestion/parsers/`)

`DocumentProfiler` misura per pagina: parole di testo nativo, copertura immagini, posizione di header/footer.

| Soglia (`DocumentProfilingConfig`) | Valore | Significato |
|---|---|---|
| `min_native_body_words` | 30 | sotto questa soglia il testo nativo è "rado" |
| `min_image_coverage_without_text` | 0.05 | pagina senza testo: OCR se le immagini coprono ≥ 5% |
| `min_image_coverage_with_sparse_text` | 0.05 | testo rado: OCR se immagini ≥ 5% |
| `aggressive_ocr_image_coverage` | 0.12 | OCR aggressivo se immagini ≥ 12% |
| `aggressive_ocr_body_word_limit` | 120 | ...e testo body < 120 parole |
| `header_fraction` / `footer_fraction` | 0.16 / 0.10 | fasce alta/bassa della pagina |
| `ocr_dpi` | 200 | risoluzione del rendering per OCR |
| `duplicate_bbox_overlap` | 0.60 | due regioni con overlap ≥ 60% sono duplicate |
| `duplicate_text_similarity` | 0.85 | ...e similarità testuale ≥ 0,85 → merge in `merge_extraction` |

## 2. Feature e geometria (`features/`)

Funzioni base in `features/geometry.py`:

- `valid_box(bbox)`: bbox non nullo, 4 coordinate finite, `x1 > x0`, `y1 > y0`;
- `horizontal_overlap(a, b)`: frazione della larghezza del più stretto coperta dall'altro;
- `vertical_gap(a, b)`: distanza verticale (0 se si sovrappongono);
- `Region.has_valid_bbox`: property equivalente per una regione.

`RegionFeatureExtractor` (`features/region_features.py`) calcola per regione: `word_count`, `text_length`, `has_url`, `has_foglio`, `has_tiratura`, `has_diffusione`, marker di navigazione/related/watermark/rights, `bold_ratio`, entity lookup (opzionale).

## 3. Classificazione tecnica e boilerplate (`classification/`)

### Watermark (`technical_region_classifier.py`, `WatermarkDetectionConfig`)

Score a componenti, soglia `min_score = 0.65`:

| Componente | Peso/condizione |
|---|---|
| bassa opacità | se `opacity < low_opacity_threshold` (0.55) |
| ripetizione cross-pagina | stessa posizione relativa su ≥ `repeated_page_fraction` (0.50) delle pagine (min 2 pagine) |
| posizione ai bordi | entro `edge_fraction_x` (0.06) / `edge_fraction_y` (0.10) della pagina |
| marker forti/deboli | testo breve con marker di registro: ≤ 120 char (forte), ≤ 200 char (debole) |

La sola bassa opacità **non** basta. Nota: `minimum_editorial_overlap = 2` è un bug noto (l'overlap varia 0–1, il componente non si attiva mai).

### Boilerplate (`boilerplate_detector.py`, `DocumentBoilerplateConfig`)

Una regione è boilerplate se appare su ≥ `min_distinct_pages` (2) pagine con frequenza ≥ `min_page_frequency` (0.50), posizione relativa simile (`max_relative_position_delta` 0.045, `max_relative_size_delta` 0.06), font simile (±20%), testo breve (8–180 char, ≤ 12 parole). La ripetizione annota il pattern ma **non** esclude automaticamente dal body: lo decidono i classificatori successivi.

### Pagina (`page_classifier.py`)

`PageType` decisa per conteggio di marker strutturali **solo su regioni non editoriali**:

- `INDEX`: pagina con righe di indice riconosciute;
- `CLIPPING`: ≥ 1 marker di clipping (foglio `n/m`, tiratura, diffusione...) + header metadata o ≥ 2 marker;
- `WEB`: ≥ 2 segnali web (URL, marker web) oppure URL presente senza marker clipping;
- `UNKNOWN`: default.

## 4. Entity lookup (`core/lookup.py`, `EntityLookupConfig`)

`resolve_entity()` valuta in cascata: exact source → alias → fuzzy source (soglia 0.88) → exact provider → alias → fuzzy provider (0.88) → exact location → fuzzy location (0.95/0.90/0.85 per lunghezza 5/8/>8 char). Il vincitore fuzzy deve battere il secondo per ≥ `min_fuzzy_winner_margin` (0.04). Per le location, `location_min_match_coverage` (0.60) impedisce che «Napoli» copra l'intera regione «CRONACHE DI NAPOLI».

`text_similarity(a, b)`: `SequenceMatcher(None, a, b).ratio()`, 0.0 se una stringa è vuota. `word_set_similarity`: Jaccard sui token.

## 5. Review Index (`review_index/`)

### Parsing (`ReviewIndexConfig`)

- `min_valid_rows = 2`: servono almeno 2 righe valide (la copertina da sola vale come prova di formato).
- Limiti di sanità: `START_PAGE_MAX = 60`, `ORIG_PAGE_MAX = 120`.

### Matching (`matcher.py`)

`title_similarity(left, right)` = `0.65 * char_score + 0.35 * token_score` su titoli normalizzati (NFKD, minuscole, senza punteggiatura), con fallback su confronto **compattato** (senza spazi) se entrambi ≥ 10 char — robusto alle fusioni OCR.

Score di match entry↔titolo: similarità titolo (max 0.60) + bonus per data/fonte/pagina originale uguali. Soglie: `min_title_similarity` 0.68, `strong_title_similarity` 0.84, `min_match_score` 0.72, `min_score_margin` 0.10. **Override a evidenza forte:** se fonte + data + pagina originale coincidono tutte, il match si accetta con similarità ≥ `strong_evidence_min_similarity` (0.50).

## 6. Titolo, sottotitolo, autore (`enrichment/`)

### Titolo (`title_resolver.py`, `TitleResolutionConfig`)

`score(region, page, entries)` somma componenti pesati:

| Componente | Peso | Condizione |
|---|---|---|
| `docling_label` | 0.12 | label Docling compatible con titolo |
| `relative_width` | 0.08 | larghezza ≥ 25% della pagina |
| `position` | 0.08 | `y0 / page_height ≤ 0.55` (parte alta) |
| `body_proximity` | 0.13 | gap dal body ≤ `max_body_gap_relative` (0.12) della pagina |
| `relative_font_size` | fino a 0.22 | prominenza font: `(ratio-1)/(1.45-1)` per ratio ≥ 1 |
| `bold` | 0.12 × `bold_ratio` | con evidenza stile ≥ 0.50 |
| `near_author_or_subtitle` | 0.10 | autore/sottotitolo vicino |
| `review_index_title` | 0.30 × similarità | miglior entry indice con similarità ≥ 0.68 |

Accettazione: `score ≥ min_score` (0.38) e margine sul secondo ≥ `min_winning_margin` (0.08). Veti assoluti: timbri di registro (`is_stamp_code_text`) e frasi chrome web → degradati, mai titoli.

### Sottotitolo (`subtitle_resolver.py`, `SubtitleResolutionConfig`)

Candidati sopra/sotto il titolo entro `max_gap_above` (60 pt) / `max_gap_below` (80 pt), overlap orizzontale ≥ 0.25, font ≤ 1.75× il body. Accettazione: `score ≥ min_score` (0.55), o ≥ 0.70 senza anchor body; margine ≥ 0.08; max 2 sottotitoli per titolo. Veti: breadcrumb («Home > …»), chrome web (glifi, contatori, date, byline tecniche).

### Autore (`author_resolver.py`, `AuthorResolutionConfig`)

Byline esplicite («di …», «a cura di …»): `explicit_min_score` 0.70. Nomi impliciti: 2–5 token, non in lista negativa, non heading editoriale, non source/location: `implicit_min_score` 0.72. Bonus 0.25 se l'entry indice del titolo indica un autore con similarità ≥ 0.92. Gap dall'header ≤ 110 pt, overlap ≥ 0.20.

### Header editoriale (`editorial_header_arbitrator.py`, `EditorialHeaderConfig`)

Regola **banner + deck**: titolo che copre ≥ 55% della larghezza pagina con un titolo più stretto immediatamente sotto (gap ≤ 55 pt, overlap ≥ 0.35) → il largo è la headline, lo stretto è il deck. Margine di score ≥ 0.12 per scegliere il main title in uno stack.

## 7. Web main content e intrusion (`classification/web/`)

`WebMainContentResolver` assegna a ogni regione uno `content_scope` (`main`, `related`, `advertisement`, `navigation`, `non_main`, `unknown`) con `content_scope_reason`. I moduli related sono delimitati localmente (non propagano al body sottostante). `InlineIntrusionDetector` trova intrusioni fra due body della stessa area combinando marker, formattazione link, lunghezza, differenza di stile, gap e discontinuità lessicale.

## 8. Clustering e assegnazione body (`reconstruction/clustering/`)

### `score_body` (`clustering_core.py`, `ArticleClusteringConfig`)

Score del candidato body verso un titolo:

| Componente | Peso |
|---|---|
| overlap orizzontale | 0.25 × overlap |
| allineamento colonna | 0.24 × score colonna |
| stesso article_id / stessa lane web | +0.16 / +0.18 |
| gap verticale | +0.12 se entro `max_local_gap_fraction` (0.12) |
| larghezza simile | +0.08 |
| stile simile | +0.06 |
| +0.04 | bonus aggiuntivo (provenance/contesto) |

Veti: geometria mancante, title barrier, colonna diversa, distanza eccessiva, `web_candidate_id` diverso → score 0 con reason. Accettazione: `score ≥ min_assignment_score` (0.62) e margine ≥ `min_winning_margin` (0.12).

### Colonne e multicolonna (`clustering_columns.py`)

- `adjacent_column_score`: base 0.28 per adiacenza + 0.04×width_quality + 0.32 (stessa fonte) + 0.12 (stile compatibile) + 0.15 (no titolo concorrente) + lexical_score + 0.10 + 0.06.
- `bootstrap_group_score`: `header_coverage` 0.38 + `adjacent_columns` 0.18 + altri segnali; soglie `min_bootstrap_group_score` 0.72, margine 0.12.
- Soglie colonne: tolleranza bordo `column_left_tolerance` 18 pt, overlap intra-colonna ≥ 0.55, gap colonne adiacenti ≤ 0.07 della pagina, differenza larghezza ≤ 0.12.

### Fusione headline duplicate

`_same_headline`: similarità ≥ 0.90 dopo normalizzazione e rimozione suffisso sito, overlap di colonna ≥ 0.25 → stessa identità (`duplicate_headline_v1`).

## 9. Continuazione body (`reconstruction/continuation/body_continuation.py`)

Score di un frammento UNKNOWN verso un riferimento con `article_id`:

| Componente | Peso |
|---|---|
| `same_extraction_pass_page_and_item` | 0.50 |
| `consecutive_effective_charspans` | 0.25 |
| `adjacent_column` | 0.15 |
| `same_column` | 0.17 × allineamento |
| `horizontal_overlap` | 0.14 × overlap (min 0.45) |
| `forward_proximity` | 0.22 × `1 - gap/max_forward_gap` (max 120 pt) |
| `width_similarity` | 0.11 |
| `dominant_font` / `font_size` | 0.10 + 0.10 |
| `bold` | 0.03 |

Accettazione: `score ≥ min_score` (0.70), margine ≥ 0.12. Continua a convergenza aggiungendo i frammenti promossi ai riferimenti.

## 10. Continuazione cross-pagina

### Cartacea (`newspaper_continuation.py`, `NewspaperContinuationConfig`)

`1/2 → 2/2` è prova forte. Candidato: `score ≥ min_candidate_score` (0.50); accettato: `score ≥ min_accept_score` (0.74). Titolo ripetuto con similarità ≥ 0.84 o match indice ≥ 0.84. Gap pagine PDF ≤ 1.

### Web (`web_article_continuation.py`)

Score a evidenze: stesso URL, fonte, data, colonna compatibile, body che inizia presto, continuità sintattica/lessicale, stesso autore, **fogli WEB sequenziali** (footer «WEB n» → «n+1»). Accettazione in `article_flow.py`: `score ≥ min_web_accept_score` (0.82), con bonus +0.12 per boundary forte.

### General continuation (`general_continuation.py`)

Second-chance multi-segnale, base 0.15:

| Segnale | Peso |
|---|---|
| stesso URL canonico articolo | +0.36 |
| stessa fonte | +0.17 |
| stessa data di pubblicazione | +0.09 |
| entrambe web | +0.11 |
| stesso wrapper di stampa | +0.29 |
| stesso pubblico di stampa | +0.13 |
| sequenza fogli clipping | +0.38 |
| title-only web anchor | +0.05 |
| lane body compatibile | +0.08 |

Score cappato a 1.0; i segnali indipendenti sono conteggiati separatamente.

## 11. Membership globale (`reconstruction/membership/`)

`GlobalArticleMembershipResolver`: `MIN_SCORE` 0.68, `MIN_MARGIN` 0.14 sul vincitore. `OrphanProximityFallbackResolver`: un solo titolo candidato sopra il frammento, overlap ≥ `min_horizontal_overlap`, assegna con score fisso 0.55 e metodo `proximity_fallback_v1` (sempre in review). `EditorialFragmentRefinementResolver`: frammenti racchiusi da body dello stesso proprietario o in colonna interpolata adiacente (≤ 18 pt) a colonna posseduta, senza concorrenti.

## 12. Reading order e completezza (`reconstruction/assembly/`)

`ArticleReadingOrderResolver`: colonne con tolleranza 18 pt, overlap ≥ 0.55; regioni spanning (overlap ≥ 0.35 con più colonne) fungono da separatori di banda; gap verticale sospetto > 120 pt segnalato (non scarta testo). `ExtractionCompletenessConfig` (calibrata sul corpus):

| Metrica | Soglia `ready` |
|---|---|
| coverage body assegnato | ≥ 0.90 |
| retention testo OCR | ≥ 0.94 (0.92 se body interamente OCR) |
| char non assegnati | ≤ 150 (o ≤ 10% dei char assegnati) |
| char non mappati | ≤ 100 |
| char editoriali irrisolti | ≤ 200 |
| similarità titolo duplicato | ≥ 0.82 |

## 13. Finalizzazione (`article_finalizer.py`, `ArticleFinalizationConfig`)

`ready` richiede: titolo, body non vuoto, source e data presenti (`require_source_for_ready`, `require_date_for_ready`), `reconstruction_confidence ≥ 0.65`, completezza senza squalifiche, nessun link candidato/contraddizione. Record untitled (recupero body orfani): sempre `review_required`, min 200 char di body. Rilevamento duplicati: overlap ≥ 0.40 con shingle di 8 parole. Tail attachment: frammento orfano in minuscola entro 2 pagine da un record con body non concluso.

`reconstruction_confidence` è una proxy euristica (reading order + clustering, penalità per link candidati), **non** una probabilità calibrata. `extraction_confidence` resta `null` senza score misurati.
