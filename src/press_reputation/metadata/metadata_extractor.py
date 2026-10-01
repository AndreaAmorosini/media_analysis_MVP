import re
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from press_reputation.models.page import (ClippingInfo, PageRecord, SourceType, RegionType)

ITALIAN_MONTHS = {
    "GEN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAG": 5,
    "GIU": 6,
    "LUG": 7,
    "AGO": 8,
    "SET": 9,
    "OTT": 10,
    "NOV": 11,
    "DIC": 12,
}

class MetadataExtractor:
    # Estrae metadata dal PageRecord in maniera deterministica
    #TODO: Da rivedere
    
    def enrich(self, page: PageRecord) -> PageRecord:
        text = self.metadata_text(page)

        # if not text:
        #     text = self.header_zone_text(page)

        publication_date = self.extract_date(text)
        original_page = self.extract_original_page(text)
        sheet_info = self.extract_sheet_info(text)
        surface_percent = self.extract_surface_percent(text)
        url = self.extract_url(self.url_candidate_text(page))
        section = self.extract_section(page)
        source_name = self.extract_source_name(page)

        if publication_date:
            page.source.publication_date = publication_date

        if original_page:
            page.source.original_page = original_page

        if url:
            page.source.url = url
            page.source.type = SourceType.WEB
            
            if page.source.name is None:
                page.source.name = self.source_name_from_url(url)

        if section:
            page.section = section

        if source_name and page.source.name is None:
            page.source.name = source_name
            if page.source.type == SourceType.UNKNOWN:
                page.source.type = SourceType.NEWSPAPER

        if sheet_info or surface_percent is not None:
            if page.clipping is None:
                page.clipping = ClippingInfo()

            if sheet_info:
                page.clipping.sheet_current = sheet_info[0]
                page.clipping.sheet_total = sheet_info[1]

            if surface_percent is not None:
                page.clipping.surface_percent = surface_percent

            if page.source.type == SourceType.UNKNOWN:
                page.source.type = SourceType.NEWSPAPER

        return page
    
    @staticmethod
    def metadata_text(page: PageRecord) -> str:
        allowed = {
            RegionType.HEADER_METADATA,
            RegionType.SOURCE_NAME,
            RegionType.PRESS_REVIEW_PROVIDER,
            RegionType.PUBLICATION_DATE,
            RegionType.ORIGINAL_PAGE,
            RegionType.CLIPPING_SHEET,
        }

        return "\n".join(region.text.strip() for region in page.regions if (region.type in allowed and
                                                                            region.text and
                                                                            not region.metadata.get("inside_article_position_thumbnail")))
    @staticmethod
    def url_candidate_text(page: PageRecord) -> str:
        candidates = []
        
        for region in page.regions:
            if not region.text or len(region.text) > 250:
                continue
            
            if region.type in {RegionType.HEADER_METADATA, RegionType.SOURCE_NAME}:
                candidates.append(region.text)
                continue
            
            if not region.bbox or not page.page_height:
                continue
            
            near_edge = (region.bbox[3] <= 0.15 * page.page_height or region.bbox[1] >= 0.85 * page.page_height)
            if (near_edge and "http" in region.text.lower() and 
                region.type not in {RegionType.NAVIGATION, RegionType.RELATED_CONTENT, RegionType.ADVERTISEMENT}):
                candidates.append(region.text)
                
        return "\n".join(candidates)
    
    
    @staticmethod
    def extract_sheet_info(text: str) -> Optional[tuple[int, Optional[int]]]:
        match = re.search(r"\bfoglio\s+(\d+)(?:\s*/\s*(\d+))?", text, flags=re.IGNORECASE)
        
        if not match:
            return None
        
        current = int(match.group(1))
        total = int(match.group(2)) if match.group(2) else None
        
        return current, total
    
    @staticmethod
    def extract_surface_percent(text: str) -> Optional[float]:
        match = re.search(r"\bSuperficie\s+([\d.,]+)\s*%", text, flags=re.IGNORECASE)
        
        if not match:
            return None
        
        value = match.group(1).replace(",", ".")
        return float(value)
    
    @staticmethod
    def extract_original_page(text: str) -> Optional[int]:
        match = re.search(r"\bda\s+pag\.?\s+(\d+)", text, flags=re.IGNORECASE)
        
        if not match:
            return None
        
        return int(match.group(1))
    
    @staticmethod
    def extract_url(text: str) -> Optional[str]:
        match = re.search(r"https?://[^\s\])>\"']+", text, flags=re.IGNORECASE)
        
        if not match:
            return None
        
        return match.group(0)
    
    @staticmethod
    def extract_date(text: str) -> Optional[date]:
        italian_pattern = (
            r"\b(\d{1,2})-([A-Z]{3})-(\d{4})\b"
        )

        for match in re.finditer(
            italian_pattern,
            text,
            flags=re.IGNORECASE,
        ):
            month = ITALIAN_MONTHS.get(
                match.group(2).upper()
            )
            if month is None:
                continue

            try:
                return date(int(match.group(3)), month, int(match.group(1)))
            except ValueError:
                # Es.: 31-FEB-2026. Non interrompere il PDF.
                continue

        slash_pattern = (
            r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"
        )

        for match in re.finditer(slash_pattern, text):
            year = int(match.group(3))
            if year < 100:
                year += 2000

            try:
                return date(year, int(match.group(2)), int(match.group(1)))
            except ValueError:
                # Es.: 39/04/2026, 24/19/2026.
                continue

        return None
    
    @staticmethod
    def extract_source_name(page: PageRecord) -> str | None:
        for region in page.regions:
            if region.type == RegionType.SOURCE_NAME and region.text:
                return region.text.strip()

        return None
    
    @staticmethod
    def extract_section(page: PageRecord) -> str | None:
        known_sections = {
            "stampa locale": "STAMPA_LOCALE",
            "stampa nazionale": "STAMPA_NAZIONALE",
            "web": "WEB",
            "radio": "RADIO",
            "tv": "TV",
            "televisione": "TELEVISIONE",
        }

        candidates: list[tuple[str, object]] = []

        for region in page.regions:
            if not region.text:
                continue

            # Un testo dentro una miniatura tecnica non è una
            # sezione della pagina PDF corrente.
            if region.metadata.get(
                "inside_article_position_thumbnail"
            ):
                continue

            eligible = (
                region.type in {
                    RegionType.FOOTER,
                    RegionType.HEADER_METADATA,
                }
                or region.metadata.get(
                    "in_header_metadata_zone"
                ) is True
            )
            if not eligible:
                continue

            normalized = " ".join(
                region.text.casefold().split()
            )
            section = known_sections.get(normalized)

            if section is not None:
                candidates.append((section, region))

        distinct = {section for section, _ in candidates}

        if len(distinct) != 1:
            # Nessun candidato oppure etichette in conflitto:
            # non scegliere arbitrariamente la prima.
            return None

        section = next(iter(distinct))
        for _, region in candidates:
            region.metadata["section_detection_method"] = (
                "bounded_exact_label"
            )
            region.metadata["section_value"] = section

        return section
    
    @staticmethod
    def source_name_from_url(url: str) -> str | None:
        host = urlparse(url).netloc.lower()
        
        if not host:
            return None
        
        host = host.removeprefix("www.")
        
        if not host:
            return None
        
        return host