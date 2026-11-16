from __future__ import annotations
import hashlib
import re

from press_reputation.classification.scopes import is_blocked_scope
from press_reputation.core.config import ArticleClusteringConfig
from press_reputation.features.geometry import horizontal_overlap, vertical_gap
from press_reputation.core.lookup import text_similarity
from press_reputation.core.models.page import PageRecord, Region, RegionType

class ClusteringCore:
    #Helper condivisi usati da tutti i metodi di clustering
    
    EXCLUDED_TYPES = {
        RegionType.HEADER_METADATA,
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
        RegionType.FOOTER,
        RegionType.ADVERTISEMENT,
        RegionType.NAVIGATION,
        RegionType.RELATED_CONTENT,
    }

    MEDIA_TYPES = {
        RegionType.IMAGE,
        RegionType.INFOGRAPHIC,
        RegionType.TABLE,
        RegionType.PULL_QUOTE,
    }

    _SITE_SUFFIX = re.compile(
        r"[\s]*[-–—]\s*\S+\.(it|com|net|tv|eu|org|info)\s*$", re.IGNORECASE
    )
    
    def __init__(self, config: ArticleClusteringConfig | None = None) -> None:
        self.config = config or ArticleClusteringConfig()
        
    @staticmethod
    def region_id(region: Region) -> str | None:
        value = region.metadata.get("region_id")
        return value if isinstance(value, str) and value else None
    
    def eligible(self, region:Region) -> bool:
        return (
            region.type not in self.EXCLUDED_TYPES
            and not region.exclude_from_article_text
            and not region.metadata.get("inside_article_position_thumbnail")
            and not region.metadata.get("in_header_metadata_zone")
            and not is_blocked_scope(region)
        )
        
    @staticmethod
    def title_id(page: PageRecord, title: Region) -> str | None:
        region_id = title.metadata.get("region_id")
        if not isinstance(region_id, str) or not region_id:
            return None

        raw = (f"{page.document_id}\0{region_id}").encode()
        return "article_" + hashlib.sha256(raw).hexdigest()[:16]
    
    
    @staticmethod
    def record(
        region: Region,
        *,
        status: str,
        method: str,
        score: float | None = None,
        evidence: list[str] | None = None,
        alternatives: list[dict] | None = None,
        anchor_region_id: str | None = None,
    ) -> None:

        region.metadata["article_clustering"] = {
            "status": status,
            "method": method,
            "score": score,
            "evidence": evidence or [],
            "alternatives": alternatives or [],
            "anchor_region_id": anchor_region_id,
        }

    def assign(
        self,
        region: Region,
        article_id: str,
        *,
        method: str,
        score: float,
        evidence: list[str],
        anchor_region_id: str | None,
    ) -> bool:

        if region.article_id and region.article_id != article_id:
            self.record(
                region,
                status="conflict",
                method=method,
                evidence=[
                    f"existing:{region.article_id}",
                    f"proposed:{article_id}",
                ],
                anchor_region_id=anchor_region_id,
            )
            return False

        region.article_id = article_id
        self.record(
            region,
            status="assigned",
            method=method,
            score=round(score, 4),
            evidence=evidence,
            anchor_region_id=anchor_region_id,
        )

        return True
    
    @classmethod
    def _same_headline(cls, left: str | None, right: str | None) -> bool:
        """Headline quasi identiche (normalizzate) = stesso titolo ripetuto.

        Il layout di stampa puo' aggiungere il suffisso del sito
        ("titolo - sestante.tv"): si normalizza via prima del confronto.
        """
        if not left or not right:
            return False
        strip = lambda value: cls._SITE_SUFFIX.sub(
            "", " ".join(value.casefold().split())
        ).strip()
        a, b = strip(left), strip(right)
        if not a or not b:
            return False
        if a == b:
            return True
        return text_similarity(a, b) >= 0.90
    
    def title_barrier(
        self,
        page: PageRecord,
        anchor: Region,
        candidate: Region,
    ) -> bool:
        for other in page.regions:
            if (
                other is anchor
                or other is candidate
                or other.type != RegionType.ARTICLE_TITLE
                or other.metadata.get("title_role") in {"not_main", "ambiguous"}
                or
                # Un headline ripetuto della STESSA identita' non e' una barriera:
                # il body sotto il titolo replicato continua lo stesso articolo.
                (
                    other.article_id
                    and anchor.article_id
                    and other.article_id == anchor.article_id
                )
                or not other.has_valid_bbox
                or horizontal_overlap(other.bbox, candidate.bbox)
                < self.config.min_horizontal_overlap
            ):
                continue

            if anchor.bbox[3] <= other.bbox[1] <= candidate.bbox[1]:
                return True

        return False

    def score_body(
        self,
        candidate: Region,
        anchor: Region,
        assigned_bodies: list[Region],
        page: PageRecord,
        article_id: str,
        has_index_match: bool,
    ) -> tuple[float, list[str]]:
        if not (
            candidate.has_valid_bbox
            and anchor.has_valid_bbox
            and page.page_height
            and page.page_width
        ):
            return 0.0, ["missing_geometry"]

        if candidate.bbox[1] < anchor.bbox[1] or self.title_barrier(
            page, anchor, candidate
        ):
            return 0.0, ["title_barrier"]

        references = [body for body in assigned_bodies if body.has_valid_bbox] or [
            anchor
        ]

        best = max(
            references,
            key=lambda ref: (
                horizontal_overlap(candidate.bbox, ref.bbox),
                vertical_gap(candidate.bbox, ref.bbox),
            ),
        )

        horizontal = horizontal_overlap(candidate.bbox, best.bbox)
        if horizontal < self.config.min_horizontal_overlap:
            return 0.0, ["different_column"]

        gap = vertical_gap(candidate.bbox, best.bbox) / page.page_height
        if gap > self.config.max_local_gap_fraction:
            return 0.0, ["too_distant"]

        score = 0.25 * horizontal + 0.24 * (
            1 - gap / self.config.max_local_gap_fraction
        )
        evidence = ["horizontal_overlap", "local_vertical_continuity"]

        left_difference = abs(candidate.bbox[0] - best.bbox[0]) / page.page_width
        if left_difference <= self.config.max_column_edge_difference:
            score += 0.16
            evidence.append("compatible_column_edge")

        if candidate.bbox[1] >= anchor.bbox[3]:
            score += 0.12
            evidence.append("after_title")

        candidate_font = candidate.style.get("dominant_font")
        reference_font = best.style.get("dominant_font")

        if (
            assigned_bodies
            and candidate_font
            and reference_font
            and candidate_font == reference_font
        ):
            score += 0.08
            evidence.append("same_body_font")

        candidate_size = candidate.style.get("median_font_size")
        reference_size = best.style.get("median_font_size")

        if (
            assigned_bodies
            and candidate_size
            and reference_size
            and abs(candidate_size - reference_size)
            / max(candidate_size, reference_size)
            <= self.config.max_font_size_ratio_difference
        ):
            score += 0.06
            evidence.append("compatible_body_font_size")

        web_id = candidate.metadata.get("article_candidate_id")

        if web_id:
            if web_id != article_id:
                return 0.0, ["different_web_candidate_id"]

            score += 0.18
            evidence.append("same_web_candidate_id")

        if has_index_match:
            score += 0.04
            evidence.append("matched_review_index_title")

        return min(score, 1.0), evidence
    
    
    @staticmethod
    def source_span(region: Region) -> tuple[str, str, int, str, int, int] | None:
        method = region.extraction_method
        if method not in {"ocr", "pdf_text"}:
            return None

        matching = [
            prov
            for prov in region.provenance
            if prov.get("self_ref") and prov.get("extraction_method") == method
        ]
        if len(matching) != 1:
            return None

        prov = matching[0]
        page_no = prov.get("page")
        if type(page_no) is not int:
            return None

        extraction_pass = prov.get("extraction_pass")
        if method == "ocr" and not extraction_pass:
            # Senza identificare il passaggio OCR non considerare
            # self_ref una prova forte di continuità.
            return None

        pass_key = extraction_pass or "pdf_native"
        recovery = region.metadata.get("text_fragment_recovery") or {}
        if recovery.get("status") == "ambiguous":
            return None

        span = recovery.get("effective_charspan")
        if span is None:
            span = prov.get("charspan")

        if (
            not isinstance(span, (tuple, list))
            or len(span) != 2
            or type(span[0]) is not int
            or type(span[1]) is not int
            or not 0 <= span[0] < span[1]
        ):
            return None

        return method, pass_key, page_no, prov["self_ref"], span[0], span[1]
    
    def same_source_continuation(
        self, previous: list[Region], following: list[Region]
    ) -> bool:
        for left in previous:
            left_span = self.source_span(left)
            if left_span is None:
                continue

            for right in following:
                right_span = self.source_span(right)
                if right_span is None:
                    continue

                # Method, extraction pass, pagina e self_ref devono
                # coincidere; poi si confrontano gli offset effettivi.
                if left_span[:4] != right_span[:4]:
                    continue

                gap = right_span[4] - left_span[5]
                if 0 <= gap <= self.config.max_same_source_charspan_gap:
                    return True

        return False








