from difflib import SequenceMatcher

from press_reputation.config import DocumentProfilingConfig
from press_reputation.models.page import PageRecord, Region
from press_reputation.profiling.models import DocumentProfile

def _overlap(a: list[float], b: list[float]) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return intersection / min(area_a, area_b)

def _duplicate(candidate: Region, existing: Region, config: DocumentProfilingConfig) -> bool:
    if (not candidate.text or not existing.text or not candidate.bbox or not existing.bbox):
        return False
    
    candidate_text = " ".join(candidate.text.casefold().split())
    existing_text = " ".join(existing.text.casefold().split())

    return (_overlap(candidate.bbox, existing.bbox) >= config.duplicate_bbox_overlap and
            SequenceMatcher(None, candidate_text, existing_text).ratio() >= config.duplicate_text_similarity)

def merge_extraction(native_pages: list[PageRecord], ocr_pages: dict[int, PageRecord], profile: DocumentProfile, config: DocumentProfilingConfig) -> list[PageRecord]:
    for page in native_pages:
        page_profile = profile.pages[page.pdf_page]
        ocr_page = ocr_pages.get(page.pdf_page)
        
        if ocr_page is not None:
            for region in ocr_page.regions:
                if not region.text or region.extraction_method != "ocr":
                    continue
                
                if any(_duplicate(region, existing, config) for existing in page.regions):
                    continue
                
                page.regions.append(region)
                
        methods = {region.extraction_method for region in page.regions if region.text and region.text.strip()}
        
        if {"pdf_text", "ocr"} <= methods:
            page_profile.kind = "mixed"
        elif "pdf_text" in methods:
            page_profile.kind = "native_pdf"
        elif "ocr" in methods:
            page_profile.kind = "ocr"
        elif page_profile.image_coverage > 0:
            page_profile.kind = "image_only"
        else:
            page_profile.kind = None
            
        page.extraction_profile = page_profile
        
    return native_pages