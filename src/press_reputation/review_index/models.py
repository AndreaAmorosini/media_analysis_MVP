from datetime import date
from pydantic import BaseModel, Field

class ReviewIndexEntry(BaseModel):
    id: str
    document_id: str
    
    publication_date: date | None = None
    source: str | None = None
    original_page: int | None = None
    title: str
    author: str | None = None
    review_start_page: int | None = None
    category: str | None = None
    
    index_pdf_page: int
    table_ref: str
    row_number: int
    provenance: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    
class ReviewIndexMatch(BaseModel):
    entry_id: str
    pdf_page: int
    title_region_id: str | None = None
    score: float
    title_similarity: float | None = None
    evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    status: str = "candidate"