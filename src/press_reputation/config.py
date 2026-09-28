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