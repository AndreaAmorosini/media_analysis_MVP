from pydantic import BaseModel

class RegionClassificationConfig(BaseModel):
    subtitle_max_gap_above: float = 60.0
    subtitle_max_gap_below: float = 80.0
    subtitle_min_horizontal_overlap: float = 0.25
    
    body_seed_min_words: int = 18
    body_continuation_threshold: float = 0.62
        
    boilerplate_min_page_frequency: float = 0.50
    
    watermark_opacity_threshold: float = 0.55
    
class DocumentProfilingConfig(BaseModel):
    min_native_body_words: int = 30
    min_image_coverage_without_text: float = 0.10
    min_image_coverage_with_sparse_text: float = 0.35
    
    header_fraction: float = 0.16
    footer_fraction: float = 0.10
    
    ocr_dpi: int = 200
    
    duplicate_bbox_overlap: float = 0.60
    duplicate_text_similarity: float = 0.85
    
class ReviewIndexConfig(BaseModel):
    max_initial_pages: int = 3
    min_valid_rows: int = 2
    
    min_title_similarity: float = 0.68
    strong_title_similarity: float = 0.84
    min_match_score: float = 0.72
    min_score_margin: float = 0.10
    
    max_start_page_offset: int = 2
    max_continuation_pages: int = 3
    
class HeaderMetadataConfig(BaseModel):
    max_header_relative_height: float = 0.18
    padding: float = 10.0
    
    require_full_region_inside_zone: bool = True
    classify_explicit_seeds_without_bbox: bool = True
    
class TitleResolutionConfig(BaseModel):
    min_score: float = 0.38
    min_winning_margin: float = 0.08
    index_min_similarity: float = 0.68
    
    relative_font_size_full_score: float = 1.45
    bold_min_evidence_fraction: float = 0.50
    
    max_body_gap_relative: float = 0.12
    max_related_gap_relative: float = 0.10
    min_horizontal_overlap: float = 0.20
    
class EntityLookupConfig(BaseModel):
    max_entity_text_chars: int = 120
    max_entity_words: int = 10
    
    min_fuzzy_name_chars: int = 6
    fuzzy_source_threshold: float = 0.88
    fuzzy_provider_threshold: float = 0.88
    
    location_short_max_chars: int = 5
    location_medium_max_chars: int = 8
    location_fuzzy_short: float = 0.95
    location_fuzzy_medium: float = 0.90
    location_fuzzy_long: float = 0.85
    
    location_min_match_coverage: float = 0.60
    
    min_fuzzy_winner_margin: float = 0.04
    
class SubtitleResolutionConfig(BaseModel):
    max_gap_above: float = 60.0
    max_gap_below: float = 80.0
    min_horizontal_overlap: float = 0.25
    
    min_score: float = 0.55
    min_score_without_body_anchor: float = 0.70
    min_assignment_margin: float = 0.08
    
    max_font_to_body_ratio: float = 1.75
    min_style_evidence_fraction: float = 0.50
    max_subtitles_per_title: int = 2
    
class SectionHeaderResolutionConfig(BaseModel):
    max_words: int = 10
    max_text_chars: int = 100
    
    min_body_horizontal_overlap: float = 0.45
    max_gap_from_previous_body: float = 120.0
    max_gap_to_next_body: float = 100.0
    
    min_font_size_ratio: float = 1.10
    min_style_evidence_fraction: float = 0.50
    
    min_score: float = 0.60
    min_score_for_body_reclassification: float = 0.72