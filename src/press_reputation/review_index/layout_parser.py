"""Recover press-review index rows from PageRecord text boxes when Docling
has not exposed a document_index table. Designed for *explicit* cover indexes.
"""
from __future__ import annotations

import re
from datetime import datetime
from press_reputation.models.page import PageRecord
from press_reputation.review_index.models import ReviewIndexEntry


class LayoutReviewIndexParser:
    DATE_SOURCE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+?)\s*$")
    PAGE_TITLE = re.compile(r"^\s*(\d{1,4})\s+(.{8,})\s*$")
    COVER = re.compile(r"\brassegna\s+del\s+\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)

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
                if start < 1 or original_page < 1:
                    continue
                title = title_m.group(2).strip()
                row_records.append((left, middle[0], right[0], d, source, original_page, title, start))
            # Require >=2 rows: do not reclassify a normal article by accident.
            if len(row_records) < 2:
                continue
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