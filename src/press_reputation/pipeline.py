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
from press_reputation.reconstruction.article_reading_order import ArticleReadingOrderResolver
from press_reputation.reconstruction.flow_models import ArticleReadingOrder
from press_reputation.reconstruction.newspaper_continuation import NewspaperContinuationResolver
from press_reputation.classification.inline_intrusion import InlineIntrusionDetector
from press_reputation.classification.pull_quote_resolver import PullQuoteResolver
from press_reputation.classification.editorial_header_resolver import EditorialHeaderResolver
from press_reputation.classification.watermark_text_cleaner import WatermarkTextCleaner
from press_reputation.classification.web_chrome_resolver import (
    WebChromeResolver,
)
from press_reputation.classification.web_article_zone_resolver import (
    WebArticleZoneResolver,
)
from press_reputation.reconstruction.body_membership_refinement import (
    BodyMembershipRefinementResolver,
)
from press_reputation.classification.editorial_preview_boundary import (
    EditorialPreviewBoundaryResolver,
)
from press_reputation.reconstruction.column_membership_propagation import (
    ColumnMembershipPropagationResolver,
)
from press_reputation.reconstruction.cross_page_article_reconciler import (
    CrossPageArticleReconciler,
)
from press_reputation.classification.editorial_header_arbitrator import (
    EditorialHeaderArbitrator,
)


class PageProcessingPipeline:
    def __init__(self, enable_style_enrichment: bool = True, enable_boilerplate_detection: bool = True, enable_body_continuation: bool = True) -> None:
        self.enable_style_enrichment = enable_style_enrichment
        self.enable_boilerplate_detection = enable_boilerplate_detection
        self.enable_body_continuation = enable_body_continuation
        self.reading_orders: dict[tuple[str, str], ArticleReadingOrder] = {}
        

        self.editorial_header_arbitrator = EditorialHeaderArbitrator()
        self.cross_page_article_reconciler = CrossPageArticleReconciler()
        self.column_membership_propagation_resolver = (
            ColumnMembershipPropagationResolver()
        )
        self.editorial_preview_boundary_resolver = EditorialPreviewBoundaryResolver()
        self.body_membership_refinement_resolver = (
            BodyMembershipRefinementResolver()
        )
        self.web_article_zone_resolver = WebArticleZoneResolver()
        self.web_chrome_resolver = WebChromeResolver()
        self.watermark_text_cleaner = WatermarkTextCleaner()
        self.editorial_header_resolver = EditorialHeaderResolver()
        self.pull_quote_resolver = PullQuoteResolver()
        self.inline_intrusion_detector = InlineIntrusionDetector()
        self.newspaper_continuation_resolver = NewspaperContinuationResolver()
        self.reading_order_resolver = ArticleReadingOrderResolver()
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
        self.cross_page_reconciliations = []
        self.reading_orders = {}
        
        if self.enable_style_enrichment and pdf_path is not None:
            self.style_enricher.enrich_document(pdf_path=pdf_path, pages=pages)

        if self.enable_boilerplate_detection:
            self.boilerplate_detector.enrich(pages)
        
        self.watermark_text_cleaner.enrich(pages, pdf_path=pdf_path)

        document_page_counts = Counter(page.document_id for page in pages)
        # Primo passaggio: classificazioni locali e metadata.
        for page in pages:
            self.technical_classifier.enrich(page, document_page_count=document_page_counts[page.document_id])
            self.metadata_seed_classifier.enrich(page)
            self.header_zone_detector.enrich(page)
            self.article_semantic_classifier.enrich(page)
            self.metadata_extractor.enrich(page)
            page.page_type = self.page_classifier.classify(page)
            
        self.web_chrome_resolver.enrich_document(pages)
        self.web_article_zone_resolver.enrich_document(pages)
        self.web_article_continuation_resolver.mark_continuation_priors(pages)
        self.title_resolver.resolve(pages, entries=review_index_entries)
        self.title_resolver.resolve_web_zone_titles(pages)
        for page in pages:
            self.editorial_header_resolver.enrich(page)
            self.subtitle_resolver.enrich(page)
            
        self.editorial_preview_boundary_resolver.resolve(pages)
        self.editorial_header_arbitrator.resolve_header_stacks(pages)
            
        if review_index_entries:
            self.review_index_matches = self.review_index_matcher.match(pages, review_index_entries)
            
        entries_by_id = {entry.id: entry for entry in review_index_entries or []}
        
        for page in pages:
            self.author_resolver.enrich(page, self.review_index_matches, entries_by_id)
            page.page_type = self.page_classifier.classify(page)
            

        self.web_content_resolver.enrich_document(pages)
        self.editorial_header_arbitrator.suppress_non_main_titles(pages)
        for page in pages:
            self.inline_intrusion_detector.enrich(page)
        self.web_article_continuation_resolver.enrich_document(pages)
        self.article_clustering_resolver.assign_local(pages, self.review_index_matches)
        self.title_resolver.consolidate_candidates(pages)
        for page in pages:
            self.editorial_header_resolver.enrich(page)

        self.flow_links = self.article_flow_resolver.resolve(pages)
        self.article_clustering_resolver.link_accepted_flows(pages, self.flow_links)
        self.article_clustering_resolver.detach_rejected_web_pages(pages, self.flow_links)
        self.article_clustering_resolver.propagate_web_article_metadata(pages, self.flow_links)
        
        newspaper_links = self.newspaper_continuation_resolver.resolve(pages, entries=review_index_entries, matches=self.review_index_matches)
        self.flow_links.extend(newspaper_links)
        
        for page in pages:
            if self.enable_body_continuation:
                self.body_resolver.enrich(page)
                
            self.author_resolver.recover_abbreviated_signatures(page)
            self.pull_quote_resolver.enrich(page, pages)
            
            self.article_clustering_resolver.assign_unassigned_body_in_owned_columns(page)
            self.article_clustering_resolver.recover_column_continuations(page)
            self.article_clustering_resolver.assign_recovered_body(page)
            
            self.body_membership_refinement_resolver.enrich_page(page)
            
            self.column_membership_propagation_resolver.enrich_page(page)
            
            if self.enable_body_continuation:
                self.body_resolver.enrich(page)
            
            self.section_header_resolver.enrich(page)
            
            self.article_clustering_resolver.assign_section_headers_and_media(page)
            self.body_grouping_resolver.enrich(page)
        
        self.cross_page_reconciliations = self.cross_page_article_reconciler.resolve(pages, self.flow_links)
        self.reading_orders = self.reading_order_resolver.resolve(pages)
            
        return pages