from pydantic import BaseModel

class RegionClassificationConfig(BaseModel):
    subtitle_max_gap_above: float = 60.0
    subtitle_max_gap_below: float = 80.0
    subtitle_min_horizontal_overlap: float = 0.25
    
    body_seed_min_words: int = 18
    body_continuation_threshold: float = 0.62
    
    location_exact_min_coverage: float = 0.60
    location_fuzzy_threshold_short: float = 0.95
    location_fuzzy_threshold_long: float = 0.85
    location_fuzzy_threshold_medium: float = 0.90
    
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