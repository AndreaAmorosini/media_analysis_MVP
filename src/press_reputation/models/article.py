from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from press_reputation.models.page import SourceType, RegionType, SourceType

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
    
class ArticleRegionRef(BaseModel):
    region_id: str
    pdf_page: int
    type: RegionType
    order: int | None = None
    body_raw_charspan: tuple[int, int] | None = None
    subrole: str | None = None
    
class ExtractionCompleteness(BaseModel):
    ocr_text_retention: float | None = None
    body_assignment_coverage: float | None = None

    assigned_body_chars: int = 0
    unassigned_body_chars: int = 0
    unresolved_editorial_regions: int = 0
    unresolved_editorial_chars: int = 0
    unmapped_text_chars: int = 0

    index_entry_id: str | None = None
    incomplete_cross_page_chains: int = 0

    status: Literal["complete", "incomplete", "unknown"] = "unknown"
    reasons: list[str] = Field(default_factory=list)


class DocumentCompletenessReport(BaseModel):
    document_id: str
    index_article_coverage: float | None = None
    index_entries_total: int = 0
    index_entries_with_record: int = 0
    unmatched_index_entry_ids: list[str] = Field(default_factory=list)

    skipped_article_groups: list[dict[str, Any]] = Field(default_factory=list)
    possible_duplicate_record_pairs: list[dict[str, Any]] = Field(default_factory=list)
    orphan_editorial_region_ids: list[str] = Field(default_factory=list)
    unresolved_flow_links: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

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
    
    extraction_completeness: ExtractionCompleteness = Field(default_factory=ExtractionCompleteness)

    provenance: list[Provenance] = Field(default_factory=list)
    extraction_confidence: float | None = None
    reconstruction_confidence: float | None = None

    # Mantiene il vecchio campo e distingue pronto da da-rivedere.
    extraction: ExtractionInfo
    finalization_status: Literal["ready", "review_required"] = "review_required"
    finalization_warnings: list[str] = Field(default_factory=list)
    field_origins: dict[str, str] = Field(default_factory=dict)
    
    article_header: list[ArticleRegionRef] = Field(default_factory=list)
    article_content: list[ArticleRegionRef] = Field(default_factory=list)
    article_auxiliary: list[ArticleRegionRef] = Field(default_factory=list)

    # Alias esplicito del testo normalizzato; `body` resta per compatibilità.
    body_clean: str = ""