from press_reputation.models.page import PageRecord, RegionType, Region
from dataclasses import dataclass, field
from math import isfinite
from statistics import median

def valid_bbox(region: Region) -> bool:
    bbox = region.bbox
    
    return (bbox is not None and len(bbox) == 4 and all(isfinite(coord) for coord in bbox) and bbox[2] > bbox[0] and bbox[3] > bbox[1])

def horizontal_overlap(a: list[float], b: list[float]) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    denominator = min(a[2] - a[0], b[2] - b[0])
    
    return overlap / denominator if denominator > 0 else 0.0

@dataclass
class BodyGroupingConfig:
    left_alignment_tolerance: float = 12.0
    maximum_vertical_gap: float = 18.0
    maximum_vertical_overlap: float = 4.0
    
    minimum_horizontal_overlap: float = 0.65
    minimum_font_size_ratio: float = 0.85
    maximum_style_ratio_difference: float = 0.25
    
@dataclass
class Column:
    article_id: str | None
    regions: list[Region] = field(default_factory=list)
    
    @property
    def left(self) -> float:
        return median(region.bbox[0] for region in self.regions)
    
    @property
    def right(self) -> float:
        return median(region.bbox[2] for region in self.regions)
    
    @property
    def reference_bbox(self) -> list[float]:
        return [self.left, 0.0, self.right, 1.0]

class BodyGroupingResolver:
    
    SEPARATORS = {
        RegionType.ARTICLE_TITLE,
        RegionType.ARTICLE_SUBTITLE,
        RegionType.ARTICLE_SECTION_HEADER,
        RegionType.AUTHOR,
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.ADVERTISEMENT
    }
    
    def __init__(self, config: BodyGroupingConfig | None = None) -> None:
        self.config = config or BodyGroupingConfig()

    def enrich(self, page: PageRecord) -> PageRecord:
        for region in page.regions:
            region.metadata.pop("body_group_id", None)
            region.metadata.pop("body_reading_order", None)
            region.metadata.pop("body_column_id", None)
            region.metadata.pop("body_order_method", None)
        
        bodies = [region for region in page.regions if region.type == RegionType.ARTICLE_BODY and region.text and valid_bbox(region)
                  and not region.exclude_from_article_text and not region.metadata.get("inside_article_position_thumbnail") and
                  region.metadata.get("content_scope") not in {"related", "advertisement", "boilerplate", "non_main"}]
        
        columns: list[Column] = []
        
        for region in sorted(bodies, key=lambda item: (item.bbox[0], item.bbox[1])):
            candidates = [column for column in columns if self.matches_column(region, column)]
            
            if candidates:
                selected = min(candidates, key=lambda column: abs(region.bbox[0] - column.left))
                selected.regions.append(region)
            else:
                columns.append(Column(article_id=region.article_id, regions=[region]))
                
        group_number = 0
        reading_order = 0
        
        for column_number, column in enumerate(sorted(columns, key=lambda item: item.left), start=1):
            previous: Region | None = None
            
            for region in sorted(column.regions, key=lambda item: (item.bbox[1], item.bbox[0])):
                if previous is None or not self.can_join(previous, region, page):
                    group_number += 1
                    
                reading_order += 1
                
                region.metadata.update(
                    {
                        "body_group_id": f"page_{page.pdf_page:03d}_body_{group_number:03d}",
                        "body_column_id": f"page_{page.pdf_page:03d}_column_{column_number:03d}",
                        "body_reading_order": reading_order,
                        "body_order_method": "geometric_columns_v1",
                        "body_order_scope": "page",
                        "body_group_scope": "local_column"
                    }
                )
                
                previous = region
                
        return page
    
    def matches_column(self, region: Region, column: Column) -> bool:
        if region.article_id != column.article_id:
            return False
        
        return(abs(region.bbox[0] - column.left) <= self.config.left_alignment_tolerance and
               horizontal_overlap(region.bbox, column.reference_bbox) >= self.config.minimum_horizontal_overlap)
        
    def can_join(self, previous: Region, current: Region, page: PageRecord) -> bool:
        gap = current.bbox[1] - previous.bbox[3]
        
        if not (-self.config.maximum_vertical_overlap <= gap <= self.config.maximum_vertical_gap):
            return False
        
        if not self.styles_compatible(previous, current):
            return False
        
        return not self.has_separator(previous, current, page)
    
    def styles_compatible(self, a: Region, b: Region) -> bool:
        a_size = a.style.get("median_font_size")
        b_size = b.style.get("median_font_size")
        
        if a_size and b_size:
            if min(a_size, b_size) / max(a_size, b_size) < self.config.minimum_font_size_ratio:
                return False
            
        a_font = a.style.get("dominant_font")
        b_font = b.style.get("dominant_font")
        
        if a_font and b_font and a_font != b_font:
            return False
        
        for key in ("bold_ratio", "italic_ratio"):
            a_value = a.style.get(key)
            b_value = b.style.get(key)
            
            if a_value is not None and b_value is not None:
                if abs(a_value - b_value) > self.config.maximum_style_ratio_difference:
                    return False
                
        return True
    
    def has_separator(self, previous: Region, current: Region, page: PageRecord) -> bool:
        gap_top = previous.bbox[3]
        gap_bottom = current.bbox[1]
        
        if gap_bottom <= gap_top:
            return False
        
        for region in page.regions:
            if region is previous or region is current:
                continue
            
            if region.type not in self.SEPARATORS or not valid_bbox(region):
                continue
            
            y0, y1 = region.bbox[1], region.bbox[3]
            
            intersects_gap = y0 < gap_bottom and y1 > gap_top
            
            if intersects_gap and horizontal_overlap(region.bbox, current.bbox) >= self.config.minimum_horizontal_overlap:
                return True
            
        return False