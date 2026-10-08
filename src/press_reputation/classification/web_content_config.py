from pydantic import BaseModel, Field


class WebContentConfig(BaseModel):
    seed_min_words: int = Field(default=25, ge=1)

    # relative alle dimensioni della pagina.
    minimum_column_width: float = Field(default=0.20, gt=0, lt=1)
    left_alignment_tolerance: float = Field(default=0.035, gt=0, lt=1)
    width_tolerance: float = Field(default=0.10, gt=0, lt=1)
    module_search_height: float = Field(default=0.22, gt=0, lt=1)

    minimum_horizontal_overlap: float = Field(default=0.60, gt=0, le=1)
    
    inline_module_max_height: float = Field(default=0.09, gt=0, lt=1)

    # Solo heading brevi; 
    module_markers: tuple[str, ...] = (
        "approfondimenti",
        "articoli correlati",
        "notizie correlate",
        "leggi anche",
        "ultime notizie",
        "video",
        "tutti i video",
    )
    
class InlineIntrusionConfig(BaseModel):
    min_score: float = Field(default=0.68, ge=0, le=1)
    max_words_for_short_signal: int = Field(default=45, ge=1)
    max_body_gap: float = Field(default=85.0, gt=0)
    min_horizontal_overlap: float = Field(default=0.55, gt=0, le=1)

    min_font_size_difference: float = Field(default=0.20, ge=0)
    min_trait_difference: float = Field(default=0.40, ge=0, le=1)
    min_style_evidence_fraction: float = Field(default=0.50, ge=0, le=1)

    max_lexical_similarity: float = Field(default=0.08, ge=0, le=1)