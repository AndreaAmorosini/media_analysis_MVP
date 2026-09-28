from press_reputation.models.page import PageRecord, RegionType


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

    def __init__(
        self,
        max_header_relative_height: float = 0.18,
        padding: float = 10.0,
    ) -> None:
        self.max_header_relative_height = max_header_relative_height
        self.padding = padding

    def enrich(self, page: PageRecord) -> PageRecord:
        if not page.page_height:
            return page

        absolute_top_limit = page.page_height * self.max_header_relative_height

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
            detected_bottom + self.padding,
            absolute_top_limit,
        )

        for region in page.regions:
            if not region.bbox or len(region.bbox) != 4:
                continue

            if region.type in self.PROTECTED_TYPES:
                continue

            y0 = region.bbox[1]

            if y0 > header_bottom:
                continue

            region.metadata["in_header_metadata_zone"] = True

            # Non convertire aggressivamente tutto ciò che è in alto.
            # Converti solo regioni già plausibilmente metadata.
            if region.type in self.ALLOWED_METADATA_TYPES:
                continue

            if self.looks_like_metadata_region(region):
                region.type = RegionType.HEADER_METADATA
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