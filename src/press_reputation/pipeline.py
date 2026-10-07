from pathlib import Path
from collections import Counter

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
from press_reputation.classification.metadata_seed_classifier import MetadataSeedClassifier
from press_reputation.classification.region_classifier import ArticleSemanticClassifier
from press_reputation.classification.title_resolver import TitleResolver
from press_reputation.metadata import MetadataExtractor
from press_reputation.models.page import PageRecord
from press_reputation.reconstruction import BodyContinuationResolver
from press_reputation.style import PdfStyleEnricher
from press_reputation.reconstruction.body_grouping import BodyGroupingResolver
from press_reputation.classification.web_main_content import WebMainContentResolver
from press_reputation.reconstruction.web_article_continuation import WebArticleContinuationResolver
from press_reputation.reconstruction.article_flow import ArticleFlowResolver
from press_reputation.reconstruction.flow_models import FlowLink
from press_reputation.review_index.matcher import ReviewIndexMatcher
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch
from press_reputation.classification.subtitle_resolver import SubtitleResolver
from press_reputation.classification.section_header_resolver import SectionHeaderResolver
from press_reputation.classification.author_resolver import AuthorResolver
from press_reputation.reconstruction.article_clustering import ArticleClusteringResolver


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
        
        self.article_clustering_resolver = ArticleClusteringResolver()
        self.author_resolver = AuthorResolver()
        self.section_header_resolver = SectionHeaderResolver()
        self.subtitle_resolver = SubtitleResolver()
        self.title_resolver = TitleResolver()
        self.review_index_matcher = ReviewIndexMatcher()
        self.review_index_matches: list[ReviewIndexMatch] = []
        self.style_enricher = PdfStyleEnricher()
        self.boilerplate_detector = DocumentBoilerplateDetector()
        self.technical_classifier = TechnicalRegionClassifier()
        self.header_zone_detector = HeaderMetadataZoneDetector()
        self.web_content_resolver = WebMainContentResolver()
        self.body_grouping_resolver = BodyGroupingResolver()
        self.web_article_continuation_resolver = WebArticleContinuationResolver()
        self.article_flow_resolver = ArticleFlowResolver()
        self.flow_links: list[FlowLink] = []
        self.region_classifier = RegionClassifier()
        self.body_resolver = BodyContinuationResolver()
        self.metadata_extractor = MetadataExtractor()
        self.page_classifier = PageClassifier()
        self.metadata_seed_classifier = MetadataSeedClassifier()
        self.article_semantic_classifier = ArticleSemanticClassifier()

    def process(self, pages: list[PageRecord], pdf_path: Path | None = None, review_index_entries: list[ReviewIndexEntry] | None = None) -> list[PageRecord]:
        self.flow_links = []
        self.review_index_matches = []
        
        if self.enable_style_enrichment and pdf_path is not None:
            self.style_enricher.enrich_document(pdf_path=pdf_path, pages=pages)

        if self.enable_boilerplate_detection:
            self.boilerplate_detector.enrich(pages)

        document_page_counts = Counter(page.document_id for page in pages)
        # Primo passaggio: classificazioni locali e metadata.
        for page in pages:
            self.technical_classifier.enrich(page, document_page_count=document_page_counts[page.document_id])
            self.metadata_seed_classifier.enrich(page)
            self.header_zone_detector.enrich(page)
            self.article_semantic_classifier.enrich(page)
            self.metadata_extractor.enrich(page)
            
        self.title_resolver.resolve(pages, entries=review_index_entries)
        for page in pages:
            self.subtitle_resolver.enrich(page)
            
        if review_index_entries:
            self.review_index_matches = self.review_index_matcher.match(pages, review_index_entries)
            
        entries_by_id = {entry.id: entry for entry in review_index_entries or []}
        
        for page in pages:
            self.author_resolver.enrich(page, self.review_index_matches, entries_by_id)
            page.page_type = self.page_classifier.classify(page)
            

        self.web_content_resolver.enrich_document(pages)
        self.web_article_continuation_resolver.enrich_document(pages)
        self.article_clustering_resolver.assign_local(pages, self.review_index_matches)
        self.title_resolver.consolidate_candidates(pages)
        self.flow_links = self.article_flow_resolver.resolve(pages)
        self.article_clustering_resolver.link_accepted_flows(pages, self.flow_links)
        
        for page in pages:
            if self.enable_body_continuation:
                self.body_resolver.enrich(page)
                
            self.article_clustering_resolver.assign_recovered_body(page)
            self.section_header_resolver.enrich(page)
            self.article_clustering_resolver.assign_section_headers_and_media(page)
            self.body_grouping_resolver.enrich(page)
            
        return pages