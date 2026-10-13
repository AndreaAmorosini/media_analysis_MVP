import re

from press_reputation.features import RegionFeatureExtractor
from press_reputation.features.region_features import entity_lookup_metadata
from press_reputation.models.page import PageRecord, Region, RegionType

class MetadataSeedClassifier:
    TECHNICAL_TYPES = {
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.ADVERTISEMENT,
        RegionType.FOOTER,
        RegionType.TABLE,
        RegionType.INFOGRAPHIC,
        RegionType.PULL_QUOTE
    }
    
    SEED_TYPES = {
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.HEADER_METADATA
    }
    
    def __init__(self) -> None:
        self.feature_extractor = RegionFeatureExtractor()
        
    def enrich(self, page: PageRecord) -> PageRecord:
        for region in page.regions:
            if (region.type in self.TECHNICAL_TYPES or region.exclude_from_article_text or not region.text):
                continue
            
            if region.type in self.SEED_TYPES:
                region.metadata.setdefault("metadata_seed_method", "preexisting_type")
                continue
            
            features = self.feature_extractor.extract(region, page)
            if features.entity_kind is not None:
                lookup_data = entity_lookup_metadata(features)
                region.metadata["entity_lookup"] = lookup_data
            text = features.raw_text_lower
            
            detected_type = None
            reason = None
            
            #Provider/testata prima di location o di eventuali euristiche successive
            if (features.entity_kind == "source" and not features.entity_ambiguous):
                detected_type = RegionType.SOURCE_NAME
                reason = (f"entity_{features.entity_method}_source")
            elif (features.entity_kind == "provider" and not features.entity_ambiguous):
                detected_type = RegionType.PRESS_REVIEW_PROVIDER
                reason = (f"entity_{features.entity_method}_provider")
            elif self._composite(features):
                detected_type = RegionType.HEADER_METADATA
                reason = "composite_clipping_metadata"
            elif "da pag" in text:
                detected_type = RegionType.ORIGINAL_PAGE
                reason = "original_page_marker"
            elif features.has_foglio:
                detected_type = RegionType.CLIPPING_SHEET
                reason = "clipping_sheet_marker"
            elif self._short_date(features):
                detected_type = RegionType.PUBLICATION_DATE
                reason = "short_date_marker"
            elif region.raw_label == "page_header":
                detected_type = RegionType.HEADER_METADATA
                reason = "docling_page_header"
            elif self._audience_metadata(features, region, page):
                detected_type = RegionType.HEADER_METADATA
                reason = "audience_metadata_marker"
                
            if detected_type is None:
                continue
            
            previous_type = region.type.value
            region.type = detected_type
            region.metadata["metadata_seed_method"] = reason
            region.metadata["type_before_metadata_seed"] = previous_type
            
            region.exclude_from_article_text = True
            
        return page
    
    
    @staticmethod
    def _composite(features) -> bool:
        return sum(("da pag" in features.raw_text_lower, features.has_foglio, features.has_date)) >= 2
    
    @staticmethod
    def _short_date(features) -> bool:
        return (features.has_date and features.text_length <= 40 and not features.has_foglio and "da pag" not in features.raw_text_lower)
    
    
    @staticmethod
    def _audience_metadata(
        features,
        region: Region,
        page: PageRecord,
    ) -> bool:
        """
        Riconosce i metadata tecnici della rassegna stampa.

        Richiede:
        - posizione compatibile con l'intestazione;
        - almeno un indicatore di metadata;
        - formato esplicito da scheda tecnica.

        Non considera metadata le semplici occorrenze
        di parole come 'lettori' o 'superficie' nel body.
        """

        # 1. Validazione di geometria e contenuto.
        if (
            not region.bbox
            or not page.page_height
            or not region.text
        ):
            return False

        x0, y0, x1, y1 = region.bbox

        if x1 <= x0 or y1 <= y0:
            return False

        # 2. La regione deve trovarsi interamente
        # nella fascia superiore del foglio PDF.
        header_limit = min(
            135.0,
            0.16 * page.page_height,
        )

        if y0 < 0 or y1 > header_limit:
            return False

        # 3. Deve contenere almeno un possibile
        # indicatore di metadata editoriali.
        has_metadata_marker = any((
            features.has_surface,
            features.has_tiratura,
            features.has_diffusione,
            features.has_lettori,
            features.has_dir_resp,
            features.has_quotidiano,
        ))

        if not has_metadata_marker:
            return False

        # 4. Normalizzazione del testo.
        text = re.sub(
            r"\s+",
            " ",
            region.text,
        ).strip().casefold()

        # 5. Campi tecnici espliciti, per esempio:
        # Tiratura: 15000
        # Diffusione: N.D.
        # Lettori: 50000
        # Superficie: 20%
        if re.search(
            r"\b(?:tiratura|diffusione|lettori|superficie)\s*[:=]",
            text,
        ):
            return True

        # 6. Indicazione del direttore responsabile:
        # Dir. Resp.: Mario Rossi
        if re.search(
            r"\bdir\.?\s*resp\.?\s*:",
            text,
        ):
            return True

        # 7. Indicazione della periodicità:
        # Quotidiano - Dir. Resp.: ...
        if re.match(
            r"^(?:quotidiano|settimanale|mensile)\s*[-–:]",
            text,
        ):
            return True

        # 8. Schede tecniche senza valori numerici:
        # Tiratura, diffusione e lettori non disponibili
        labels = set(re.findall(
            r"\b(?:tiratura|diffusione|lettori)\b",
            text,
        ))

        if len(labels) >= 2:
            unavailable = bool(re.search(
                r"\bnon disponibili\b|\bn\.?d\.?\b|\d",
                text,
            ))

            if unavailable:
                return True

        return False
