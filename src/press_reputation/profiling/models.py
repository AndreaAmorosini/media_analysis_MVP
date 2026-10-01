from typing import Literal
from pydantic import BaseModel, Field

ExtractionKind = Literal["native_pdf", "ocr", "mixed", "image_only"]
ExtractionMethod = Literal["pdf_text", "ocr", "unknown"]

class PageExtractionProfile(BaseModel):
    pdf_page: int
    kind: ExtractionKind | None = None
    
    native_word_count: int = 0
    native_body_word_count: int = 0
    image_coverage: float = 0.0
    
    run_ocr: bool = False
    decision_reasons: list[str] = Field(default_factory=list)
    ocr_status: Literal["not_requested", "completed", "failed"] = "not_requested"
    warnings: list[str] = Field(default_factory=list)
    
class DocumentProfile(BaseModel):
    pages: dict[int, PageExtractionProfile] = Field(default_factory=dict)