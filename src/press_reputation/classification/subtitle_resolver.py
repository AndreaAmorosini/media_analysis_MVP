from statistics import median

from press_reputation.config import SubtitleResolutionConfig
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import (PageRecord, Region, RegionType)

class SubtitleResolver:
    CANDIDATE_TYPES = {
        RegionType.UNKNOWN,
        RegionType.ARTICLE_BODY,
        RegionType.ARTICLE_SUBTITLE,
    }
    
    def __init__(self, config: SubtitleResolutionConfig | None = None) -> None:
        self.config = config or SubtitleResolutionConfig()
        self.features = RegionFeatureExtractor()
        
    @staticmethod
    def overlap(left: Region, right: Region) -> float:
        if not left.bbox or not right.bbox:
            return 0.0
        
        shared = max(0.0, min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0]))
        narrower = min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0])
        
        return shared / narrower if narrower > 0 else 0.0
    
    def eligible(self, region: Region) -> bool:
        if (region.type not in self.CANDIDATE_TYPES or not (region.text or "").strip() or not region.bbox or len(region.bbox) != 4 or
            region.exclude_from_article_text or region.metadata.get("in_header_metadata_zone") or region.metadata.get("inside_article_position_thumbnail")):
            return False
        
        if region.metadata.get("content_scope") in {"related", "advertisement", "boilerplate", "non_main", "navigation"}:
            return False
        
        entity = region.metadata.get("entity_lookup") or {}
        if entity.get("kind") in {"source", "provider"}:
            return False
        
        return True
    
    def body_anchor(self, title: Region, page: PageRecord) -> Region | None:
        if not title.bbox:
            return None
        
        bodies = [region for region in page.regions if(region.type == RegionType.ARTICLE_BODY and
                                                        region is not title and region.bbox and not
                                                        region.exclude_from_article_text and not
                                                        region.metadata.get("in_header_metadata_zone") and
                                                        region.bbox[1] >= title.bbox[3] and
                                                        self.overlap(region, title) >= self.config.min_horizontal_overlap)]
        
        return min(bodies, key=lambda region: region.bbox[1], default=None)
    
    def score(self, candidate: Region, title: Region, page: PageRecord, body: Region | None) -> tuple[float, dict[str, float], str] | None:
        if not candidate.bbox or not title.bbox:
            return None
        
        above = title.bbox[1] - candidate.bbox[3]
        below = candidate.bbox[1] - title.bbox[3]
        
        if 0 <= above <= self.config.max_gap_above:
            side = "above"
            gap = above
            max_gap = self.config.max_gap_above
        elif 0 <= below <= self.config.max_gap_below:
            side = "below"
            gap = below
            max_gap = self.config.max_gap_below
        else:
            return None
        
        horizontal = self.overlap(candidate, title)
        if horizontal < self.config.min_horizontal_overlap:
            return None
        
        if (body is not None and candidate.bbox[1] >= body.bbox[1]):
            return None
        
        if (candidate.type == RegionType.ARTICLE_BODY and body is None):
            return None
        
        features = self.features.extract(candidate, page, include_entity = False)
        
        if (features.has_url or features.has_foglio or features.has_surface or features.has_tiratura or features.has_diffusione or
            features.has_rights_notice_marker or features.has_watermark_marker or features.has_author_marker):
            return None
        
        components = {
            "title_proximity": (0.22 * (1 - gap / max_gap)),
            "horizontal_overlap": (0.18 * horizontal),
        }
        
        title_width = (title.bbox[2] - title.bbox[0])
        candidate_width = (candidate.bbox[2] - candidate.bbox[0])
        
        if title_width > 0:
            components["relative_width"] = (0.08 * min(candidate_width / title_width, 1.0))
            
        if body is not None:
            components["before_body"] = 0.15
            body_size = body.style.get("median_font_size")
            subtitle_size = (features.median_font_size)
            
            if body_size and subtitle_size:
                ratio = subtitle_size / body_size
                if 1.0 <= ratio <= (self.config.max_font_to_body_ratio):
                    components["font_relative_to_body"] = (0.15 * min((ratio - 1.0) / 0.4, 1.0))
                    
        if (features.bold_ratio is not None and features.bold_evidence_fraction >= self.config.min_style_evidence_fraction):
            components["bold"] = (0.08 * features.bold_ratio)
            
        if (features.italic_ratio is not None and features.italic_evidence_fraction >= self.config.min_style_evidence_fraction):
            components["italic"] = (0.05 * features.italic_ratio)
            
        if candidate.raw_label == "text":
            components["docling_text"] = 0.04
            
        if candidate.raw_label == "section_header":
            components["heading_ambiguity"] = -0.08
            
        return (round(sum(components.values()), 4), components, side)
    
    def enrich(self, page: PageRecord) -> PageRecord:
        titles = [region for region in page.regions if region.type == RegionType.ARTICLE_TITLE and region.bbox and not
                    region.exclude_from_article_text and not region.metadata.get("in_header_metadata_zone") and
                    region.metadata.get("title_role") not in {"not_main", "ambiguous"}]
        
        proposals: dict[int, list[tuple[float, Region, Region, str, dict]]] = {}
        
        for title in titles:
            body = self.body_anchor(title, page)
            
            for candidate in page.regions:
                if (candidate is title or not self.eligible(candidate)):
                    continue
                
                result = self.score(candidate, title, page, body)
                
                if result is None:
                    continue
                
                score, components, side = result
                required = (self.config.min_score if body is not None else self.config.min_score_without_body_anchor)
                if score < required:
                    continue
                
                proposals.setdefault(id(candidate), []).append((score, candidate, title, side, components))
                
        assigned: dict[int, list[tuple]] = {}
        
        for candidate_proposal in proposals.values():
            ranked = sorted(candidate_proposal, key=lambda item: -item[0])
            best = ranked[0]
            
            if (len(ranked) > 1 and best[0] - ranked[1][0] < self.config.min_assignment_margin):
                best[1].metadata["subtitle_resolution"] = {
                    "status": "ambiguous_title_anchor",
                    "candidate_scores": [item[0] for item in ranked] 
                }
                continue
            
            assigned.setdefault(id(best[2]), []).append(best)
            
        for title_proposals in assigned.values():
            for (score, candidate, title, side, components) in sorted(title_proposals, key=lambda item: -item[0])[:self.config.max_subtitles_per_title]:
                previous_type = candidate.type.value
                candidate.type = RegionType.ARTICLE_SUBTITLE
                candidate.metadata["subtitle_resolution"] = {
                    "status": "accepted",
                    "method": "subtitle_resolver_v1",
                    "score": score,
                    "components": components,
                    "side": side,
                    "title_region_id": (title.metadata.get("region_id")),
                    "previous_type": previous_type,
                }
                
        return page
