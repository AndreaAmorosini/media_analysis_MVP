#Si occupa di classificare le regioni tecniche come RIGHTS_NOTICE, WATERMARK, ADVERTISEMENT, ARTICLE_POSITION_THUMBNAIL, boilerplate, caption/image protection
import re
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.config import WatermarkDetectionConfig

class TechnicalRegionClassifier:
    def __init__(self, config: WatermarkDetectionConfig | None = None) -> None:
        self.feature_extractor = RegionFeatureExtractor()
        self.config = config or WatermarkDetectionConfig()
        
    def enrich(self, page: PageRecord, *, document_page_count: int = 1) -> PageRecord:
        thumbnail_regions: list[Region] = []

        for region in page.regions:
            features = self.feature_extractor.extract(region, page, include_entity=False)

            if region.type == RegionType.CAPTION:
                continue

            if self.looks_like_article_position_thumbnail(region, features):
                region.type = RegionType.ARTICLE_POSITION_THUMBNAIL
                region.metadata["technical_image"] = True
                region.metadata["exclude_from_article_media"] = True
                thumbnail_regions.append(region)
                continue
            
            if region.type in {RegionType.TABLE, RegionType.INFOGRAPHIC, RegionType.PULL_QUOTE, RegionType.IMAGE}:
                continue

            if self.looks_like_rights_notice(features):
                region.type = RegionType.RIGHTS_NOTICE
                region.exclude_from_article_text = True
                continue
            
            score, components, supported = (self.watermark_score(region, page, document_page_count=document_page_count))
            if components:
                region.metadata["watermark_detection"] = {
                    "method": "watermark_score_v1",
                    "score": score,
                    "components": components,
                    "non_visual_support": supported,
                    "threshold": self.config.min_score,
                    "accepted": (supported and score >= self.config.min_score)
                }
                
            if (supported and score >= self.config.min_score):
                region.type = RegionType.WATERMARK
                region.exclude_from_article_text = True
                continue

            if self.looks_like_advertisement(features):
                region.type = RegionType.ADVERTISEMENT
                region.exclude_from_article_text = True
                continue

        self.exclude_regions_inside_thumbnails(page, thumbnail_regions)

        return page
    
    @staticmethod
    def bbox_overlap_fraction(candidate: Region, other: Region) -> float:
        if (not candidate.bbox or not other.bbox or len(candidate.bbox) != 4 or len(other.bbox) != 4):
            return 0.0
        
        ax0, ay0, ax1, ay1 = candidate.bbox
        bx0, by0, bx1, by1 = other.bbox
        
        intersection = (max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0)))
        candidate_area = (max(ax1 - ax0, 0.0) * max(ay1 - ay0, 0.0))
        
        return intersection / candidate_area if candidate_area > 0 else 0.0
    
    def overlaps_editorial_candidate(self, region: Region, page: PageRecord) -> bool:
        return any(other is not region and other.text and not other.exclude_from_article_text and
                    other.raw_label in {"text", "section_header"} and
                    self.bbox_overlap_fraction(region, other) >= self.config.minimum_editorial_overlap for other in page.regions)
        
    @staticmethod
    def pale_neutral_color(color: object) -> bool:
        if isinstance(color, int):
            channels = (
                (color >> 16) & 255,
                (color >> 8) & 255,
                color & 255
            )
            values = [channel / 255 for channel in channels]
        elif isinstance(color, (list, tuple)) and len(color) >= 3:
            values = [float(channel) for channel in color[:3]]
            if max(values) > 1.0:
                values = [channel / 255 for channel in values]
        else:
            return False
        
        return (max(values) - min(values) <= 0.08 and sum(values) / 3 >= 0.55)
    
    def watermark_score(self, region: Region, page: PageRecord, *, document_page_count: int) -> tuple[float, dict[str, float], bool]:
        config = self.config
        text = (region.text or "").strip()
        lowered = text.casefold()
        components: dict[str, float] = {}
        
        strong_marker = (len(text) <= config.maximum_strong_marker_chars and bool(re.match(r"^data\s+stampa\b", lowered)))
        if strong_marker:
            components["data_stampa_marker"] = 0.70
            
        weak_marker = (len(text) <= config.maximum_weak_marker_chars and any(marker in lowered for marker in ("uso esclusivo", "cliente che lo riceve")))
        if weak_marker:
            components["weak_marker"] = 0.30
            
        opacity = region.style.get("median_opacity")
        if (isinstance(opacity, (int, float)) and opacity < config.low_opacity_threshold):
            components["low_opacity"] = 0.12
            
        if self.pale_neutral_color(region.style.get("dominant_color")):
            components["pale_neutral_color"] = 0.08
            
        repeated = (document_page_count >= config.min_document_pages_for_repetition and
                    region.boilerplate_frequency is not None and region.boilerplate_frequency >=  config.repeated_page_fraction)
        if repeated:
            components["repeated_across_pages"] = 0.16
            
        if (region.bbox and page.page_width and page.page_height):
            x0, y0, x1, y1 = region.bbox
            near_edge = (x0 / page.page_width <= config.edge_fraction_x or x1 / page.page_width >= 1 - config.edge_fraction_x or
                            y0 / page.page_height <= config.edge_fraction_y or y1 / page.page_height >= 1 - config.edge_fraction_y)
            if near_edge:
                components["page_edge"] = 0.09
                
            if (x1 - x0 < 25 and y1 - y0 > 150):
                components["vertical_strip"] = 0.08
                
        if self.overlaps_editorial_candidate(region, page):
            components["editorial_overlap"] = 0.12
            
        words = len(re.findall(r"\w+", text))
        if words >= 18 or len(text) > 200:
            components["long_editorial_text"] = -0.35
            
        if region.raw_label == "section_header":
            components["docling_heading"] = -0.25
            
        score = round(sum(components.values()), 4)
        
        has_non_visual_support = (strong_marker or weak_marker or (repeated and ("editorial_overlap" in components or "vertical_strip" in components)))
        
        return (score, components, has_non_visual_support)
    
    @staticmethod
    def looks_like_rights_notice(features) -> bool:
        return features.has_rights_notice_marker
    
    @staticmethod
    def looks_like_watermark(region: Region, features) -> bool:
        if features.has_watermark_marker:
            return True
        
        if features.bbox_width is not None and features.bbox_height is not None:
            if features.bbox_width < 25 and features.bbox_height > 150:
                return True
            
        opacity = region.style.get("median_opacity")
        
        if opacity is not None and opacity < 0.55:
            return True
        
    @staticmethod
    def looks_like_advertisement(features) -> bool:
        return features.has_ad_marker
    
    @staticmethod
    def looks_like_article_position_thumbnail(region: Region, features) -> bool:
        if region.type != RegionType.IMAGE and features.raw_label != "picture":
            return False

        if features.bbox_width is None or features.bbox_height is None:
            return False

        if (
            features.relative_x0 is None
            or features.relative_y0 is None
            or features.relative_x1 is None
            or features.relative_y1 is None
        ):
            return False

        width = features.bbox_width
        height = features.bbox_height

        if width <= 0 or height <= 0:
            return False

        aspect_ratio = height / width
        relative_area = (
            (features.relative_x1 - features.relative_x0)
            * (features.relative_y1 - features.relative_y0)
        )

        if features.relative_x0 < 0.70:
            return False

        if features.relative_y0 < 0.60:
            return False

        # Una miniatura di pagina è tipicamente verticale.
        if aspect_ratio < 1.15:
            return False

        # Esclude loghi/iconcine troppo piccole.
        if height < 80:
            return False

        # Esclude immagini troppo grandi.
        if relative_area > 0.08:
            return False

        if width > 240 or height > 320:
            return False

        return True
    
    def exclude_regions_inside_thumbnails(self, page: PageRecord, thumbnails: list[Region]) -> None:
        for thumbnail in thumbnails:
            if not thumbnail.bbox:
                continue

            for region in page.regions:
                if region is thumbnail:
                    continue

                if not region.bbox:
                    continue

                if self.containment_ratio(region.bbox, thumbnail.bbox) >= 0.80:
                    region.metadata["inside_article_position_thumbnail"] = True
                    region.exclude_from_article_text = True

                    if region.type not in {
                        RegionType.WATERMARK,
                        RegionType.RIGHTS_NOTICE,
                        RegionType.ARTICLE_POSITION_THUMBNAIL,
                    }:
                        region.type = RegionType.UNKNOWN


    @staticmethod
    def containment_ratio(inner: list[float], outer: list[float]) -> float:
        ix0, iy0, ix1, iy1 = inner
        ox0, oy0, ox1, oy1 = outer

        x0 = max(ix0, ox0)
        y0 = max(iy0, oy0)
        x1 = min(ix1, ox1)
        y1 = min(iy1, oy1)

        if x1 <= x0 or y1 <= y0:
            return 0.0

        intersection = (x1 - x0) * (y1 - y0)
        inner_area = max((ix1 - ix0) * (iy1 - iy0), 1.0)

        return intersection / inner_area