import re
import hashlib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from press_reputation.config import DocumentBoilerplateConfig
from press_reputation.models.page import PageRecord, RegionType, Region

@dataclass(frozen=True)
class BoilerplateFingerprint:
    family: str
    text_pattern: str
    relative_bbox: tuple[float, float, float, float]
    dominant_font: str | None
    median_font_size: float | None
@dataclass
class FingerprintGroup:
    fingerprint: BoilerplateFingerprint
    members: list[tuple[PageRecord, Region]] = field(default_factory=list)
    pdf_pages: set[int] = field(default_factory=set)

class DocumentBoilerplateDetector:
    def __init__(self, config: DocumentBoilerplateConfig | None = None) -> None:
        self.config = config or DocumentBoilerplateConfig()

    def enrich(self, pages: list[PageRecord]) -> list[PageRecord]:
        documents: dict[str, list[PageRecord]] = defaultdict(list)
        
        for page in pages:
            documents[page.document_id].append(page)
            
        for document_id, document_pages in documents.items():
            distinct_pages = {page.pdf_page for page in document_pages}
            total_pages = len(distinct_pages)
            
            if (total_pages < self.config.min_distinct_pages):
                continue
            
            groups_by_pattern: dict[tuple[str, str], list[FingerprintGroup]] = defaultdict(list)
            
            for page in sorted(document_pages, key=lambda item: item.pdf_page):
                for region in page.regions:
                    fingerprint = self.fingerprint(page, region)
                    if fingerprint is None:
                        continue
                    
                    key = (fingerprint.family, fingerprint.text_pattern)
                    groups = groups_by_pattern[key]
                    
                    selected = next((group for group in groups
                                        if self.compatible(fingerprint, group.fingerprint)), None)
                    
                    if selected is None:
                        selected = FingerprintGroup(fingerprint=fingerprint)
                        groups.append(selected)
                        
                    selected.members.append((page, region))
                    selected.pdf_pages.add(page.pdf_page)
                    
            for groups in groups_by_pattern.values():
                for group in groups:
                    supporting_pages = len(group.pdf_pages)
                    frequency = (supporting_pages / total_pages)
                    
                    repeated = (supporting_pages >= self.config.min_distinct_pages and
                                frequency >= self.config.min_page_frequency)
                    
                    digest = hashlib.sha256((document_id + "|" + group.fingerprint.family + "|" +
                                                group.fingerprint.text_pattern + "|" + str(group.fingerprint.relative_bbox)
                                                ).encode("utf-8")).hexdigest()[:16]
                    
                    for _, region in group.members:
                        region.metadata["boilerplate_detection"] = {
                            "method": ("text_layout_style_v1"),
                            "fingerprint_id": digest,
                            "family": group.fingerprint.family,
                            "text_pattern": group.fingerprint.text_pattern,
                            "supporting_pdf_pages": supporting_pages,
                            "document_pdf_pages": total_pages,
                            "frequency": frequency,
                            "repeated": repeated,
                        }
                        
                        if repeated:
                            region.boilerplate = True
                            region.boilerplate_frequency = frequency
                            
        return pages
    
    def text_pattern(self, text: str) -> tuple[str, str] | None:
        if not text:
            return None
        
        normalized = unicodedata.normalize("NFKC", text).casefold()
        normalized = re.sub(r"\s+", " ", normalized).strip()
        
        if (not normalized or len(normalized) > self.config.max_region_text_chars):
            return None
        
        if re.match(r"^data\s+stampa\b", normalized):
            #parte numerica/data/id variabile
            return ("data_stampa", "data stampa <variabile>")
        
        if normalized == "web":
            return ("section_label", "web")
        
        if normalized in {"stampa locale", "stampa nazionale"}:
            return ("section_label", normalized)
        
        if re.match(r"^articolo\s+non\s+cedibile\b", normalized):
            return ("rights_candidate", re.sub(r"\b\d+(?:[./:-]\d+)*\b", "<num>", normalized))
        
        if re.match(r"^uso\s+esclusivo\b",normalized):
            return ("rights_candidate", re.sub(r"\b\d+(?:[./:-]\d+)*\b", "<num>", normalized))
        
        if (len(normalized) < self.config.min_generic_text_chars or
            len(normalized.split()) > self.config.max_generic_words):
            return None
        
        return ("generic_exact", normalized)
    
    def fingerprint(self, page: PageRecord, region: Region) -> BoilerplateFingerprint | None:
        if (region.type in {RegionType.IMAGE, RegionType.TABLE, RegionType.INFOGRAPHIC} or
            not region.bbox or len(region.bbox) != 4 or not page.page_width or not page.page_height):
            return None
        
        pattern = self.text_pattern(region.text)
        if pattern is None:
            return None
        
        x0, y0, x1, y1 = region.bbox
        if x1 <= x0 or y1 <= y0:
            return None
        
        family, text_pattern = pattern
        return BoilerplateFingerprint(
            family = family,
            text_pattern = text_pattern,
            relative_bbox = (x0/page.page_width, y0/page.page_height, x1/page.page_width, y1/page.page_height),
            dominant_font = region.style.get("dominant_font"),
            median_font_size=region.style.get("median_font_size"),
        )
        
    def compatible(self, left: BoilerplateFingerprint, right: BoilerplateFingerprint) -> bool:
        if (left.family != right.family or left.text_pattern != right.text_pattern):
            return False
        
        a = left.relative_bbox
        b = right.relative_bbox
        
        a_center = ((a[0]+a[2])/2, (a[1]+a[3])/2)
        b_center = ((b[0]+b[2])/2, (b[1]+b[3])/2)
        
        if any(abs(x - y) > self.config.max_relative_position_delta for x, y in zip(a_center, b_center)):
            return False
        
        a_size = (a[2]-a[0], a[3]-a[1])
        b_size = (b[2]-b[0], b[3]-b[1])
        
        if any(abs(x - y) > self.config.max_relative_size_delta for x, y in zip(a_size, b_size)):
            return False
        
        if (left.dominant_font and right.dominant_font and
            left.dominant_font != right.dominant_font):
            return False
        
        if (left.median_font_size and right.median_font_size):
            larger = max(left.median_font_size, right.median_font_size)
            difference = abs(left.median_font_size - right.median_font_size) / larger
            
            if difference > self.config.max_font_size_relative_difference:
                return False
            
        return True
        