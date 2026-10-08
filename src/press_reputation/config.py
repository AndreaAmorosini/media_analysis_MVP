from pydantic import BaseModel, Field

class RegionClassificationConfig(BaseModel):
    subtitle_max_gap_above: float = 60.0
    subtitle_max_gap_below: float = 80.0
    subtitle_min_horizontal_overlap: float = 0.25
    
    body_seed_min_words: int = 18
    body_seed_min_font_ratio: float = 0.75
    body_seed_max_font_ratio: float = 1.30
    body_seed_max_bold_ratio: float = 0.70
    body_seed_min_bold_evidence_fraction: float = 0.50
    
    body_continuation_threshold: float = 0.62
    
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
    
class AuthorResolutionConfig(BaseModel):
    min_name_tokens: int = 2
    max_name_tokens: int = 5
    
    max_gap_from_header: float = 110.0
    min_horizontal_overlap: float = 0.20
    
    implicit_min_score: float = 0.72
    explicit_min_score: float = 0.70
    author_index_min_similarity: float = 0.92
    min_style_evidence_fraction: float = 0.50
    
    editorial_heading_phrases: set[str] = Field(
        default_factory=lambda: {
            "archivio storcio",
            "approfondimenti",
            "finalità educative",
            "tra tradizione e innovazione",
            "il confronto"
        }
    )
    
class WatermarkDetectionConfig(BaseModel):
    min_score: float = 0.65
    
    low_opacity_threshold: float = 0.55
    repeated_page_fraction: float = 0.50
    min_document_pages_for_repetition: int = 2
    
    minimum_editorial_overlap: int = 2
    
    edge_fraction_x: float = 0.06
    edge_fraction_y: float = 0.10
    
    maximum_strong_marker_chars: int = 120
    maximum_weak_marker_chars: int = 200
    
class DocumentBoilerplateConfig(BaseModel):
    min_distinct_pages: int = 2
    min_page_frequency: float = 0.50

    min_generic_text_chars: int = 8
    max_region_text_chars: int = 180
    max_generic_words: int = 12

    # Distanza massima fra coordinate relative della stessa regione su due pagine.
    max_relative_position_delta: float = 0.045
    max_relative_size_delta: float = 0.06

    # Confrontati solo se entrambi gli stili sono noti.
    max_font_size_relative_difference: float = 0.20
    
class ArticleClusteringConfig(BaseModel):
    min_assignment_score: float = 0.62
    min_winning_margin: float = 0.12

    min_horizontal_overlap: float = 0.25
    max_local_gap_fraction: float = 0.12
    max_media_gap_fraction: float = 0.18

    max_column_edge_difference: float = 0.06

    max_font_size_ratio_difference: float = 0.25
    max_caption_image_gap_fraction: float = 0.04

class BodyContinuationConfig(BaseModel):
    min_score: float = 0.70
    min_winning_margin: float = 0.12

    min_horizontal_overlap: float = 0.45
    max_column_left_difference: float = 30.0

    max_forward_gap: float = 120.0
    max_vertical_overlap: float = 4.0

    min_style_evidence_fraction: float = 0.50
    max_heading_candidate_words: int = 10
    
class ArticleReadingOrderConfig(BaseModel):
    column_left_tolerance: float = 18.0
    min_column_overlap: float = 0.55

    wide_region_width_ratio: float = 1.55
    spanning_column_overlap: float = 0.35

    # Usato per segnalare un salto locale, non per
    # scartare testo dall'articolo.
    suspicious_vertical_gap: float = 120.0
    
class NewspaperContinuationConfig(BaseModel):
    min_candidate_score: float = 0.50
    min_accept_score: float = 0.74
    repeated_title_similarity: float = 0.84
    index_title_similarity: float = 0.84

    max_pdf_page_gap: int = 1