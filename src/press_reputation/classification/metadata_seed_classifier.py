from press_reputation.features import RegionFeatureExtractor
from press_reputation.features.region_features import entity_lookup_metadata
from press_reputation.models.page import PageRecord, RegionType

class MetadataSeedClassifier:
    TECHNICAL_TYPES = {
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.ADVERTISEMENT,
        RegionType.FOOTER,
        RegionType.TABLE,
        RegionType.INFOGRAPHIC,
        RegionType.PULL_QUOTE
    }
    
    SEED_TYPES = {
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.HEADER_METADATA
    }
    
    def __init__(self) -> None:
        self.feature_extractor = RegionFeatureExtractor()
        
    def enrich(self, page: PageRecord) -> PageRecord:
        for region in page.regions:
            if (region.type in self.TECHNICAL_TYPES or region.exclude_from_article_text or not region.text):
                continue
            
            if region.type in self.SEED_TYPES:
                region.metadata.setdefault("metadata_seed_method", "preexisting_type")
                continue
            
            features = self.feature_extractor.extract(region, page)
            if features.entity_kind is not None:
                lookup_data = entity_lookup_metadata(features)
                region.metadata["entity_lookup"] = lookup_data
            text = features.raw_text_lower
            
            detected_type = None
            reason = None
            
            #Provider/testata prima di location o di eventuali euristiche successive
            if (features.entity_kind == "source" and not features.entity_ambiguous):
                detected_type = RegionType.SOURCE_NAME
                reason = (f"entity_{features.entity_method}_source")
            elif (features.entity_kind == "provider" and not features.entity_ambiguous):
                detected_type = RegionType.PRESS_REVIEW_PROVIDER
                reason = (f"entity_{features.entity_method}_provider")
            elif self._composite(features):
                detected_type = RegionType.HEADER_METADATA
                reason = "composite_clipping_metadata"
            elif "da pag" in text:
                detected_type = RegionType.ORIGINAL_PAGE
                reason = "original_page_marker"
            elif features.has_foglio:
                detected_type = RegionType.CLIPPING_SHEET
                reason = "clipping_sheet_marker"
            elif self._short_date(features):
                detected_type = RegionType.PUBLICATION_DATE
                reason = "short_date_marker"
            elif region.raw_label == "page_header":
                detected_type = RegionType.HEADER_METADATA
                reason = "docling_page_header"
            elif self._audience_metadata(features):
                detected_type = RegionType.HEADER_METADATA
                reason = "audience_metadata_marker"
                
            if detected_type is None:
                continue
            
            previous_type = region.type.value
            region.type = detected_type
            region.metadata["metadata_seed_method"] = reason
            region.metadata["type_before_metadata_seed"] = previous_type
            
            region.exclude_from_article_text = True
            
        return page
    
    
    @staticmethod
    def _composite(features) -> bool:
        return sum(("da pag" in features.raw_text_lower, features.has_foglio, features.has_date)) >= 2
    
    @staticmethod
    def _short_date(features) -> bool:
        return (features.has_date and features.text_length <= 40 and not features.has_foglio and "da pag" not in features.raw_text_lower)
    
    @staticmethod
    def _audience_metadata(features) -> bool:
        return any((features.has_surface, features.has_tiratura, features.has_diffusione, features.has_lettori, features.has_dir_resp, features.has_quotidiano))
