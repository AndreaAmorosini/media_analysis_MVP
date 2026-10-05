import re
from pydantic import BaseModel
from press_reputation.models.page import PageRecord, Region
from press_reputation.lookup import resolve_entity

class RegionFeatures(BaseModel):
    raw_text_lower: str = ""
    text_length: int = 0
    word_count: int = 0
    uppercase_ratio: float = 0.0
    
    has_url: bool = False
    has_email: bool = False
    has_date: bool = False
    
    has_foglio: bool = False
    has_surface: bool = False
    has_tiratura: bool = False
    has_diffusione: bool = False
    has_lettori: bool = False
    has_dir_resp: bool = False
    has_quotidiano: bool = False
    
    has_author_marker: bool = False
    has_ad_marker: bool = False
    has_rights_notice_marker: bool = False
    has_watermark_marker: bool = False
    
    has_newsletter: bool = False
    has_share_marker: bool = False
    has_related_marker: bool = False
    has_navigation_marker: bool = False
    has_cookie_marker: bool = False
        
    municipalities: list[dict] = []
    
    entity_kind: str | None = None
    entity_method: str | None = None
    entity_canonical_name: str | None = None
    entity_matched_name: str | None = None
    entity_similarity: float | None = None
    entity_match_coverage: float | None = None
    entity_priority: int | None = None
    entity_ambiguous: bool = False
    
    like_section_label: bool = False
    like_index_entry: bool = False
    
    bbox_x0: float | None = None
    bbox_y0: float | None = None
    bbox_x1: float | None = None
    bbox_y1: float | None = None
    bbox_width: float | None = None
    bbox_height: float | None = None
    bbox_area: float | None = None
    
    page_width: float | None = None
    page_height: float | None = None
    
    relative_x0: float | None = None
    relative_y0: float | None = None
    relative_x1: float | None = None
    relative_y1: float | None = None
    
    is_top_area: bool = False
    is_bottom_area: bool = False
    is_left_area: bool = False
    is_right_area: bool = False
    is_center_area: bool = False
    
    font_names: list[str] = []
    dominant_font: str | None = None
    median_font_size: float | None = None
    max_font_size: float | None = None
    bold_ratio: float | None = None
    italic_ratio: float | None = None
    dominant_color: tuple[float, ...] | None = None
    median_opacity: float | None = None

    style_source: str | None = None
    style_match_count: int = 0
    bold_evidence_fraction: float = 0.0
    italic_evidence_fraction: float = 0.0
    
    raw_label: str | None = None
    
def entity_lookup_metadata(features: RegionFeatures) -> dict | None:
    if features.entity_kind is None:
        return None
    
    return {
        "kind": features.entity_kind,
        "method": features.entity_method,
        "canonical_name": features.entity_canonical_name,
        "matched_name": features.entity_matched_name,
        "similarity": features.entity_similarity,
        "match_coverage": features.entity_match_coverage,
        "priority": features.entity_priority,
        "ambiguous": features.entity_ambiguous,
    }
class RegionFeatureExtractor:
    def extract(self, region: Region, page: PageRecord, *, include_entity: bool = True) -> RegionFeatures:
        text = region.text or ""
        lower = text.lower()
        normalized_line = lower.strip()
        words = re.findall(r"\w+", text)
        
        entity = (resolve_entity(text) if include_entity else None)
        accepted = (entity if entity is not None and not entity.ambiguous else None)
        
        municipalities = (accepted.record["municipalities"] if accepted is not None and accepted.kind == "location" else [])

        features = RegionFeatures(
            raw_text_lower = lower,
            text_length=len(text),
            word_count=len(words),
            uppercase_ratio=self.uppercase_ratio(text),
            has_url="http://" in lower or "https://" in lower,
            has_date=bool(
                re.search(r"\b\d{1,2}[-/][a-zA-Z]{3}[-/]\d{4}\b", text)
                or re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
            ),
            has_author_marker=(
                lower.strip().startswith("di ") or lower.strip().startswith("da" ) or "a cura di " in lower or lower.strip() in {"redazione", "la redazione"}
            ),
            has_ad_marker=(
                "pubblicità" in lower
                or "pubblicita" in lower
                or "advertisement" in lower
                or "sponsored" in lower
                or "sponsorizzato" in lower
                or "annuncio" in lower
                or "banner" in lower
                or "google ads" in lower
                or "adsbygoogle" in lower
                or "promoted" in lower
                or lower.strip() in {"adv", "ads", "ad"}
            ),
            has_foglio="foglio" in lower,
            has_surface="superficie" in lower,
            has_tiratura="tiratura" in lower,
            has_diffusione="diffusione" in lower,
            has_lettori="lettori" in lower,
            has_dir_resp="dir. resp" in lower or "dir.resp" in lower,
            has_quotidiano="quotidiano" in lower,
            has_newsletter="newsletter" in lower,
            has_share_marker="condividi" in lower or "twitta" in lower,
            has_related_marker=(
                "articoli correlati" in lower
                or "ultimi articoli" in lower
                or "leggi anche" in lower
            ),
            has_navigation_marker=(
                "menu" in lower
                or "home" == lower.strip()
                or "chi siamo" in lower
                or "contatti" in lower
            ),
            has_rights_notice_marker=(
                "©" in text
                or "copyright" in lower
                or "riproduzione riservata" in lower
                or "riproduzione vietata" in lower
                or "tutti i diritti riservati" in lower
                or "all rights reserved" in lower
                or "articolo non cedibile" in lower
                or "uso esclusivo" in lower
            ),
            has_watermark_marker=(
                "data stampa" in lower
                or "articolo non cedibile" in lower
                or "uso esclusivo" in lower
                or "cliente che lo riceve" in lower
            ),
            has_cookie_marker="cookie" in lower or "privacy policy" in lower,
            entity_kind=entity.kind if entity else None,
            entity_method=entity.method if entity else None,
            entity_canonical_name=(entity.canonical_name if entity else None),
            entity_matched_name=(entity.matched_name if entity else None),
            entity_similarity=(entity.similarity if entity else None),
            entity_match_coverage=(entity.match_coverage if entity else None),
            entity_priority=(entity.priority if entity else None),
            entity_ambiguous=(entity.ambiguous if entity else False),
            municipalities=municipalities,
            like_section_label=normalized_line in {"stampa locale", "stampa nazionale", "web", "radio", "tv", "televisione"},
            like_index_entry=self.looks_like_index_entry(text),
            raw_label=region.raw_label,
            font_names = list(region.style.get("font_names") or []),
            dominant_font=region.style.get("dominant_font"),
            median_font_size = region.style.get("median_font_size"),
            max_font_size = region.style.get("max_font_size"),
            bold_ratio = region.style.get("bold_ratio"),
            italic_ratio = region.style.get("italic_ratio"),
            dominant_color = (tuple(region.style["dominant_color"]) if region.style.get("dominant_color") is not None else None),
            median_opacity=region.style.get("median_opacity"),
            style_source=region.style.get("style_source"),
            style_match_count=region.style.get("style_match_count", 0),
            bold_evidence_fraction=region.style.get("bold_evidence_fraction", 0.0),
            italic_evidence_fraction=region.style.get("italic_evidence_fraction", 0.0)
        )

        self.add_bbox_features(features, region, page)

        return features
    
    @staticmethod
    def looks_like_index_entry(text: str) -> bool:
        lower = text.lower().strip()
        
        if len(lower) > 180:
            return False
        
        return bool("pag." in lower or re.search(r"\bpagina\s+\d+\b", lower) or re.search(r"\.{3,}\s*\d+$", lower))
    
    @staticmethod
    def add_bbox_features(features: RegionFeatures, region: Region, page: PageRecord) -> None:
        if not region.bbox or len(region.bbox) != 4:
            return

        x0, y0, x1, y1 = region.bbox

        features.bbox_x0 = x0
        features.bbox_y0 = y0
        features.bbox_x1 = x1
        features.bbox_y1 = y1
        features.bbox_width = max(0.0, x1 - x0)
        features.bbox_height = max(0.0, y1 - y0)
        features.bbox_area = features.bbox_width * features.bbox_height

        if not page.page_width or not page.page_height:
            return

        features.relative_x0 = x0 / page.page_width
        features.relative_y0 = y0 / page.page_height
        features.relative_x1 = x1 / page.page_width
        features.relative_y1 = y1 / page.page_height

        features.is_top_area = features.relative_y0 < 0.15
        features.is_bottom_area = features.relative_y1 > 0.80
        features.is_left_area = features.relative_x1 < 0.35
        features.is_right_area = features.relative_x0 > 0.60
        features.is_center_area = (features.relative_x0 > 0.15 and features.relative_x1 < 0.85)
        
    @staticmethod
    def page_size_from_regions(page: PageRecord) -> tuple[float | None, float | None]:
        max_x = None
        max_y = None
        
        for region in page.regions:
            if not region.bbox or len(region.bbox) != 4:
                continue
            
            _, _, x1, y1 = region.bbox
            
            max_x = x1 if max_x is None else max(max_x, x1)
            max_y = y1 if max_y is None else max(max_y, y1)
            
        return max_x, max_y
    
    @staticmethod
    def uppercase_ratio(text: str) -> float:
        letters = [char for char in text if char.isalpha()]
        
        if not letters:
            return 0.0
        
        uppercase = [char for char in letters if char.isupper()]
        
        return len(uppercase) / len(letters)
