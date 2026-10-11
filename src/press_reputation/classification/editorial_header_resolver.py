import re

from press_reputation.config import EditorialHeaderConfig
from press_reputation.lookup import resolve_entity
from press_reputation.models.page import PageRecord, Region, RegionType


class EditorialHeaderResolver:
    def __init__(self, config: EditorialHeaderConfig | None = None) -> None:
        self.config = config or EditorialHeaderConfig()

    @staticmethod
    def overlap(left: Region, right: Region) -> float:
        if not left.bbox or not right.bbox:
            return 0.0
        shared = max(0.0, min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0]))
        width = min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0])
        return shared / width if width > 0 else 0.0

    def preceding_titles(self, region: Region, page: PageRecord) -> list[Region]:
        if not region.bbox:
            return []

        return [
            title for title in page.regions
            if title is not region and title.type == RegionType.ARTICLE_TITLE and title.bbox
            and title.metadata.get("title_role") not in {"not_main", "ambiguous"}
            and not title.exclude_from_article_text
            and 0 <= title.bbox[1] - region.bbox[3] <= self.config.max_header_gap
            and self.overlap(region, title) >= self.config.min_horizontal_overlap
        ]

    @staticmethod
    def standalone_place(text: str) -> str | None:
        value = text.strip()
        # Solo una regione autonoma: non estrarre una città dal prefisso
        # di un paragrafo narrativo.
        value = re.sub(r"\s*[,–—-]\s*$", "", value)
        match = resolve_entity(value)
        return match.canonical_name if match and match.kind == "location" and not match.ambiguous else None

    def enrich(self, page: PageRecord) -> PageRecord:
        for region in page.regions:
            if (
                region.type not in {RegionType.UNKNOWN, RegionType.LOCATION, RegionType.ARTICLE_TITLE}
                or not region.text or not region.bbox or region.exclude_from_article_text
                or region.metadata.get("in_header_metadata_zone")
                or region.metadata.get("inside_article_position_thumbnail")
            ):
                continue

            # Un marker tecnico non diventa né rubrica né dateline.
            lowered = region.text.casefold()
            if any(marker in lowered for marker in ("data stampa", "articolo non cedibile", "uso esclusivo")):
                continue

            titles = self.preceding_titles(region, page)
            if len(titles) != 1:
                continue
            title = titles[0]
            title_id = title.metadata.get("region_id")
            words = region.text.split()

            place = self.standalone_place(region.text) if len(words) <= self.config.max_dateline_words else None
            if place and region.type in {RegionType.UNKNOWN, RegionType.LOCATION}:
                previous_type = region.type.value
                region.type = RegionType.LOCATION
                region.metadata["location_role"] = "dateline"
                region.metadata["editorial_header_resolution"] = {
                    "method": "standalone_dateline_v1",
                    "canonical_place": place,
                    "title_region_id": title_id,
                    "previous_type": previous_type,
                }
                continue
            
            cleanup = region.metadata.get("watermark_text_cleanup") or {}
            if cleanup.get("residual_role_hint") == "short_header_candidate":
                region.metadata["editorial_header_resolution"] = {
                    "status": "ambiguous",
                    "method": "watermark_residual_header_v1",
                    "reason": "place_not_confirmed_after_ocr_cleanup",
                    "title_region_id": title_id,
                }
                continue

            if not 1 <= len(words) <= self.config.max_kicker_words:
                continue

            candidate_score = region.metadata.get("title_candidate", {}).get("score", 0.0)
            title_score = title.metadata.get("title_candidate", {}).get("score", 0.0)
            if title_score - candidate_score < self.config.min_title_score_margin:
                continue

            if region.type == RegionType.ARTICLE_TITLE and region.raw_label != "section_header":
                continue

            # Ripetizione alla stessa posizione suggerisce rubrica, non è
            # motivo per escludere la regione dall'articolo.
            fingerprint = region.metadata.get("boilerplate_detection") or {}
            subrole = "rubric" if fingerprint.get("repeated") else "kicker"
            previous_type = region.type.value

            region.type = RegionType.ARTICLE_KICKER
            region.metadata["editorial_subrole"] = subrole
            region.metadata["editorial_header_resolution"] = {
                "method": "standalone_kicker_v1",
                "title_region_id": title_id,
                "previous_type": previous_type,
                "title_score": title_score,
                "candidate_score": candidate_score,
            }

        return page