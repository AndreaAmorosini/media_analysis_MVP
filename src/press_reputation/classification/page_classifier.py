from press_reputation.models.page import PageRecord, PageType
from press_reputation.features import PageFeatureExtractor, PageFeatures


class PageClassifier:
    #Classificazione deterministico per PageType usando features estartte da page_features
    #TODO: migliorare classificazione con ML
    
    def __init__(self) -> None:
        self.feature_extractor = PageFeatureExtractor()
    
    def classify(self, page: PageRecord) -> PageType:
        features = self.feature_extractor.extract(page)
        
        if self.looks_like_clipping(features):
            return PageType.CLIPPING
        
        if self.looks_like_web(features):
            return PageType.WEB
        
        if self.looks_like_pure_text(features):
            return PageType.PURE_TEXT
        
        return PageType.UNKNOWN

    @staticmethod
    def looks_like_clipping(features: PageFeatures) -> bool:
        if features.clipping_marker_count >= 2:
            return True
        
        if (features.header_metadata_count >= 1 and features.clipping_marker_count >= 1):
            return True
        
        return False

    @staticmethod
    def looks_like_pure_text(features: PageFeatures) -> bool:
        if features.text_char_count < 500:
            return False
        
        if features.image_region_ratio > 0.4:
            return False
        
        if features.clipping_marker_count > 0:
            return False
        
        if features.web_marker_count > 0:
            return False
        
        return True
    
    @staticmethod
    def looks_like_web(features: PageFeatures) -> bool:
        if features.web_marker_count >= 2:
            return True
        
        if features.has_url and features.has_newsletter:
            return True
        
        if features.has_url and features.has_related_content_marker:
            return True
        
        return False

