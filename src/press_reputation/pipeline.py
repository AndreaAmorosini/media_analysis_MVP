from pathlib import Path

from press_reputation.classification import (
    PageClassifier,
    RegionClassifier,
    TechnicalRegionClassifier,
)
from press_reputation.classification.boilerplate_detector import (
    DocumentBoilerplateDetector,
)
from press_reputation.classification.header_metadata_zone import (
    HeaderMetadataZoneDetector,
)
from press_reputation.metadata import MetadataExtractor
from press_reputation.models.page import PageRecord
from press_reputation.reconstruction import BodyContinuationResolver
from press_reputation.style import PdfStyleEnricher
from press_reputation.reconstruction.body_grouping import BodyGroupingResolver


class PageProcessingPipeline:
    def __init__(
        self,
        enable_style_enrichment: bool = True,
        enable_boilerplate_detection: bool = True,
        enable_body_continuation: bool = True,
    ) -> None:
        self.enable_style_enrichment = enable_style_enrichment
        self.enable_boilerplate_detection = enable_boilerplate_detection
        self.enable_body_continuation = enable_body_continuation

        self.style_enricher = PdfStyleEnricher()
        self.boilerplate_detector = DocumentBoilerplateDetector()
        self.technical_classifier = TechnicalRegionClassifier()
        self.header_zone_detector = HeaderMetadataZoneDetector()
        self.body_grouping_resolver = BodyGroupingResolver()
        self.region_classifier = RegionClassifier()
        self.body_resolver = BodyContinuationResolver()
        self.metadata_extractor = MetadataExtractor()
        self.page_classifier = PageClassifier()

    def process(
        self,
        pages: list[PageRecord],
        pdf_path: Path | None = None,
    ) -> list[PageRecord]:
        if self.enable_style_enrichment and pdf_path is not None:
            self.style_enricher.enrich_document(
                pdf_path=pdf_path,
                pages=pages,
            )

        if self.enable_boilerplate_detection:
            self.boilerplate_detector.enrich(pages)

        for page in pages:
            self.technical_classifier.enrich(page)
            self.header_zone_detector.enrich(page)
            self.region_classifier.enrich(page)

            if self.enable_body_continuation:
                self.body_resolver.enrich(page)
                
            self.body_grouping_resolver.enrich(page)

            self.metadata_extractor.enrich(page)
            page.page_type = self.page_classifier.classify(page)

        return pages