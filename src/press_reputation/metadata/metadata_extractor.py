import re
from datetime import date
from typing import Optional

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
    #TODO: Da rivedere completamente per renderlo agnostico
    
    def enrich(self, page: PageRecord) -> PageRecord:
        text = self.metadata_text(page)

        if not text:
            text = self.page_text(page)

        publication_date = self.extract_date(text)
        original_page = self.extract_original_page(text)
        sheet_info = self.extract_sheet_info(text)
        surface_percent = self.extract_surface_percent(text)
        url = self.extract_url(self.page_text(page))
        section = self.extract_section(page)
        source_name = self.extract_source_name(page)

        if publication_date:
            page.source.publication_date = publication_date

        if original_page:
            page.source.original_page = original_page

        if url:
            page.source.url = url
            page.source.type = SourceType.WEB

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
        metadata_types = {
            RegionType.HEADER_METADATA,
            RegionType.SOURCE_NAME,
            RegionType.PRESS_REVIEW_PROVIDER,
            RegionType.PUBLICATION_DATE,
            RegionType.ORIGINAL_PAGE,
            RegionType.CLIPPING_SHEET,
        }

        return "\n".join(
            region.text.strip()
            for region in page.regions
            if region.type in metadata_types
            and region.text
            and region.text.strip()
        )
    
    @staticmethod
    def page_text(page: PageRecord) -> str:
        return "\n".join(region.text.strip() for region in page.regions if region.text and region.text.strip())
    
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
        italian_match = re.search(r"\b(\d{1,2})-([A-Z]{3})-(\d{4})\b", text, flags=re.IGNORECASE)
        
        if italian_match:
            day = int(italian_match.group(1))
            month_str = italian_match.group(2).upper()
            year = int(italian_match.group(3))
            
            month = ITALIAN_MONTHS.get(month_str)
            
            if month:
                return date(year, month, day)
            
        slash_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
        
        if slash_match:
            day = int(slash_match.group(1))
            month = int(slash_match.group(2))
            year = int(slash_match.group(3))
            
            if year < 100:
                year += 2000
            
            return date(year, month, day)
        
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
            "stampa locale",
            "stampa nazionale",
            "web",
            "radio",
            "tv",
            "televisione",
        }

        for region in page.regions:
            if not region.text:
                continue

            normalized = region.text.strip().lower()

            if normalized in known_sections:
                return normalized.upper().replace(" ", "_")

        return None