from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from press_reputation.models.page import SourceType

#Modello per rappresentare un articolo che puo' essere composto da piu' pagine

class ArticleSource(BaseModel):
    source_type: SourceType = SourceType.UNKNOWN
    source: Optional[str] = None

class Media(BaseModel):
    type: str
    page: int


class Provenance(BaseModel):
    page: int
    bbox: Optional[list[float]] = None
    content_type: str


class ExtractionInfo(BaseModel):
    method: str
    confidence: Optional[float] = None
    warnings: list[str] = Field(default_factory=list)


class ArticleRecord(BaseModel):
    id: str
    document_id: str
    source: ArticleSource = Field(default_factory=ArticleSource)
    publication_date: Optional[date] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    original_pages: list[int] = Field(default_factory=list)
    url: Optional[str] = None
    pdf_pages: list[int]
    body: str = ""
    media: list[Media] = Field(default_factory=list)
    extraction: ExtractionInfo
    provenance: list[Provenance] = Field(default_factory=list)