"""Recover press-review index rows from PageRecord text boxes when Docling
has not exposed a document_index table. Designed for *explicit* cover indexes.
"""
from __future__ import annotations

import re
from datetime import datetime
from press_reputation.models.page import PageRecord, PageType
from press_reputation.review_index.models import ReviewIndexEntry


class LayoutReviewIndexParser:
    DATE_SOURCE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+?)\s*$")
    PAGE_TITLE = re.compile(r"^\s*(\d{1,4})\s+(.{8,})\s*$")
    COVER = re.compile(r"\brassegna\s+del\s+\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
    # Etichette di sezione stampate sugli indici: non sono titoli di articoli.
    SECTION_LABELS = {"stampa locale", "web", "web locale", "stampa nazionale"}
    # Riga intera fusa in una regione: data fonte pagina titolo ... start.
    # I puntini di rimando possono essere letti male dall'OCR ("ab", "ao") o
    # mancare del tutto: il separatore finale e' opzionale e la pagina di
    # inizio resta vincolata dai limiti di sanita' (START/ORG_PAGE_MAX).
    FUSED_ROW = re.compile(
        r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+?)\s+(\d{1,4})\s+(.+?)\s*(?:\.{2,3}|[a-zà-ù]{1,4})?\s*(\d{1,3})\s*$"
    )
    # La rassegna non supera queste dimensioni: interpretazioni di riga con
    # numeri fuori scala (es. titolo che finisce con un numero) vengono scartate.
    START_PAGE_MAX = 60
    ORIG_PAGE_MAX = 120
    # Etichette corte centrate sopra le righe dell'indice.
    LABEL_MAX_CHARS = 24

    @staticmethod
    def _date(raw: str):
        for fmt in ("%d/%m/%y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
        return None

    def parse(self, pages: list[PageRecord], *, document_id: str) -> list[ReviewIndexEntry]:
        entries: list[ReviewIndexEntry] = []
        for page in sorted(pages, key=lambda p: p.pdf_page)[:3]:
            text = " ".join(r.text or "" for r in page.regions)
            if not self.COVER.search(text):
                continue
            # Rows have a left date + source, a central page + title,
            # and a rightmost destination PDF page number.
            candidates = [r for r in page.regions if r.bbox and (r.text or "").strip()]
            lefts = [r for r in candidates if self.DATE_SOURCE.match((r.text or "").strip())]
            row_records = []
            for left in lefts:
                m = self.DATE_SOURCE.match((left.text or "").strip())
                d = self._date(m.group(1)) if m else None
                source = m.group(2).strip() if m else None
                if not (d and source):
                    continue
                middle = [r for r in candidates
                            if r is not left and r.bbox[0] > left.bbox[0] + 65
                                and r.bbox[0] < (page.page_width or 595) * 0.78
                                and abs((r.bbox[1]+r.bbox[3])/2 - (left.bbox[1]+left.bbox[3])/2) <= 9
                                and self.PAGE_TITLE.match((r.text or "").strip())]
                if len(middle) != 1:
                    continue
                title_m = self.PAGE_TITLE.match(middle[0].text.strip())
                right = [r for r in candidates
                            if r not in (left,middle[0])
                                and r.bbox[0] >= (page.page_width or 595) * .84
                                and abs((r.bbox[1]+r.bbox[3])/2 - (left.bbox[1]+left.bbox[3])/2) <= 9
                                and (r.text or "").strip().isdecimal()]
                if len(right) != 1:
                    continue
                start = int(right[0].text.strip())
                original_page = int(title_m.group(1))
                if not (1 <= start <= self.START_PAGE_MAX and 1 <= original_page <= self.ORIG_PAGE_MAX):
                    continue
                title = title_m.group(2).strip()
                row_records.append((left, middle[0], right[0], d, source, original_page, title, start))
            # Fallback: rassegne in cui l'intera riga d'indice finisce in
            # UNA sola regione (colonne fuse dall'estrazione). Formato:
            # data fonte pagina titolo [rimando] pagina-inizio.
            if not row_records:
                for region in candidates:
                    m = self.FUSED_ROW.match((region.text or "").strip())
                    if not m:
                        continue
                    d = self._date(m.group(1))
                    if not d:
                        continue
                    start = int(m.group(5))
                    original_page = int(m.group(3))
                    if not (1 <= start <= self.START_PAGE_MAX and 1 <= original_page <= self.ORIG_PAGE_MAX):
                        continue
                    title = m.group(4).strip()
                    if len(title) < 8:
                        continue
                    row_records.append((region, region, region, d, m.group(2).strip(),
                                        original_page, title, start))
            # La copertina ("Rassegna del GG/MM/AAAA") e' gia' una prova
            # forte: anche una sola riga ben formata (3 colonne + pagina di
            # inizio decimale) e' un indice legittimo. Le rassegne con un
            # unico clipping producevano altrimenti zero voci.
            if not row_records:
                continue

            # Gatekeeping della pagina indice: le righe e le etichette di
            # sezione ("STAMPA LOCALE") non sono testo editoriale e non
            # devono finire negli articoli (intero indice diventava un
            # articolo-titolo-etichetta con le righe come body).
            row_regions = {id(left) for left, _, _, *_ in row_records}
            for region in page.regions:
                if id(region) in row_regions:
                    region.exclude_from_article_text = True
                    region.metadata["index_row"] = True
                label = (region.text or "").strip().casefold()
                if (len(label) <= self.LABEL_MAX_CHARS
                        and label in self.SECTION_LABELS):
                    region.exclude_from_article_text = True
                    region.metadata["index_section_label"] = True
            page.page_type = PageType.INDEX

            for i, (left, middle, right, d, source, original_page, title, start) in enumerate(row_records):
                entries.append(ReviewIndexEntry(
                    id=f"{document_id}:layout_index:p{page.pdf_page}:row{i}",
                    document_id=document_id,
                    publication_date=d, source=source, original_page=original_page,
                    title=title, author=None, review_start_page=start,
                    category=None, index_pdf_page=page.pdf_page,
                    table_ref=f"layout_index:page={page.pdf_page}", row_number=i,
                    provenance=[{"page": page.pdf_page, "text": r.text, "bbox": r.bbox,
                                    "role": role} for role,r in
                                (("source",left),("title",middle),("start",right))],
                    warnings=["parsed_from_page_regions_not_docling_table"],
                ))
            break
        return entries