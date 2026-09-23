from press_reputation.features import RegionFeatureExtractor, RegionFeatures
from press_reputation.models.page import PageRecord, Region, RegionType

PROTECTED_REGION_TYPES = {
    RegionType.ARTICLE_POSITION_THUMBNAIL,
    RegionType.CAPTION
}

class RegionClassifier:
    #Classifica le regioni di una pagina in base a caratteristiche specifiche (testuali e di layout)
    
    def __init__(self):
        self.feature_extractor = RegionFeatureExtractor()
    
    def enrich(self, page: PageRecord) -> PageRecord:
        self.classify_individual_regions(page)
        self.classify_contextual_regions(page)
        return page
    
    def classify_individual_regions(self, page: PageRecord) -> None:
        for region in page.regions:
            features = self.feature_extractor.extract(region, page)

            if features.municipalities:
                region.metadata["municipalities"] = features.municipalities
                
            self.enrich_region_metadata(region, features)

            region.type = self.classify(region, page, features)
    
    def classify(self, region: Region, page: PageRecord, features: RegionFeatures) -> RegionType:
        if region.type in PROTECTED_REGION_TYPES:
            return region.type
        
        features = self.feature_extractor.extract(region, page)
        
        if self.like_article_position_thumbnail(features):
            return RegionType.ARTICLE_POSITION_THUMBNAIL
        
        if features.is_press_review_provider:
            return RegionType.PRESS_REVIEW_PROVIDER
        
        if features.is_known_newspaper:
            return RegionType.SOURCE_NAME
        
        if self.like_composite_clipping_metadata(features):
            return RegionType.HEADER_METADATA
        
        if self.like_location(features):
            return RegionType.LOCATION
        
        if "da pag" in features.raw_text_lower:
            return RegionType.ORIGINAL_PAGE

        if features.has_foglio:
            return RegionType.CLIPPING_SHEET
        
        if self.like_publication_date(features):
            return RegionType.PUBLICATION_DATE
                
        if self.like_header_metadata(features):
            return RegionType.HEADER_METADATA
        
        if self.like_footer(features):
            return RegionType.FOOTER
        
        if self.like_navigation(features):
            return RegionType.NAVIGATION
        
        if self.like_related_content(features):
            return RegionType.RELATED_CONTENT
                
        if self.like_article_title(features):
            return RegionType.ARTICLE_TITLE
        
        if self.like_article_body(features):
            return RegionType.ARTICLE_BODY
        
        return region.type  # Mantieni il tipo originale se non corrisponde a nessuna categoria nota
    
    def enrich_region_metadata(self, region: Region, features: RegionFeatures) -> None:
        text = region.text or ""
        
        original_page = self.extract_original_page(text)
        publication_date = self.extract_publication_date_text(text)
        sheet_info = self.extract_sheet_info(text)
        
        if original_page is not None:
            region.metadata["original_page"] = original_page
            
        if publication_date is not None:
            region.metadata["publication_date"] = publication_date
            
        if sheet_info is not None:
            region.metadata["sheet_current"] = sheet_info[0]
            region.metadata["sheet_total"] = sheet_info[1]
            
    @staticmethod
    def extract_original_page(text: str) -> int | None:
        import re

        match = re.search(r"\bda\s+pag\.?\s+(\d+)", text, flags=re.IGNORECASE)

        if not match:
            return None

        return int(match.group(1))


    @staticmethod
    def extract_sheet_info(text: str) -> tuple[int, int | None] | None:
        import re

        match = re.search(
            r"\bfoglio\s+(\d+)(?:\s*/\s*(\d+))?",
            text,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        current = int(match.group(1))
        total = int(match.group(2)) if match.group(2) else None

        return current, total


    @staticmethod
    def extract_publication_date_text(text: str) -> str | None:
        import re
        from datetime import date

        months = {
            "GEN": 1,
            "FEB": 2,
            "MAR": 3,
            "APR": 4,
            "MAG": 5,
            "GIU": 6,
            "LUG": 7,
            "AGO": 8,
            "SET": 9,
            "OTT": 10,
            "NOV": 11,
            "DIC": 12,
        }

        match = re.search(
            r"\b(\d{1,2})-([A-Z]{3})-(\d{4})\b",
            text,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        day = int(match.group(1))
        month = months.get(match.group(2).upper())
        year = int(match.group(3))

        if month is None:
            return None

        return date(year, month, day).isoformat()
            
    
    @staticmethod
    def like_publication_date(features: RegionFeatures) -> bool:
        if not features.has_date:
            return False

        if "da pag" in features.raw_text_lower:
            return False

        if features.has_foglio:
            return False

        if features.text_length > 40:
            return False

        return True
    
    @staticmethod
    def like_composite_clipping_metadata(features: RegionFeatures) -> bool:
        text = features.raw_text_lower
        
        has_original_page = "da pag" in text
        return sum([has_original_page, features.has_foglio, features.has_date]) >= 2
    
    @staticmethod
    def like_location(features: RegionFeatures) -> bool:
        if features.municipality_count == 0:
            return False
        
        if features.word_count > 5:
            return False
        
        if features.text_length > 100:
            return False
        
        if features.has_foglio or features.has_surface:
            return False
        
        if features.has_tiratura or features.has_diffusione or features.has_lettori:
            return False
        
        if features.has_dir_resp or features.has_quotidiano:
            return False
        
        if features.has_url:
            return False
        
        if features.has_newsletter or features.has_related_marker:
            return False
        
        if features.has_navigation_marker:
            return False
        
        
        return True
    
    @staticmethod
    def like_header_metadata(features: RegionFeatures) -> bool:
        metadata_markers = sum([
            features.has_surface,
            features.has_tiratura,
            features.has_diffusione,
            features.has_lettori,
            features.has_dir_resp,
            features.has_quotidiano
        ])
        
        if metadata_markers >= 1 and features.is_top_area:
            return True
        
        if metadata_markers >= 2:
            return True
        
        return False
    
    @staticmethod
    def like_footer(features: RegionFeatures) -> bool:
        if features.like_section_label:
            return True
        
        return features.is_bottom_area and features.text_length <= 180
    
    @staticmethod
    def like_navigation(features: RegionFeatures) -> bool:
        if features.has_navigation_marker:
            return True
        
        if features.has_url and features.word_count <= 8:
            return True
        
        return False
        
    
    @staticmethod
    def like_related_content(features: RegionFeatures) -> bool:
        return (
            features.has_related_marker
            or features.has_newsletter
            or features.has_share_marker
        )
        
    @staticmethod
    def like_article_title(features: RegionFeatures) -> bool:
        if features.raw_label != "section_header":
            return False

        if features.is_known_newspaper or features.is_press_review_provider:
            return False

        if features.word_count < 3 or features.word_count > 18:
            return False

        if features.has_url:
            return False

        if features.has_foglio or features.has_surface or features.has_tiratura:
            return False

        if features.has_newsletter or features.has_related_marker:
            return False

        if features.bbox_width is not None and features.bbox_width < 120:
            return False

        return True

    @staticmethod
    def like_article_body(features: RegionFeatures) -> bool:
        if features.raw_label != "text":
            return False

        if features.word_count < 18:
            return False

        if features.has_url:
            return False

        if features.has_newsletter or features.has_related_marker:
            return False

        if features.has_foglio or features.has_surface:
            return False

        if features.has_tiratura or features.has_diffusione or features.has_lettori:
            return False

        if features.uppercase_ratio > 0.85:
            return False

        return True
    
    @staticmethod
    def like_article_position_thumbnail(features: RegionFeatures) -> bool:
        if features.raw_label != "picture":
            return False

        if features.bbox_width is None or features.bbox_height is None:
            return False

        if features.relative_x0 is None or features.relative_y0 is None:
            return False

        relative_area = 0.0
        if features.relative_x0 is not None and features.relative_x1 is not None:
            if features.relative_y0 is not None and features.relative_y1 is not None:
                relative_area = (
                    (features.relative_x1 - features.relative_x0)
                    * (features.relative_y1 - features.relative_y0)
                )

        # Miniatura tecnica: piccola, tipicamente in basso/destra.
        if relative_area > 0.08:
            return False

        if features.bbox_width > 220:
            return False

        if features.bbox_height > 220:
            return False

        if features.relative_x0 >= 0.55 and features.relative_y0 >= 0.55:
            return True

        return False

    def classify_contextual_regions(self, page: PageRecord) -> None:
        regions = [
            region
            for region in page.regions
            if region.bbox and len(region.bbox) == 4
        ]

        regions.sort(key=lambda item: (item.bbox[1], item.bbox[0]))

        for index, region in enumerate(regions):
            if region.type not in {
                RegionType.UNKNOWN,
                RegionType.ARTICLE_BODY,
            }:
                continue

            previous_title = self.find_previous_title(regions, index)

            if previous_title is None:
                continue

            vertical_gap = region.bbox[1] - previous_title.bbox[3]

            if vertical_gap < 0:
                continue

            features = self.feature_extractor.extract(region, page)

            if self.like_article_subtitle_after_title(features, vertical_gap):
                region.type = RegionType.ARTICLE_SUBTITLE
                
    @staticmethod
    def like_article_subtitle_after_title(
        features: RegionFeatures,
        vertical_gap: float,
    ) -> bool:
        if vertical_gap > 100:
            return False

        if features.word_count < 5:
            return False

        if features.word_count > 45:
            return False

        if features.text_length > 320:
            return False

        if features.has_url:
            return False

        if features.has_foglio or features.has_surface:
            return False

        if features.has_tiratura or features.has_diffusione or features.has_lettori:
            return False

        if features.has_newsletter or features.has_related_marker:
            return False

        if features.uppercase_ratio > 0.85:
            return False

        return True
                
    @staticmethod
    def find_previous_title(
        regions: list[Region],
        current_index: int,
    ) -> Region | None:
        for previous in reversed(regions[:current_index]):
            if previous.type == RegionType.ARTICLE_TITLE:
                return previous

        return None