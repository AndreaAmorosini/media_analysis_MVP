import re
from difflib import SequenceMatcher

from press_reputation.config import AuthorResolutionConfig
from press_reputation.features import RegionFeatureExtractor
from press_reputation.lookup import normalize_entity_key
from press_reputation.models.page import (PageRecord, Region, RegionType)
from press_reputation.review_index.models import (ReviewIndexEntry, ReviewIndexMatch)

class AuthorResolver:
    BYLINE = re.compile(
        r"^\s*(?:(?:di|da)\s+|a\s+cura\s+di\s+)"
        r"(?P<name>.+?)\s*$",
        flags=re.IGNORECASE,
    )

    PARTICLES = {
        "de", "del", "della", "di", "da",
        "van", "von",
    }

    CANDIDATE_TYPES = {
        RegionType.UNKNOWN,
        RegionType.AUTHOR,
    }

    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation"
    }
    
    def __init__(self, config: AuthorResolutionConfig | None = None) -> None:
        self.config = config or AuthorResolutionConfig()
        self.features = RegionFeatureExtractor()
        
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
        normalized = normalize_entity_key(region.text)
        
        return (normalized not in self.config.editorial_heading_phrases and entity.get("kind") not in {"source", "provider", "location"} and
                not features.has_url and not features.has_foglio and not features.has_surface and not features.has_tiratura and
                not features.has_diffusione and not features.has_rights_notice_marker and not features.has_watermark_marker and
                not features.has_related_marker and not features.has_navigation_marker)
        
    def parsed_name(self, text:str) -> tuple[str, bool]:
        match = self.BYLINE.match(text)
        if match:
            return match.group("name").strip(), True
        
        return text.strip(), False
    
    def name_like(self, name: str) -> bool:
        tokens = name.split()
        if not (self.config.min_name_tokens <= len(tokens) <= self.config.max_name_tokens):
            return False
        
        if any (not re.fullmatch(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", token, flags=re.UNICODE) for token in tokens):
            return False
        
        significant = [token for token in tokens if token.casefold() not in self.PARTICLES]
        if len(significant) < 2:
            return False
        
        return all(token[0].isupper() for token in significant)
    
    def nearest_header(self, candidate: Region, page: PageRecord) -> Region | None:
        headers = []
        
        for region in page.regions:
            if (region.type not in {RegionType.ARTICLE_TITLE, RegionType.ARTICLE_SUBTITLE} or not region.bbox or region.exclude_from_article_text or
                region.metadata.get("title_role") in {"not_main", "ambiguous"}):
                continue
            
            gap = (candidate.bbox[1] - region.bbox[3])
            if (0 <= gap <= self.config.max_gap_from_header and self.overlap(candidate, region) >= self.config.min_horizontal_overlap):
                headers.append((gap, region))
                
        return (min(headers, key=lambda item: item[0])[1] if headers else None)
    
    def body_after(self, candidate: Region, page: PageRecord) -> Region | None:
        bodies = [region for region in page.regions if (region.type == RegionType.ARTICLE_BODY and region.bbox and
                                                        not region.exclude_from_article_text and
                                                        region.metadata.get("content_scope") not in self.BLOCKED_SCOPES and
                                                        region.bbox[1] >= candidate.bbox[3] and
                                                        self.overlap(candidate, region) >= self.config.min_horizontal_overlap)]
        
        return min(bodies, key=lambda region: region.bbox[1], default=None)
    
    def body_already_started(self, candidate: Region, header: Region, page: PageRecord) -> bool:
        return any(region.type == RegionType.ARTICLE_BODY and region.bbox and not region.exclude_from_article_text and
                    region.bbox[1] >= header.bbox[3] and region.bbox[3] <= candidate.bbox[1] and
                    self.overlap(candidate, region) >= self.config.min_horizontal_overlap for region in page.regions)
        
    @staticmethod
    def index_prior(header: Region | None, matches: list[ReviewIndexMatch], entries_by_id: dict[str, ReviewIndexEntry]) -> tuple[ReviewIndexEntry, str] | None:
        if header is None:
            return None
        
        title_id = header.metadata.get("region_id")
        
        if header.type == RegionType.ARTICLE_SUBTITLE:
            title_id = (header.metadata.get("subtitle_resolution", {}).get("title_region_id"))
            
        for match in matches:
            if(match.status == "matched" and match.title_region_id == title_id):
                entry = entries_by_id.get(match.entry_id)
                if entry and entry.author:
                    return entry, entry.author
                
        return None
    
    def enrich(self, page: PageRecord, matches: list[ReviewIndexMatch], entries_by_id: dict[str, ReviewIndexEntry]) -> PageRecord:
        page_matches = [match for match in matches if match.pdf_page == page.pdf_page]
        
        for region in page.regions:
            if not self.eligible(region, page):
                continue
            
            name, explicit = self.parsed_name(region.text)
            name_shaped = self.name_like(name)
            header = self.nearest_header(region, page)
            
            if not explicit and header is None:
                continue
            
            if (header is not None and self.body_already_started(region, header, page)):
                continue
            
            following_body = self.body_after(region, page)
            prior = self.index_prior(header, page_matches, entries_by_id)
            
            components: dict[str, float] = {}
            
            if explicit:
                components["explicit_marker"] = 0.48
                
            if name_shaped:
                components["name_pattern"] = 0.22
            elif not explicit:
                if prior is None:
                    continue
                
            if header is not None:
                components["near_title_or_subtitle"] = 0.20
                
            if following_body is not None:
                components["before_body"] = 0.14
                
                body_font = (following_body.style.get("dominant_font"))
                author_font = (region.style.get("dominant_font"))
                
                if (body_font and author_font and body_font != author_font):
                    components["font_differs_from_body"] = 0.08
                    
            italic = region.style.get("italic_ratio")
            evidence = region.style.get("italic_evidence_fraction", 0.0)
            
            if (italic is not None and italic >= 0.5 and evidence >= self.config.min_style_evidence_fraction):
                components["italic"] = 0.10
                
            if prior is not None:
                entry, index_author = prior
                similarity = SequenceMatcher(None, normalize_entity_key(name), normalize_entity_key(index_author)).ratio()
                
                if (similarity >= self.config.author_index_min_similarity):
                    components["review_index_author"] = 0.25
                    
            score = round(sum(components.values()), 4)
            required = self.config.explicit_min_score if explicit else self.config.implicit_min_score
            
            if score < required:
                continue
            
            previous_type = region.type.value
            region.type = RegionType.AUTHOR
            region.metadata["author_resolution"] = {
                "method": ("explicit_byline" if explicit else "implicit_name"),
                "score": score,
                "components": components,
                "name_candidate": name,
                "previous_type": previous_type,
                "header_region_id": (header.metadata.get("region_id") if header else None),
                "review_index_entry_id": (prior[0].id if prior else None),
                "index_author_similarity": (similarity if prior is not None else None)
            }
            
            if header is not None:
                article_id = (header.article_id or header.metadata.get("article_candidate_id"))
                if article_id:
                    region.metadata.setdefault("article_candidate_id", article_id)
                    
        return page
