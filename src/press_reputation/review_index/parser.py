import re
from datetime import datetime
from typing import Any

from press_reputation.config import ReviewIndexConfig
from press_reputation.review_index.models import ReviewIndexEntry

class ReviewIndexParser:
    ORIGINAL_PAGE_AND_TITLE = re.compile(r"^\s*(\d{1,4})\s+(.+?)\s*$")
    
    def __init__(self, config: ReviewIndexConfig | None = None) -> None:
        self.config = config or ReviewIndexConfig()
        
    def parse(self, document: Any, document_id: str) -> list[ReviewIndexEntry]:
        raw = self._to_dict(document)
        entries: list[ReviewIndexEntry] = []
        
        for table in raw.get("tables", []):
            provenances = table.get("prov") or []
            if not provenances:
                continue
            
            index_page = provenances[0].get("page_no")
            if (not isinstance(index_page, int) or index_page > self.config.max_initial_pages):
                continue
            
            data = table.get("data") or {}
            grid = data.get("grid") or []
            if table.get("label") != "document_index":
                continue
            
            if data.get("num_cols") != 5:
                continue
            
            parsed_rows = []
            for row_number, row in enumerate(grid):
                if len(row) != 5:
                    continue
                
                texts = [(cell.get("text") or "").strip() if isinstance(cell, dict) else "" for cell in row]
                
                parsed = self._parse_row(texts)
                if parsed is not None:
                    parsed_rows.append((row_number, row, parsed))
                    
            if len(parsed_rows) < self.config.min_valid_rows:
                continue
            
            table_ref = table.get("self_ref") or "#/tables/unknown"
            
            for row_number, row, values in parsed_rows:
                entries.append(ReviewIndexEntry(
                    id=(f"{document_id}:index:{table_ref}:row={row_number}"),
                    document_id = document_id,
                    index_pdf_page=index_page,
                    table_ref=table_ref,
                    row_number=row_number,
                    **values,
                    provenance=[
                        {
                            "page": index_page,
                            "table_ref": table_ref,
                            "row_number": row_number,
                            "column": column,
                            "text": cell.get("text"),
                            "bbox": cell.get("bbox"),
                        }
                        for column, cell in enumerate(row) if isinstance(cell, dict)
                    ]
                ))
                
        return entries

    def _parse_row(self, cells: list[str]) -> dict | None:
        raw_date, source, page_and_title, author, raw_start = cells
        
        publication_date = self._date(raw_date)
        match = self.ORIGINAL_PAGE_AND_TITLE.match(page_and_title)
        
        if not publication_date or not source or not match:
            return None
        
        start = self._positive_int(raw_start)
        if start is None:
            return None
        
        title = match.group(2).strip()
        if len(title) < 8:
            return None
        
        return {
            "publication_date": publication_date,
            "source": source,
            "original_page": int(match.group(1)),
            "title": title,
            "author": author or None,
            "review_start_page": start,
            "category": None,
        }
        
    @staticmethod
    def _date(text: str):
        for pattern in ("%d/%m/%y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                pass
        return None
    
    @staticmethod
    def _positive_int(text: str) -> int | None:
        value = text.strip()
        return int(value) if value.isdecimal() and int(value) > 0 else None
    
    @staticmethod
    def _to_dict(document: Any) -> dict:
        if isinstance(document, dict):
            return document
        if hasattr(document, "export_to_dict"):
            return document.export_to_dict()
        return document.model_dump(
            mode = "json", by_alias=True, exclude_none=True
        )