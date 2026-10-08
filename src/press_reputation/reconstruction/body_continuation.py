import heapq
import re
from math import isfinite

from press_reputation.config import BodyContinuationConfig
from press_reputation.features import RegionFeatureExtractor
from press_reputation.models.page import PageRecord, PageType, Region, RegionType


class BodyContinuationResolver:
    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation"
    }

    BARRIERS = {
        RegionType.ARTICLE_TITLE,
        RegionType.ARTICLE_SUBTITLE,
        RegionType.AUTHOR,
    }

    def __init__(self, config: BodyContinuationConfig | None = None) -> None:
        self.config = config or BodyContinuationConfig()
        self.feature_extractor = RegionFeatureExtractor()

    @staticmethod
    def valid_bbox(region: Region) -> bool:
        box = region.bbox

        return (box is not None and len(box) == 4 and all(isfinite(value) for value in box) and box[2] > box[0] and box[3] > box[1])

    def allowed_scope(self, region: Region, page: PageRecord) -> bool:
        scope = region.metadata.get("content_scope")

        return (scope not in self.BLOCKED_SCOPES and (page.page_type != PageType.WEB or scope == "main"))

    def eligible_candidate(self, region: Region, page: PageRecord) -> bool:
        if (region.type != RegionType.UNKNOWN or not region.text or not self.valid_bbox(region) or region.exclude_from_article_text or
            region.metadata.get("in_header_metadata_zone") or region.metadata.get("inside_article_position_thumbnail") or
            not self.allowed_scope(region, page)):
            
            return False

        features = self.feature_extractor.extract(region, page, include_entity=False)

        if (features.has_url or features.has_navigation_marker or features.has_related_marker or features.has_newsletter or
            features.has_ad_marker or features.has_rights_notice_marker or features.has_watermark_marker or
            features.has_foglio or features.has_surface):
            return False

        # Lasciare al SectionHeaderResolver le heading brevi:
        # un frammento dopo un body non è automaticamente body.
        if (region.raw_label == "section_header" and features.word_count <= self.config.max_heading_candidate_words):
            return False

        if (features.has_author_marker and features.word_count <= self.config.max_heading_candidate_words):
            return False

        return True

    def eligible_reference(self, region: Region, page: PageRecord) -> bool:
        return (region.type == RegionType.ARTICLE_BODY and region.article_id is not None and self.valid_bbox(region) and
                not region.exclude_from_article_text and self.allowed_scope(region, page))

    @staticmethod
    def horizontal_overlap(candidate: Region, reference: Region) -> float:
        shared = max(0.0, min(candidate.bbox[2], reference.bbox[2]) - max(candidate.bbox[0], reference.bbox[0]))
        width = min(candidate.bbox[2] - candidate.bbox[0], reference.bbox[2] - reference.bbox[0])

        return shared / width if width > 0 else 0.0

    @staticmethod
    def width_similarity(candidate: Region, reference: Region) -> float:
        candidate_width = candidate.bbox[2] - candidate.bbox[0]
        reference_width = reference.bbox[2] - reference.bbox[0]

        return min(candidate_width, reference_width) / max(candidate_width, reference_width, 1.0)

    @staticmethod
    def normalized_font(value: str | None) -> str | None:
        if not value:
            return None

        # I prefissi dei subset PDF possono variare pur usando
        # lo stesso font embedded.
        return re.sub(r"^[A-Z]{6}\+", "", value).casefold()

    @staticmethod
    def font_similarity(candidate: Region, reference: Region) -> float:
        left = BodyContinuationResolver.normalized_font(
            candidate.style.get("dominant_font")
        )
        right = BodyContinuationResolver.normalized_font(
            reference.style.get("dominant_font")
        )

        if not left or not right:
            return 0.5

        return 1.0 if left == right else 0.0

    @staticmethod
    def font_size_similarity(candidate: Region, reference: Region) -> float:
        left = candidate.style.get("median_font_size")
        right = reference.style.get("median_font_size")

        if not left or not right:
            return 0.5

        return min(left, right) / max(left, right)

    def trait_similarity(self, candidate: Region, reference: Region, trait: str) -> float:
        left = candidate.style.get(f"{trait}_ratio")
        right = reference.style.get(f"{trait}_ratio")

        left_evidence = candidate.style.get(f"{trait}_evidence_fraction", 0.0)
        right_evidence = reference.style.get(f"{trait}_evidence_fraction", 0.0)

        if (left is None or right is None or left_evidence < self.config.min_style_evidence_fraction or
            right_evidence < self.config.min_style_evidence_fraction):
            return 0.5

        return max(0.0, 1.0 - abs(left - right))

    def crosses_article_barrier(self, candidate: Region, reference: Region, page: PageRecord) -> bool:
        gap_top = reference.bbox[3]
        gap_bottom = candidate.bbox[1]

        if gap_bottom <= gap_top:
            return False

        for other in page.regions:
            if (other is candidate or other is reference or
                not self.valid_bbox(other) or self.horizontal_overlap(candidate, other) < self.config.min_horizontal_overlap):
                continue

            if (other.bbox[1] < gap_bottom and other.bbox[3] > gap_top):
                if (other.type in self.BARRIERS and other.article_id != reference.article_id):
                    return True

                if (other.type == RegionType.ARTICLE_BODY and other.article_id and other.article_id != reference.article_id):
                    return True

        return False

    def pair_score(self, candidate: Region, reference: Region, page: PageRecord) -> tuple[float, dict[str, float]] | None:
        if (not reference.article_id or (candidate.article_id and candidate.article_id != reference.article_id)):
            return None

        overlap = self.horizontal_overlap(candidate, reference)
        left_difference = abs(candidate.bbox[0] - reference.bbox[0])

        if (overlap < self.config.min_horizontal_overlap or left_difference > self.config.max_column_left_difference):
            return None

        # Compatibilità con il reading order locale:
        # procede verso il basso nella stessa colonna.
        # body_reading_order non è ancora disponibile:
        # BodyGroupingResolver viene eseguito dopo questo step.
        gap = candidate.bbox[1] - reference.bbox[3]

        if not (-self.config.max_vertical_overlap <= gap <= self.config.max_forward_gap):
            return None

        if self.crosses_article_barrier(candidate, reference, page):
            return None

        forward_proximity = (1.0 if gap <= 0 else max(0.0, 1.0 - gap / self.config.max_forward_gap))
        column_alignment = max(0.0, 1.0 - left_difference / self.config.max_column_left_difference)

        components = {
            "same_column": (
                0.17 * column_alignment
            ),
            "horizontal_overlap": (
                0.14 * overlap
            ),
            "forward_proximity": (
                0.22 * forward_proximity
            ),
            "width_similarity": (
                0.11 * self.width_similarity(candidate, reference)
            ),
            "dominant_font": (
                0.10 * self.font_similarity(candidate, reference)
            ),
            "font_size": (
                0.10 * self.font_size_similarity(candidate, reference)
            ),
            "bold": (
                0.03 * self.trait_similarity(candidate, reference, "bold")
            ),
            "italic": (
                0.03 * self.trait_similarity(candidate, reference, "italic")
            ),
            "reading_order_compatible": 0.10,
        }

        return round(sum(components.values()), 4), components

    def enrich(self, page: PageRecord) -> PageRecord:
        references = [region for region in page.regions if self.eligible_reference(region, page)]
        candidates = {index: region for index, region in enumerate(page.regions) if self.eligible_candidate(region, page)}

        if not references or not candidates:
            return page

        # candidate_index → article_id → (score, reference, components)
        best_by_candidate: dict[int, dict[str, tuple[float, Region, dict[str, float]]]] = {index: {} for index in candidates}

        # Una versione invalida gli score precedenti nello heap.
        versions = {index: 0 for index in candidates}
        frontier: list[tuple[float, int, int]] = []
        round_number = 0

        def offer(index: int, candidate: Region, reference: Region) -> None:
            result = self.pair_score(candidate, reference, page)
            if result is None:
                return

            score, components = result
            article_id = reference.article_id
            previous = best_by_candidate[index].get(article_id)

            if (previous is not None and previous[0] >= score):
                return

            best_by_candidate[index][article_id] = (score, reference, components)
            versions[index] += 1

            top_score = max(value[0] for value in best_by_candidate[index].values())
            heapq.heappush(frontier, (-top_score, index, versions[index]))

        for index, candidate in candidates.items():
            for reference in references:
                offer(index, candidate, reference)

        while frontier:
            negative_score, index, version = (heapq.heappop(frontier))

            if (index not in candidates or version != versions[index]):
                continue

            ranked = sorted(best_by_candidate[index].items(), key=lambda item: (-item[1][0], item[0]))
            if not ranked:
                continue

            article_id, (score, reference, components) = ranked[0]
            second_score = (ranked[1][1][0] if len(ranked) > 1 else 0.0)

            if (score < self.config.min_score or ( candidates[index].article_id is None and score - second_score < self.config.min_winning_margin)):
                # Non eliminare il candidato: se viene promosso
                # un nuovo riferimento, offer() lo rivaluterà.
                continue

            candidate = candidates.pop(index)
            round_number += 1

            candidate.type = RegionType.ARTICLE_BODY
            candidate.article_id = article_id
            candidate.metadata.update(
                {
                    "body_role": "continuation",
                    "body_detection_method": (
                        "iterative_article_aware_v2"
                    ),
                    "body_continuation_score": score,
                    "body_continuation_round": (
                        round_number
                    ),
                    "body_continuation_reference_region_id": (
                        reference.metadata.get("region_id")
                    ),
                    "body_continuation_evidence": (
                        components
                    ),
                }
            )

            candidate.metadata.setdefault(
                "article_clustering",
                {
                    "status": "assigned",
                    "method": (
                        "iterative_body_continuation_v2"
                    ),
                    "score": score,
                    "evidence": [
                        "same_article_reference",
                        "compatible_local_reading_order",
                    ],
                    "anchor_region_id": (
                        reference.metadata.get("region_id")
                    ),
                },
            )

            if page.page_type == PageType.WEB:
                candidate.metadata["include_in_main_body"] = True

            references.append(candidate)

            # Immediato: il frammento appena promosso può
            # offrire un nuovo score a tutti i restanti UNKNOWN.
            for other_index, other in candidates.items():
                offer(other_index, other, candidate)

        for index, candidate in candidates.items():
            ranked = sorted(best_by_candidate[index].items(), key=lambda item: (-item[1][0], item[0]))

            if ranked:
                candidate.metadata["body_continuation_candidates"] = [
                    {
                        "article_id": article_id,
                        "score": score,
                        "reference_region_id": (
                            reference.metadata.get("region_id")
                        ),
                    }
                    for article_id, (score, reference, _) in ranked[:3]
                ]

        return page