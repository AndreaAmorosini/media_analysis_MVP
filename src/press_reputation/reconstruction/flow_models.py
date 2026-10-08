from typing import Literal
from pydantic import BaseModel, Field

from press_reputation.models.page import RegionType

class FlowLink(BaseModel):
    document_id: str
    article_candidate_id: str
    from_pdf_page: int
    to_pdf_page: int
    
    status: Literal["candidate", "accepted", "rejected"]
    method: str = "boundary_rules_v1"
    
    evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    
    from_region_id: str | None = None
    to_region_id: str | None = None
    confidence: float | None = None
    
class ReadingOrderSegment(BaseModel):
    article_id: str
    region_id: str
    type: RegionType
    pdf_page: int

    column: int | None = None
    order: int

    bbox: list[float] | None = None
    text: str
    confidence: float

    method: str = "article_columns_v1"
    provenance: list[dict] = Field(default_factory=list)


class ArticleReadingOrder(BaseModel):
    document_id: str
    article_id: str
    segments: list[ReadingOrderSegment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    
class DraftSegment(BaseModel):
    region_id: str
    pdf_page: int
    bbox: list[float] | None = None
    text: str
    
    article_charspan: tuple[int, int]
    
    selection_status: Literal["main", "candidate"]
    provenance: list[dict] = Field(default_factory=list)
    
    type: RegionType = RegionType.ARTICLE_BODY
    column: int | None = None
    order: int | None = None
    confidence: float | None = None
    
class ArticleDraft(BaseModel):
    id: str
    document_id: str
    pdf_pages: list[int]
    
    assembly_status: Literal["candidate"] = "candidate"
    title: str | None = None
    body: str = ""
    
    segments: list[DraftSegment] = Field(default_factory=list)
    links: list[FlowLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)