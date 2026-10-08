import re
from statistics import median

from press_reputation.classification.metadata_seed_classifier import MetadataSeedClassifier
from press_reputation.features import RegionFeatureExtractor, RegionFeatures
from press_reputation.features.region_features import entity_lookup_metadata
from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.config import RegionClassificationConfig

PROTECTED_REGION_TYPES = {
    RegionType.CAPTION,
    RegionType.ARTICLE_POSITION_THUMBNAIL,
}

class ArticleSemanticClassifier:
    #Classifica le regioni di una pagina in base a caratteristiche specifiche (testuali e di layout)
    
    def __init__(self, config: RegionClassificationConfig | None = None) -> None:
        self.config = config or RegionClassificationConfig()
        self.feature_extractor = RegionFeatureExtractor()
    
    def enrich(self, page: PageRecord) -> PageRecord:
        self.classify_individual_regions(page)
        return page
        
    def classify_individual_regions(self, page: PageRecord) -> None:
        body_font_size = self.body_font_reference(page)
        
        for region in page.regions:
            features = self.feature_extractor.extract(region, page)
            
            if (features.entity_kind == "location" and not features.entity_ambiguous):
                lookup_data = entity_lookup_metadata(features)
                if lookup_data is not None:
                    region.metadata.setdefault("entity_lookup", lookup_data)
                
            self.enrich_region_metadata(region, features)

            region.type = self.classify(region, page, features, body_font_size=body_font_size)
            
            if (region.type == RegionType.LOCATION and features.municipalities):
                region.metadata["municipalities"] = (features.municipalities)
            
    def classify_article_section_headers(self, page: PageRecord) -> None:
        body_seen = False
        
        ordered = sorted([region for region in page.regions if (region.bbox and not region.metadata.get("in_header_metadata_zone")
                                                                and not region.exclude_from_article_text)], key = lambda region: (region.bbox[1], region.bbox[0]))
        
        for region in ordered:
            if region.type == RegionType.ARTICLE_BODY:
                body_seen = True
                continue
            
            if (body_seen and region.raw_label == "section_header" and region.type in {RegionType.UNKNOWN, RegionType.ARTICLE_TITLE}
                and region.text and not region.exclude_from_article_text):
                region.type = RegionType.ARTICLE_SECTION_HEADER
    
    def classify(self, region: Region, page: PageRecord, features: RegionFeatures, *, body_font_size: float = None) -> RegionType:
        #L'ordine delle condizioni è importante: alcune categorie hanno priorità su altre. Ad esempio, se una regione è già classificata come CAPTION, non verrà riclassificata come ARTICLE_TITLE anche se soddisfa i criteri per quest'ultima.
        if (region.exclude_from_article_text or region.metadata.get("in_header_metadata_zone") or 
                region.type in MetadataSeedClassifier.SEED_TYPES):
            return region.type
        
        if region.metadata.get("in_header_metadata_zone"):
            return region.type
        
        if region.type in {
            RegionType.CAPTION,
            RegionType.IMAGE,
            RegionType.ARTICLE_POSITION_THUMBNAIL,
            RegionType.RIGHTS_NOTICE,
            RegionType.WATERMARK,
            RegionType.ADVERTISEMENT,
            RegionType.PULL_QUOTE,
            RegionType.TABLE,
            RegionType.INFOGRAPHIC,
            RegionType.ARTICLE_SECTION_HEADER
        }:
            return region.type
        
        if region.type in PROTECTED_REGION_TYPES:
            return region.type
                
        if region.type == RegionType.CAPTION:
            return RegionType.CAPTION

        if region.type == RegionType.ARTICLE_POSITION_THUMBNAIL:
            return RegionType.ARTICLE_POSITION_THUMBNAIL

        if region.type == RegionType.IMAGE:
            return RegionType.IMAGE

        if self.like_location(features):
            return RegionType.LOCATION

        if self.like_navigation(features):
            return RegionType.NAVIGATION

        if self.like_related_content(features):
            return RegionType.RELATED_CONTENT

        if region.type == RegionType.UNKNOWN:
            accepted, evidence = self.is_body_seed(region, features, body_font_size=body_font_size)
            
            if (features.word_count >= self.config.body_seed_min_words and region.raw_label == "text"):
                region.metadata["body_seed_evaluation"] = {
                    "accepted": accepted,
                    "evidence": evidence,
                    "word_count": features.word_count,
                    "method": "semantic_body_seed_v1"
                }
                
            if accepted:
                region.metadata["body_role"] = "seed"
                region.metadata["body_detection_method"] = "semantic_body_seed_v1"
                
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
    def overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
        overlap = max(0.0, min(a1, b1) - max(a0, b0))
        base = max(min(a1 - a0, b1 - b0), 1.0)
        
        return overlap / base
    
    @staticmethod
    def like_advertisement(features: RegionFeatures) -> bool:
        if features.has_ad_marker:
            return True
        
        if features.raw_text_lower.strip() in {"adv", "ads"}:
            return True
        
        return False
        
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
        if (features.entity_kind != "location" or features.entity_ambiguous):
            return False
        
        if "," in features.raw_text_lower:
            return False

        if ":" in features.raw_text_lower:
            return False

        if features.word_count > 5:
            return False

        if features.text_length > 100:
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
        
    def is_body_seed(self, region: Region, features: RegionFeatures, *, body_font_size: float | None) -> tuple[bool, list[str]]:
        evidence = []
        
        if(region.type != RegionType.UNKNOWN or region.exclude_from_article_text or region.metadata.get("in_header_metadata_zone") or
            region.boilerplate or region.metadata.get("inside_article_position_thumbnail")):
            return False, ["protected_or_boilerplate"]
            
        if (region.metadata.get("content_scope") in {"related", "advertisement", "boilerplate", "non_main"}):
            return False, ["external_content_scope"]
        
        if (not region.text or region.raw_label != "text" or features.word_count < self.config.body_seed_min_words):
            return False, ["insufficient_text_or_docling_label"]
        
        if (features.has_url or features.has_navigation_marker or features.has_newsletter or features.has_share_marker or features.has_ad_marker or
            features.has_rights_notice_marker or features.has_watermark_marker):
            return False, ["navigation_related_or_techinical_marker"]
        
        if (features.has_foglio or features.has_surface or features.has_tiratura or features.has_diffusione or features.has_lettori or
            features.has_dir_resp or ("da pag" in features.raw_text_lower)):
            return False, ["clipping_metadata_marker"]
        
        if features.like_index_entry:
            return False, ["index_entry_marker"]
        
        entity = (region.metadata.get("entity_lookup") or {})
        if entity.get("kind") in {
            "source", "provider"
        }:
            return False, ["source_or_provider_match"]
        
        if features.uppercase_ratio > 0.85:
            return False, ["mostly_uppercase"]
        
        candidate_size = features.median_font_size
        if (body_font_size and candidate_size and candidate_size > 0):
            ratio = candidate_size / body_font_size
            if not(self.config.body_seed_min_font_ratio <= ratio <= self.config.body_seed_max_font_ratio):
                return False, ["font_size_unlike_body"]
            evidence.append("body_like_font_size")
        else:
            evidence.append("font_size_unavailable")
            
        if (features.bold_ratio is not None and features.bold_evidence_fraction >= self.config.body_seed_min_bold_evidence_fraction and
            features.bold_ratio > self.config.body_seed_max_bold_ratio):
            return False, ["predominantly_bold"]
        
        evidence.append("no_editorial_or_technical_veto")
        return True, evidence
    
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
        
    def body_font_reference(self, page: PageRecord) -> float | None:
        sizes = []
        
        for region in page.regions:
            if(region.raw_label != "text" or not region.text or region.exclude_from_article_text or
                region.metadata.get("in_header_metadata_zone") or region.type != RegionType.UNKNOWN):
                continue
            
            if (len(re.findall(r"\w+", region.text)) < self.config.body_seed_min_words):
                continue
            
            size = region.style.get("median_font_size")
            
            if isinstance(size, (int, float)) and size > 0:
                sizes.append(size)
                
        return median(sizes) if sizes else None
        
RegionClassifier = ArticleSemanticClassifier