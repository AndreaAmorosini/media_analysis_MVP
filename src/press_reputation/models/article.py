from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from press_reputation.models.page import SourceType

#Modello per rappresentare un articolo che puo' essere composto da piu' pagine

class ArticleSource(BaseModel):
    source_type: SourceType = SourceType.UNKNOWN
    source: Optional[str] = None

class Media(BaseModel):
    type: str
    page: int
    region_id: str | None = None
    bbox: list[float] | None = None
    text: str | None = None
    linked_media_region_id: str | None = None
    extraction_method: str | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class Provenance(BaseModel):
    page: int
    bbox: Optional[list[float]] = None
    content_type: str
    region_id: str | None = None
    method: str | None = None
    extraction_method: str | None = None
    article_charspan: tuple[int, int] | None = None
    source_refs: list[dict[str, Any]] = Field(default_factory=list)


class ExtractionInfo(BaseModel):
    method: str
    confidence: Optional[float] = None
    warnings: list[str] = Field(default_factory=list)


class SectionHeader(BaseModel):
    text: str
    page: int
    region_id: str
    order: int | None = None
    body_raw_charspan: tuple[int, int] | None = None

class ArticleRecord(BaseModel):
    id: str
    document_id: str

    source: ArticleSource = Field(default_factory=ArticleSource)
    publication_date: Optional[date] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    original_pages: list[int] = Field(default_factory=list)
    pdf_pages: list[int]
    url: Optional[str] = None

    body_raw: str = ""
    body: str = ""
    section_headers: list[SectionHeader] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)
    article_type: Literal["newspaper", "web", "unknown"] = "unknown"

    provenance: list[Provenance] = Field(default_factory=list)
    extraction_confidence: float | None = None
    reconstruction_confidence: float | None = None

    # Mantiene il vecchio campo e distingue pronto da da-rivedere.
    extraction: ExtractionInfo
    finalization_status: Literal["ready", "review_required"] = "review_required"
    finalization_warnings: list[str] = Field(default_factory=list)
    field_origins: dict[str, str] = Field(default_factory=dict)