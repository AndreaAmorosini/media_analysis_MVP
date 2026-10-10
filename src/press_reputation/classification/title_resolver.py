from statistics import median

from press_reputation.config import TitleResolutionConfig
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.review_index.matcher import title_similarity
from press_reputation.review_index.models import ReviewIndexEntry

class TitleResolver:
    BLOCKED_TYPES = {
        RegionType.HEADER_METADATA,
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.AUTHOR,
        RegionType.LOCATION,
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.FOOTER,
        RegionType.NAVIGATION,
        RegionType.RELATED_CONTENT,
        RegionType.ADVERTISEMENT,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
    }

    CANDIDATE_TYPES = {
        RegionType.UNKNOWN,
        RegionType.ARTICLE_BODY,
        RegionType.ARTICLE_SECTION_HEADER,
        RegionType.ARTICLE_TITLE,
    }
    
    def __init__(self, config: TitleResolutionConfig | None = None) -> None:
        self.config = config or TitleResolutionConfig()
        self.features = RegionFeatureExtractor()
        
    def resolve(self, pages: list[PageRecord], entries: list[ReviewIndexEntry] | None = None) -> list[PageRecord]:
        entries = entries or []
        index_pages = {entry.index_pdf_page for entry in entries}
        
        for page in pages:
            if page.pdf_page in index_pages:
                continue
            
            page_entries = [entry for entry in entries if entry.document_id == page.document_id]
            
            for region in page.regions:
                if not self.eligible(region):
                    continue
                
                components, index_entry_id = self.score(region, page, page_entries)
                
                total = round(sum(components.values()), 4)
                
                region.metadata["title_candidate"] = {
                    "score": total,
                    "components": components,
                    "index_entry_id": index_entry_id,
                    "method": "title_resolution_v1",
                }
                
                if total < self.config.min_score:
                    continue
                
                if region.type != RegionType.ARTICLE_TITLE:
                    region.metadata["type_before_title_resolution"] = (region.type.value)
                    if (region.raw_label == "section_header" and self.follows_body_in_same_column(region, page) and not index_entry_id):
                        region.metadata["title_candidate_veto"] = "heading_after_body"
                        region.metadata["deferred_section_header_candidate"] = True
                        continue
                    region.type = RegionType.ARTICLE_TITLE
                    
                region.metadata["title_role"] = "candidate"
                
        return pages
    
    def eligible(self, region: Region) -> bool:
        return (
            region.type in self.CANDIDATE_TYPES
            and region.type not in self.BLOCKED_TYPES
            and bool((region.text or "").strip())
            and region.bbox is not None
            and len(region.bbox) == 4
            and region.bbox[2] > region.bbox[0]
            and region.bbox[3] > region.bbox[1]
            and not region.exclude_from_article_text
            and not region.metadata.get("in_header_metadata_zone")
            and not region.metadata.get(
                "inside_article_position_thumbnail"
            )
            and region.metadata.get("content_scope") not in {
                "related",
                "advertisement",
                "boilerplate",
                "non_main",
                "navigation"
            }
        )
        
    @staticmethod
    def horizontal_overlap(a: Region, b: Region) -> float:
        overlap = max(.0, min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]))
        width = min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
        return overlap / width if width > 0 else 0.0
    
    def nearest_body(self, region: Region, page: PageRecord) -> tuple[Region, float] | None:
        if not page.page_height:
            return None
        
        candidates = []
        for body in page.regions:
            if (body is region or body.type != RegionType.ARTICLE_BODY or not body.bbox or
                body.exclude_from_article_text or body.metadata.get("in_header_metadata_zone")):
                continue
            
            gap = body.bbox[1] - region.bbox[3]
            if (gap < 0 or gap / page.page_height > self.config.max_body_gap_relative or
                self.horizontal_overlap(region, body) < self.config.min_horizontal_overlap):
                continue
            
            candidates.append((body, gap))
            
        return (min(candidates, key=lambda item: item[1]) if candidates else None)
        
    def related_region(self, region: Region, page: PageRecord) -> bool:
        if not page.page_height:
            return False
        
        for other in page.regions:
            if (other is region or other.type not in {RegionType.AUTHOR, RegionType.ARTICLE_SUBTITLE} or not other.bbox or
                other.exclude_from_article_text or other.metadata.get("in_header_metadata_zone")):
                continue
            
            gap = min(abs(other.bbox[1] - region.bbox[3]), abs(region.bbox[1] - other.bbox[3]))
            if (gap / page.page_height <= self.config.max_related_gap_relative and
                self.horizontal_overlap(region, other) >= self.config.min_horizontal_overlap):
                return True
            
        return False
    
    def score(self, region: Region, page: PageRecord, entries: list[ReviewIndexEntry]) -> tuple[dict[str, float], str | None]:
        features = self.features.extract(region, page)
        components: dict[str, float] = {}
        best_entry_id: str | None = None

        if region.raw_label == "section_header":
            components["docling_label"] = 0.12

        if (page.page_width and region.bbox[2] - region.bbox[0] >= 0.25 * page.page_width):
            components["relative_width"] = 0.08

        if (page.page_height and region.bbox[1] / page.page_height <= 0.55):
            components["position"] = 0.08

        nearest = self.nearest_body(region, page)
        if nearest:
            components["body_proximity"] = 0.13
            body_size = nearest[0].style.get("median_font_size")
        else:
            # Fallback solo per confrontare le dimensioni,
            # non per attribuire il body allo stesso articolo.
            sizes = [
                other.style.get("median_font_size")
                for other in page.regions
                if other.type == RegionType.ARTICLE_BODY
                    and other is not region
                    and not other.exclude_from_article_text
                    and other.style.get("median_font_size")
            ]
            body_size = median(sizes) if sizes else None

        title_size = features.median_font_size
        if title_size and body_size and body_size > 0:
            ratio = title_size / body_size
            prominence = min(max(ratio - 1.0, 0.0) / (self.config.relative_font_size_full_score - 1.0), 1.0)
            if prominence > 0:
                components["relative_font_size"] = 0.22 * prominence

        if (features.bold_ratio is not None and features.bold_evidence_fraction >= self.config.bold_min_evidence_fraction):
            components["bold"] = 0.12 * features.bold_ratio

        if self.related_region(region, page):
            components["near_author_or_subtitle"] = 0.10

        scored_entries = [
            (title_similarity(region.text or "", entry.title), entry)
            for entry in entries
            if not (
                entry.publication_date
                and page.source.publication_date
                and entry.publication_date
                != page.source.publication_date
            )
            and not (entry.original_page is not None and page.source.original_page is not None and entry.original_page != page.source.original_page)
        ]

        if scored_entries:
            similarity, entry = max(scored_entries, key=lambda item: item[0])
            if similarity >= self.config.index_min_similarity:
                components["review_index_title"] = (0.30 * similarity)
                best_entry_id = entry.id

        index_similarity = max((title_similarity(region.text or "", entry.title) for entry in entries), default=0.0)
        strong_index_title = index_similarity >= 0.84
        font_prominence = components.get("relative_font_size", 0.0)
        
        if region.type == RegionType.ARTICLE_BODY:
            accepted_body_seed = region.metadata.get("body_seed_evaluation", {}).get("accepted") is True
            
            if accepted_body_seed and features.word_count >= 35 and not strong_index_title:
                region.metadata["title_candidate_veto"] = "accepted_long_body_seed"
                return {}, None
            
            if not strong_index_title and font_prominence < 0.12:
                region.metadata["title_candidate_veto"] = "body_without_title_prominence"
                return {}, None
        
        return components, best_entry_id
    
    def consolidate_candidates(self, pages: list[PageRecord]) -> None:
        #Applica il vincolo solo ai gruppi con identita' articolo gia' disponibile
        
        groups: dict[tuple[str, str], list[Region]] = {}
        
        for page in pages:
            for region in page.regions:
                if (region.type != RegionType.ARTICLE_TITLE or "title_candidate" not in region.metadata):
                    continue
                
                article_id = (region.article_id or region.metadata.get("article_candidate_id"))
                if not article_id:
                    region.metadata["title_role"] = "unresolved_article_identity"
                    continue
                
                groups.setdefault((page.document_id, article_id), []).append(region)
                
        for (_, article_id), regions in groups.items():
            ordered = sorted(regions, key=lambda region: (-region.metadata["title_candidate"]["score"], region.metadata.get("region_id", "")))
            winner = ordered[0]
            runner_up = (ordered[1] if len(ordered) > 1 else None)
            
            if (runner_up and winner.metadata["title_candidate"]["score"] - runner_up.metadata["title_candidate"]["score"] < self.config.min_winning_margin):
                for region in ordered:
                    region.metadata["title_role"] = "ambiguous"
                continue
            
            winner.metadata["title_role"] = "main"
            winner.metadata["title_article_id"] = article_id
            
            for region in ordered[1:]:
                region.metadata["title_role"] = "not_main"
                region.metadata["title_article_id"] = article_id
                previous = region.metadata.get("type_before_title_resolution")
                region.type = (
                    RegionType(previous) if previous in {
                        RegionType.UNKNOWN.value,
                        RegionType.ARTICLE_BODY.value,
                        RegionType.ARTICLE_SECTION_HEADER.value,
                    }
                    else RegionType.UNKNOWN
                )
                
    def follows_body_in_same_column(self, region: Region, page: PageRecord) -> bool:
        return any(
            other is not region and other.type == RegionType.ARTICLE_BODY and other.bbox and not other.exclude_from_article_text and
            other.bbox[3] <= region.bbox[1] and self.horizontal_overlap(region, other) >= self.config.min_horizontal_overlap
            for other in page.regions
        )