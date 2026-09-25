from press_reputation.features import RegionFeatureExtractor, RegionFeatures
from press_reputation.models.page import PageRecord, Region, RegionType

PROTECTED_REGION_TYPES = {
    RegionType.CAPTION,
    RegionType.ARTICLE_POSITION_THUMBNAIL,
}

class RegionClassifier:
    #Classifica le regioni di una pagina in base a caratteristiche specifiche (testuali e di layout)
    
    def __init__(self):
        self.feature_extractor = RegionFeatureExtractor()
    
    def enrich(self, page: PageRecord) -> PageRecord:
        self.classify_individual_regions(page)
        self.classify_contextual_regions(page)
        self.enforce_single_title_and_subtitle(page)
        return page
    
    def classify_individual_regions(self, page: PageRecord) -> None:
        for region in page.regions:
            features = self.feature_extractor.extract(region, page)

            if features.municipalities:
                region.metadata["municipalities"] = features.municipalities
                
            self.enrich_region_metadata(region, features)

            region.type = self.classify(region, page, features)
    
    def classify(self, region: Region, page: PageRecord, features: RegionFeatures) -> RegionType:
        #L'ordine delle condizioni è importante: alcune categorie hanno priorità su altre. Ad esempio, se una regione è già classificata come CAPTION, non verrà riclassificata come ARTICLE_TITLE anche se soddisfa i criteri per quest'ultima.
        if region.type in PROTECTED_REGION_TYPES:
            return region.type
                
        if region.type == RegionType.CAPTION:
            return RegionType.CAPTION

        if self.like_article_position_thumbnail(features):
            return RegionType.ARTICLE_POSITION_THUMBNAIL

        if region.type == RegionType.ARTICLE_POSITION_THUMBNAIL:
            return RegionType.ARTICLE_POSITION_THUMBNAIL

        if region.type == RegionType.IMAGE:
            return RegionType.IMAGE

        if features.is_press_review_provider:
            return RegionType.PRESS_REVIEW_PROVIDER

        if features.is_known_newspaper:
            return RegionType.SOURCE_NAME

        if self.like_composite_clipping_metadata(features):
            return RegionType.HEADER_METADATA

        if "da pag" in features.raw_text_lower:
            return RegionType.ORIGINAL_PAGE

        if features.has_foglio:
            return RegionType.CLIPPING_SHEET

        if self.like_publication_date(features):
            return RegionType.PUBLICATION_DATE

        if self.like_location(features):
            return RegionType.LOCATION

        if self.like_advertisement(features):
            return RegionType.ADVERTISEMENT

        if self.like_navigation(features):
            return RegionType.NAVIGATION

        if self.like_related_content(features):
            return RegionType.RELATED_CONTENT

        if self.like_author(features):
            return RegionType.AUTHOR

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
    
    def enforce_single_title_and_subtitle(self, page: PageRecord) -> None:
        if self.is_continuation_page(page):
            for region in page.regions:
                if region.type == RegionType.ARTICLE_SUBTITLE or region.type == RegionType.ARTICLE_TITLE:
                    region.type = RegionType.UNKNOWN
            return

        
        title_candidates = [
            region for region in page.regions if region.type == RegionType.ARTICLE_TITLE and region.bbox and len(region.bbox) == 4
        ]
        
        if not title_candidates:
            return
        
        best_title = max(title_candidates, key=lambda region: self.title_score(region, page))
        
        for region in title_candidates:
            if region is not best_title:
                region.type = RegionType.UNKNOWN
                
        subtitle_candidates = self.find_subtitle_candidates(page, best_title)
        
        if not subtitle_candidates:
            return
        
        best_subtitle = max(subtitle_candidates, key=lambda region: self.subtitle_score(region, best_title))
        
        for region in subtitle_candidates:
            if region is best_subtitle:
                region.type = RegionType.ARTICLE_SUBTITLE
            elif region.type == RegionType.ARTICLE_SUBTITLE:
                region.type = RegionType.UNKNOWN
                
                
    def title_score(self, region: Region, page: PageRecord) -> float:
        features = self.feature_extractor.extract(region, page)

        score = 0.0

        if features.raw_label == "section_header":
            score += 2.0

        if 3 <= features.word_count <= 18:
            score += 2.0

        if features.bbox_width:
            score += min(features.bbox_width / 300, 2.0)

        if features.bbox_height:
            score += min(features.bbox_height / 40, 1.5)

        if features.is_known_newspaper:
            score -= 5.0

        if features.municipality_count > 0 and features.word_count <= 5:
            score -= 4.0

        if features.has_foglio or features.has_surface:
            score -= 5.0

        return score
    
    def find_subtitle_candidates(self, page: PageRecord, title: Region) -> list[Region]:
        candidates = []

        if not title.bbox:
            return candidates

        title_x0, title_y0, title_x1, title_y1 = title.bbox

        for region in page.regions:
            if region is title:
                continue

            if region.type not in {
                RegionType.UNKNOWN,
                RegionType.ARTICLE_BODY,
                RegionType.ARTICLE_SUBTITLE,
            }:
                continue

            if not region.bbox or len(region.bbox) != 4:
                continue

            features = self.feature_extractor.extract(region, page)
            
            if (features.bbox_height is not None and features.bbox_height > 80) or features.word_count > 35:
                return False
            
            if not self.like_subtitle_candidate(features):
                continue

            x0, y0, x1, y1 = region.bbox

            horizontal_overlap = self.overlap_ratio(title_x0, title_x1, x0, x1)

            if horizontal_overlap < 0.25:
                continue

            gap_above = title_y0 - y1
            gap_below = y0 - title_y1

            is_near_above = 0 <= gap_above <= 50
            is_near_below = 0 <= gap_below <= 60

            if is_near_above or is_near_below:
                candidates.append(region)

        return candidates
    
    def subtitle_score(self, region: Region, title: Region) -> float:
        if not region.bbox or not title.bbox:
            return 0.0

        x0, y0, x1, y1 = region.bbox
        tx0, ty0, tx1, ty1 = title.bbox

        score = 0.0

        overlap = self.overlap_ratio(tx0, tx1, x0, x1)
        score += overlap * 2.0

        gap_above = ty0 - y1
        gap_below = y0 - ty1

        if 0 <= gap_above <= 80:
            score += 1.5

        if 0 <= gap_below <= 100:
            score += 1.2

        width = x1 - x0
        title_width = tx1 - tx0

        if title_width > 0:
            width_ratio = min(width / title_width, 1.5)
            score += width_ratio

        return score
    
    @staticmethod
    def overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
        overlap = max(0.0, min(a1, b1) - max(a0, b0))
        base = max(min(a1 - a0, b1 - b0), 1.0)
        
        return overlap / base
    
    @staticmethod
    def like_subtitle_candidate(features: RegionFeatures) -> bool:
        if features.word_count < 6:
            return False

        if features.word_count > 35:
            return False

        if features.text_length > 260:
            return False

        if features.raw_label not in {"text", "section_header"}:
            return False

        if features.has_url:
            return False

        if features.municipality_count > 0 and features.word_count <= 5:
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
    def like_advertisement(features: RegionFeatures) -> bool:
        if features.has_ad_marker:
            return True
        
        if features.raw_text_lower.strip() in {"adv", "ads"}:
            return True
        
        return False
    
    @staticmethod
    def like_author(features: RegionFeatures) -> bool:
        if features.has_foglio or features.has_surface:
            return False

        if features.has_tiratura or features.has_diffusione or features.has_lettori:
            return False

        if features.has_dir_resp or features.has_quotidiano:
            return False

        if features.has_url:
            return False

        if features.municipality_count > 0:
            return False

        if features.bbox_width is not None and features.bbox_height is not None:
            if features.bbox_width < 20 and features.bbox_height > 150:
                return False

        if features.uppercase_ratio > 0.85:
            return False

        if not features.has_author_marker:
            return False

        if features.word_count < 2 or features.word_count > 8:
            return False

        return True
    
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
        
        if "," in features.raw_text_lower:
            return False

        if ":" in features.raw_text_lower:
            return False

        if features.word_count > 5:
            return False

        if features.text_length > 100:
            return False

        if features.is_known_newspaper or features.is_press_review_provider:
            return False

        if features.has_url:
            return False

        if features.has_foglio or features.has_surface:
            return False

        if features.has_tiratura or features.has_diffusione or features.has_lettori:
            return False

        if features.has_dir_resp or features.has_quotidiano:
            return False

        if features.has_newsletter or features.has_related_marker:
            return False

        if features.has_author_marker:
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
    
    def is_continuation_page(self, page: PageRecord) -> bool:
        return (
            page.clipping is not None
            and page.clipping.sheet_current is not None
            and page.clipping.sheet_current > 1
        )