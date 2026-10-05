from press_reputation.config import SectionHeaderResolutionConfig
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import (PageRecord, Region, RegionType)

class SectionHeaderResolver:
    CANDIDATE_TYPES = {
        RegionType.UNKNOWN,
        RegionType.ARTICLE_SECTION_HEADER,
        RegionType.ARTICLE_BODY
    }
    
    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main"
    }
    
    def __init__(self, config: SectionHeaderResolutionConfig | None = None) -> None:
        self.config = config or SectionHeaderResolutionConfig()
        self.features = RegionFeatureExtractor()
        
    @staticmethod
    def identity(region: Region) -> str | None:
        return (region.article_id or region.metadata.get("article_candidate_id"))
    
    @staticmethod
    def overlap(left: Region, right: Region) -> float:
        if not left.bbox or not right.bbox:
            return 0.0
        
        shared = max(0.0, min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0]))
        narrower = min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0])
        
        return shared / narrower if narrower > 0 else 0.0
    
    def eligible(self, region: Region, page: PageRecord) -> bool:
        if (region.type not in self.CANDIDATE_TYPES or not region.text or not region.bbox or len(region.bbox) != 4 or region.exclude_from_article_text or
            region.metadata.get("in_header_metadata_zone") or region.metadata.get("inside_article_position_thumbnail") or
            region.metadata.get("content_scope") in self.BLOCKED_SCOPES):
            return False
        
        features = self.features.extract(region, page, include_entity=False)
        entity = region.metadata.get("entity_lookup") or {}
        
        return (1 <= features.word_count <= self.config.max_words and features.text_length <= self.config.max_text_chars and not
                features.has_url and not features.has_author_marker and not features.has_foglio and not features.has_surface and not
                features.has_rights_notice_marker and not features.has_watermark_marker and entity.get("kind") not in {"source", "provider", "location"})
        
    def body_pair(self, candidate: Region, page: PageRecord) -> tuple[Region, Region] | None:
        before: list[Region] = []
        after: list[Region] = []
        
        for body in page.regions:
            if (body is candidate or body.type != RegionType.ARTICLE_BODY or not body.bbox or body.exclude_from_article_text or
                body.metadata.get("content_scope") in self.BLOCKED_SCOPES or self.overlap(body, candidate) < self.config.min_body_horizontal_overlap):
                continue
            
            if (body.bbox[3] <= candidate.bbox[1] and candidate.bbox[1] - body.bbox[3] <= self.config.max_gap_from_previous_body):
                before.append(body)
                
            if (body.bbox[1] >= candidate.bbox[3] and body.bbox[1] - candidate.bbox[3] <= self.config.max_gap_to_next_body):
                after.append(body)
                
        if not before or not after:
            return None
        
        previous = max(before, key=lambda body: body.bbox[3])
        following = min(after, key=lambda body: body.bbox[1])
        
        if (self.overlap(previous, following) < self.config.min_body_horizontal_overlap):
            return None
        
        return previous, following
    
    def association_status(self, candidate: Region, previous: Region, following: Region) -> tuple[str, str | None] | None:
        previous_id = self.identity(previous)
        following_id = self.identity(following)
        candidate_id = self.identity(candidate)
        
        if previous_id or following_id:
            if (not previous_id or previous_id != following_id or (candidate_id and candidate_id != previous_id)):
                return None
            
            return "confirmed_id", previous_id
        
        if candidate_id:
            return None
        
        return "provisional_local_body_pair", None
    
    def crossed_by_another_title(self, candidate: Region, previous: Region, following: Region, page: PageRecord) -> bool:
        for region in page.regions:
            if (region is candidate or region.type != RegionType.ARTICLE_TITLE or not region.bbox or region.metadata.get("title_role") == "not_main"):
                continue
            
            if (previous.bbox[3] <= region.bbox[1] and region.bbox[3] <= following.bbox[1] and
                self.overlap(candidate, region) >= self.config.min_body_horizontal_overlap):
                return True
            
        return False
    
    def heading_style(self, candidate: Region, previous: Region, following: Region) -> tuple[bool, dict[str, float]]:
        components: dict[str, float] = {}
        sizes = [body.style.get("median_font_size") for body in (previous, following) if body.style.get("median_font_size")]
        heading_size = candidate.style.get("median_font_size")
        
        if sizes and heading_size:
            body_size = sum(sizes) / len(sizes)
            if (heading_size / body_size >= self.config.min_font_size_ratio):
                components["larger_than_body"] = 0.16
                
        for trait in ("bold", "italic"):
            ratio = candidate.style.get(f"{trait}_ratio")
            evidence = candidate.style.get(f"{trait}_evidence_fraction", 0.0)
            
            if (ratio is not None and evidence >= self.config.min_style_evidence_fraction and ratio >= 0.5):
                components[f"{trait}_evidence"] = 0.10
                
        
        return bool(components), components
    
    def enrich(self, page: PageRecord) -> PageRecord:
        for candidate in page.regions:
            if not self.eligible(candidate, page):
                continue
            
            pair = self.body_pair(candidate, page)
            if pair is None:
                continue
            
            previous, following = pair
            association = self.association_status(candidate, previous, following)
            if association is None:
                continue
            
            status, article_id = association
            
            if self.crossed_by_another_title(candidate, previous, following, page):
                continue
            
            has_style, style_components = self.heading_style(candidate, previous, following)
            
            raw_heading = (candidate.raw_label == "section_header")
            
            if not has_style and not raw_heading:
                continue
            
            components = {
                "body_before_and_after": 0.35,
                "same_body_band": 0.20,
                "short_region": 0.08,
                **style_components
            }
            
            if raw_heading:
                components["docling_heading"] = 0.20
                
            score = round(sum(components.values()), 4)
            
            required = (self.config.min_score_for_body_reclassification if candidate.type == RegionType.ARTICLE_BODY else self.config.min_score)
            
            if score < required:
                continue
            
            previous_type = candidate.type.value
            candidate.type = RegionType.ARTICLE_SECTION_HEADER
            candidate.metadata["section_header_resolution"] = {
                "method": "body_sandwitch_v1",
                "score": score,
                "components": components,
                "previous_type": previous_type,
                "association_status": status,
                "article_id_priority": article_id,
                "previous_body_region_id": previous.metadata.get("region_id"),
                "following_body_region_id": following.metadata.get("region_id")
            }
            
            if article_id and not self.identity(candidate):
                if (previous.article_id and previous.article_id == following.article_id):
                    candidate.article_id = article_id
                else:
                    candidate.metadata["article_candidate_id"] = article_id
                    
        return page
