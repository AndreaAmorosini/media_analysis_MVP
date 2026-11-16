from datetime import date
from enum import Enum
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field

from press_reputation.ingestion.profiling.models import ExtractionMethod, PageExtractionProfile

# Modello per rappresentare una singola pagina di un documento estratta


class PageType(str, Enum):
    CLIPPING = "clipping"
    WEB = "web"
    PURE_TEXT = "pure_text"
    UNKNOWN = "unknown"
    INDEX = "index"


class SourceType(str, Enum):
    NEWSPAPER = "newspaper"
    WEB = "web"
    UNKNOWN = "unknown"


class RegionType(str, Enum):
    HEADER_METADATA = "header_metadata"
    SOURCE_NAME = "source_name"
    PRESS_REVIEW_PROVIDER = "press_review_provider"
    PUBLICATION_DATE = "publication_date"
    ORIGINAL_PAGE = "original_page"
    CLIPPING_SHEET = "clipping_sheet"
    LOCATION = "location"
    ARTICLE_SECTION_HEADER = "article_section_header"
    WATERMARK = "watermark"
    RIGHTS_NOTICE = "rights_notice"

    ARTICLE_TITLE = "article_title"
    ARTICLE_SUBTITLE = "article_subtitle"
    ARTICLE_BODY = "article_body"
    ARTICLE_KICKER = "article_kicker"
    AUTHOR = "author"

    IMAGE = "image"
    ARTICLE_POSITION_THUMBNAIL = "article_position_thumbnail"
    CAPTION = "caption"
    FOOTER = "footer"
    SIDEBAR = "sidebar"
    ADVERTISEMENT = "advertisement"
    NAVIGATION = "navigation"
    RELATED_CONTENT = "related_content"
    UNKNOWN = "unknown"

    PULL_QUOTE = "pull_quote"
    TABLE = "table"
    INFOGRAPHIC = "infographic"


class SourceInfo(BaseModel):
    name: str | None = None
    type: SourceType = SourceType.UNKNOWN
    publication_date: date | None = None
    original_page: int | None = None
    url: str | None = None


class ClippingInfo(BaseModel):
    sheet_current: int | None = None
    sheet_total: int | None = None
    surface_percent: float | None = None


class Region(BaseModel):
    type: RegionType = RegionType.UNKNOWN
    text: str | None = None
    bbox: list[float] | None = None
    raw_label: str | None = None
    extraction_method: ExtractionMethod = "unknown"
    extraction_profile: PageExtractionProfile | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)
    boilerplate: bool = False
    boilerplate_frequency: float | None = None
    exclude_from_article_text: bool = False
    article_id: str | None = None

    @property
    def has_valid_bbox(self) -> bool:
        return (
            self.bbox is not None
            and len(self.bbox) == 4
            and all(isfinite(x) for x in self.bbox)
            and self.bbox[2] > self.bbox[0]
            and self.bbox[3] > self.bbox[1]
        )


class PageRecord(BaseModel):
    document_id: str
    pdf_page: int
    page_type: PageType = PageType.UNKNOWN
    extraction_profile: PageExtractionProfile | None = None
    section: str | None = None
    page_width: float | None = None
    page_height: float | None = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    clipping: ClippingInfo | None = None
    regions: list[Region] = Field(default_factory=list)
