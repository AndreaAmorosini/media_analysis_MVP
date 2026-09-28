import re
from collections import Counter

from press_reputation.models.page import PageRecord, RegionType

class DocumentBoilerplateDetector:
    def __init__(self, min_frequency: float = 0.5) -> None:
        self.min_frequency = min_frequency
        
    def enrich(self, pages: list[PageRecord]) -> list[PageRecord]:
        page_count = max(len(pages), 1)
        counter: Counter[str] = Counter()
        
        for page in pages:
            seen_on_page = set()
            
            for region in page.regions:
                key = self.region_key(region.text)
                
                if key:
                    seen_on_page.add(key)
                    
            counter.update(seen_on_page)
            
        frequencies = {key:count/page_count for key, count in counter.items()}
        
        for page in pages:
            for region in page.regions:
                key = self.region_key(region.text)
                
                if not key:
                    continue
                
                frequency = frequencies.get(key, 0.0)
                
                if frequency >= self.min_frequency:
                    region.boilerplate = True
                    region.boilerplate_frequency = frequency
                    
                    if region.type not in {
                        RegionType.SOURCE_NAME,
                        RegionType.PUBLICATION_DATE,
                        RegionType.ORIGINAL_PAGE,
                        RegionType.CLIPPING_SHEET,
                    }:
                        region.exclude_from_article_text = True
        
        return pages
    
    @staticmethod
    def region_key(text: str | None) -> str | None:
        if not text:
            return None
        
        value = text.lower().strip()
        value = re.sub(r"\s+", " ", value)
        
        if len(value) < 8:
            return None
        
        return value