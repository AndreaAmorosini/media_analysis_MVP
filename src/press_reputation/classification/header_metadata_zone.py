from press_reputation.models.page import PageRecord, RegionType
from press_reputation.config import HeaderMetadataConfig


class HeaderMetadataZoneDetector:
    """
    Identifica una zona metadata iniziale della pagina.

    Importante:
    - WATERMARK e RIGHTS_NOTICE NON devono determinare l'estensione della zona.
    - regioni tecniche, immagini e footer non devono essere riclassificati.
    - la zona metadata deve restare confinata alla parte alta della pagina.
    """

    SEED_TYPES = {
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.HEADER_METADATA,
    }

    PROTECTED_TYPES = {
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.FOOTER,
        RegionType.ADVERTISEMENT,
        RegionType.TABLE,
        RegionType.INFOGRAPHIC,
        RegionType.PULL_QUOTE,
    }

    ALLOWED_METADATA_TYPES = {
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.HEADER_METADATA,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
    }

    def __init__(self, config: HeaderMetadataConfig | None = None) -> None:
        self.config = config or HeaderMetadataConfig()

    def enrich(self, page: PageRecord) -> PageRecord:
        if not page.page_height:
            return page

        absolute_top_limit = page.page_height * self.config.max_header_relative_height

        seed_regions = [
            region
            for region in page.regions
            if region.type in self.SEED_TYPES
            and region.bbox
            and len(region.bbox) == 4
            and self.is_valid_header_seed(region, absolute_top_limit)
        ]

        if not seed_regions:
            return page

        detected_bottom = max(region.bbox[3] for region in seed_regions)

        # La zona non può mai estendersi oltre la fascia alta configurata.
        header_bottom = min(
            detected_bottom + self.config.padding,
            absolute_top_limit,
        )

        for region in page.regions:
            if (region.type in self.PROTECTED_TYPES or region.exclude_from_article_text and region.type not in self.SEED_TYPES):
                continue
            
            box = region.bbox
            if not box or len(box) != 4:
                continue
            
            x0, y0, x1, y1 = box
            if x1 <= x0 or y1 <= y0:
                continue
            
            if self.config.require_full_region_inside_zone:
                inside = y0 >= 0 and y1 <= header_bottom
            else:
                inside = y0 >= 0 and y0 <= header_bottom
                
            if not inside:
                continue
            
            region.metadata["in_header_metadata_zone"] = True
            region.metadata["header_zone_bottom"] = header_bottom
            
            if region.type in self.SEED_TYPES:
                region.metadata["header_zone_role"] = "seed"
                continue
            
            if self.looks_like_metadata_region(region):
                region.metadata["type_before_header_zone"] = region.type.value
                region.type = RegionType.HEADER_METADATA
                region.exclude_from_article_text = True
                region.metadata["header_zone_role"] = "metadata_marker"
            else:
                region.metadata["header_zone_role"] = "unresolved_header_content"
                region.exclude_from_article_text = True

        return page

    @staticmethod
    def is_valid_header_seed(region, absolute_top_limit: float) -> bool:
        x0, y0, x1, y1 = region.bbox
        width = x1 - x0
        height = y1 - y0

        if y0 > absolute_top_limit:
            return False

        # Esclude watermark verticali o notice laterali.
        if width < 30 and height > 120:
            return False

        return True

    @staticmethod
    def looks_like_metadata_region(region) -> bool:
        text = (region.text or "").lower()

        if region.raw_label == "page_header":
            return True

        metadata_markers = [
            "da pag",
            "foglio",
            "tiratura",
            "diffusione",
            "lettori",
            "dir. resp",
            "quotidiano",
            "settimanale",
            "mensile",
            "superficie",
        ]

        return any(marker in text for marker in metadata_markers)