from datetime import date
from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field

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
    
    ARTICLE_TITLE = "article_title"
    ARTICLE_SUBTITLE = "article_subtitle"
    ARTICLE_BODY = "article_body"
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


class SourceInfo(BaseModel):
    name: Optional[str] = None
    type: SourceType = SourceType.UNKNOWN
    publication_date: Optional[date] = None
    original_page: Optional[int] = None
    url: Optional[str] = None


class ClippingInfo(BaseModel):
    sheet_current: Optional[int] = None
    sheet_total: Optional[int] = None
    surface_percent: Optional[float] = None


class Region(BaseModel):
    type: RegionType = RegionType.UNKNOWN
    text: Optional[str] = None
    bbox: Optional[list[float]] = None
    raw_label: Optional[str] = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PageRecord(BaseModel):
    document_id: str
    pdf_page: int
    page_type: PageType = PageType.UNKNOWN
    section: Optional[str] = None
    page_width: Optional[float] = None
    page_height: Optional[float] = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    clipping: Optional[ClippingInfo] = None
    regions: list[Region] = Field(default_factory=list)