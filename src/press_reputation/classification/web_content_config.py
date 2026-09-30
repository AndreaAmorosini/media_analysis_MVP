from pydantic import BaseModel, Field


class WebContentConfig(BaseModel):
    seed_min_words: int = Field(default=25, ge=1)

    # relative alle dimensioni della pagina.
    minimum_column_width: float = Field(default=0.20, gt=0, lt=1)
    left_alignment_tolerance: float = Field(default=0.035, gt=0, lt=1)
    width_tolerance: float = Field(default=0.10, gt=0, lt=1)
    module_search_height: float = Field(default=0.22, gt=0, lt=1)

    minimum_horizontal_overlap: float = Field(default=0.60, gt=0, le=1)

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