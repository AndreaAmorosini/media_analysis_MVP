import re
from pydantic import BaseModel
from press_reputation.models.page import PageRecord, Region

class RegionFeatures(BaseModel):
    text_length: int = 0
    word_count: int = 0
    uppercase_ratio: float = 0.0
    
    has_url: bool = False
    has_email: bool = False
    
    has_foglio: bool = False
    has_surface: bool = False
    has_tiratura: bool = False
    has_diffusione: bool = False
    has_lettori: bool = False
    has_dir_resp: bool = False
    has_quotidiano: bool = False
    
    has_newsletter: bool = False
    has_share_marker: bool = False
    has_related_marker: bool = False
    has_navigation_marker: bool = False
    has_cookie_marker: bool = False
    
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
    
    raw_label: str | None = None
    
class RegionFeatureExtractor:
    def extract(self, region: Region, page: PageRecord) -> RegionFeatures:
        text = region.text or ""
        lower = text.lower()
        words = re.findall(r"\w+", text)

        uppercase_ratio = self.uppercase_ratio(text)

        features = RegionFeatures(
            text_length=len(text),
            word_count=len(words),
            uppercase_ratio=uppercase_ratio,
            has_url="http://" in lower or "https://" in lower,
            has_date=bool(
                re.search(r"\b\d{1,2}[-/][a-zA-Z]{3}[-/]\d{4}\b", text)
                or re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
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
            has_cookie_marker="cookie" in lower or "privacy policy" in lower,
            raw_label=region.raw_label,
        )

        self.add_bbox_features(features, region, page)

        return features
    
    def add_bbox_features(self, features: RegionFeatures, region: Region, page: PageRecord) -> None:
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
        
        page_width, page_height = self.page_size_from_regions(page)
        
        if not page_width or not page_height:
            return
        
        features.page_width = page_width
        features.page_height = page_height
        
        features.relative_x0 = x0 / page_width
        features.relative_y0 = y0 / page_height
        features.relative_x1 = x1 / page_width
        features.relative_y1 = y1 / page_height
        
        features.is_top_area = features.relative_y0 < 0.15
        features.is_bottom_area = features.relative_y1 > 0.85
        features.is_left_area = features.relative_x1 < 0.35
        features.is_right_area = features.relative_x0 > 0.65
        features.is_center_area = (features.relative_x0 > 0.2 and features.relative_x1 < 0.0)
        
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
