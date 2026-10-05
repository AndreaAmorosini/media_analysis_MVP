#Si occupa di classificare le regioni tecniche come RIGHTS_NOTICE, WATERMARK, ADVERTISEMENT, ARTICLE_POSITION_THUMBNAIL, boilerplate, caption/image protection
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import PageRecord, Region, RegionType

class TechnicalRegionClassifier:
    def __init__(self) -> None:
        self.feature_extractor = RegionFeatureExtractor()
        
    def enrich(self, page: PageRecord) -> PageRecord:
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

            if region.type == RegionType.IMAGE:
                continue

            if self.looks_like_rights_notice(features):
                region.type = RegionType.RIGHTS_NOTICE
                region.exclude_from_article_text = True
                continue
            
            # if features.is_press_review_provider:
            #     region.type = RegionType.PRESS_REVIEW_PROVIDER
            #     region.metadata["press_review_provider_name"] = features.press_review_provider_name
            #     continue

            if self.looks_like_watermark(region, features):
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