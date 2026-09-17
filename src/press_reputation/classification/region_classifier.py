from press_reputation .features import RegionFeatureExtractor, RegionFeatures
from press_reputation.models.page import PageRecord, Region, RegionType

PROTECTED_REGION_TYPES = {
    RegionType.IMAGE,
    RegionType.CAPTION
}

class RegionClassifier:
    #Classifica le regioni di una pagina in base a caratteristiche specifiche (testuali e di layout)
    
    def __init__(self):
        self.feature_extractor = RegionFeatureExtractor()
    
    def enrich(self, page: PageRecord) -> PageRecord:
        for region in page.regions:
            region.type = self.classify(region, page)
            
        return page
    
    def classify(self, region: Region, page: PageRecord) -> RegionType:
        if region.type in PROTECTED_REGION_TYPES:
            return region.type
        
        features = self.feature_extractor.extract(region, page)
        
        if self.like_header_metadata(features):
            return RegionType.HEADER_METADATA
        
        if self.like_footer(features):
            return RegionType.FOOTER
        
        if self.like_navigation(features):
            return RegionType.NAVIGATION
        
        if self.like_related_content(features):
            return RegionType.RELATED_CONTENT
        
        if self.like_article_title(features):
            return RegionType.ARTICLE_TITLE
        
        if self.like_article_body(features):
            return RegionType.ARTICLE_BODY
        
        return region.type  # Mantieni il tipo originale se non corrisponde a nessuna categoria nota
    
    @staticmethod
    def like_header_metadata(features: RegionFeatures) -> bool:
        metadata_markers = sum([
            features.has_foglio,
            features.has_surface,
            features.has_tiratura,
            features.has_diffusione,
            features.has_lettori,
            features.has_dir_resp,
            features.has_quotidiano
        ])
        
        if metadata_markers >= 1 and features.is_top_area:
            return True
        
        if metadata_markers >= 2:
            return True
        
        return False
    
    @staticmethod
    def like_footer(features: RegionFeatures) -> bool:
        if not features.is_bottom_area:
            return False
        
        if features.text_length <= 180:
            return True
        
        if features.has_cookie_marker:
            return True
        
        return False
    
    @staticmethod
    def like_related_content(features: RegionFeatures) -> bool:
        if features.has_related_marker:
            return True
        
        if features.has_newsletter:
            return True
        
        if features.has_share_marker:
            return True
        
        return False
        
    @staticmethod
    def looks_like_article_title(features: RegionFeatures) -> bool:
        if features.raw_label != "section_header":
            return False

        if features.word_count < 3:
            return False

        if features.word_count > 18:
            return False

        if features.has_url:
            return False

        if features.has_newsletter or features.has_related_marker:
            return False

        return True

    @staticmethod
    def looks_like_article_body(features: RegionFeatures) -> bool:
        if features.raw_label != "text":
            return False

        if features.word_count < 15:
            return False

        metadata_markers = sum(
            [
                features.has_foglio,
                features.has_surface,
                features.has_tiratura,
                features.has_diffusione,
                features.has_lettori,
                features.has_dir_resp,
                features.has_quotidiano,
            ]
        )

        if metadata_markers:
            return False

        if features.has_url:
            return False

        if features.has_newsletter:
            return False

        if features.has_related_marker:
            return False

        if features.has_navigation_marker:
            return False

        if features.uppercase_ratio > 0.85:
            return False

        return True