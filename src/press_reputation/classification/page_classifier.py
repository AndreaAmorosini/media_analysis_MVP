from press_reputation.features import PageFeatureExtractor, PageFeatures
from press_reputation.models.page import PageRecord, PageType


class PageClassifier:
    def __init__(self) -> None:
        self.feature_extractor = PageFeatureExtractor()

    def classify(self, page: PageRecord) -> PageType:
        features = self.feature_extractor.extract(page)

        if self.looks_like_index(features):
            return PageType.INDEX

        if self.looks_like_clipping(features):
            return PageType.CLIPPING

        if self.looks_like_web(features):
            return PageType.WEB

        if self.looks_like_pure_text(features):
            return PageType.PURE_TEXT

        return PageType.UNKNOWN

    @staticmethod
    def looks_like_index(features: PageFeatures) -> bool:
        if features.pdf_page != 1:
            return False

        if features.clipping_marker_count > 0:
            return False

        if features.index_entry_count >= 3:
            return True

        return features.text_char_count < 1500 and features.region_count >= 8

    @staticmethod
    def looks_like_clipping(features: PageFeatures) -> bool:
        if features.clipping_marker_count >= 2:
            return True

        return (
            features.header_metadata_count >= 1
            and features.clipping_marker_count >= 1
        )

    @staticmethod
    def looks_like_web(features: PageFeatures) -> bool:
        if features.web_marker_count >= 2:
            return True

        if features.has_url and features.has_newsletter:
            return True

        return features.has_url and features.has_related_content_marker

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