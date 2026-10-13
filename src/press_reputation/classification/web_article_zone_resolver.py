
from __future__ import annotations

import re

from press_reputation.models.page import (
    PageRecord,
    PageType,
    Region,
    RegionType,
)


class WebArticleZoneResolver:
    """
    Identifica gruppi narrativi e possibili header editoriali
    nelle pagine web, senza confermare prematuramente i ruoli.
    """

    MAX_PARAGRAPH_GAP = 40.0
    HEADER_LOOKBACK_RATIO = 0.25
    MIN_HORIZONTAL_OVERLAP = 0.55

    EXCLUDED_SCOPES = {
        "navigation",
        "boilerplate",
        "related",
        "advertisement",
        "non_main",
    }

    EXCLUDED_TYPES = {
        RegionType.NAVIGATION,
        RegionType.HEADER_METADATA,
        RegionType.FOOTER,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.IMAGE,
        RegionType.ADVERTISEMENT,
    }

    def enrich_document(self, pages: list[PageRecord]) -> None:
        for page in pages:
            if page.page_type == PageType.WEB:
                self.enrich_page(page)

    def enrich_page(self, page: PageRecord) -> None:
        body_regions = [
            r for r in page.regions
            if self._is_body_candidate(r)
        ]

        if not body_regions:
            return

        groups = self._group_body_regions(body_regions)

        for index, group in enumerate(groups, start=1):
            zone_id = f"web_zone_{page.pdf_page:03d}_{index}"

            bounds = self._bounds(group)

            for region in group:
                region.metadata["web_article_zone"] = {
                    "zone_id": zone_id,
                    "candidate_role": "body",
                    "status": "candidate",
                }

            headers = self._find_header_candidates(
                page, group, bounds
            )

            for region in headers:
                region.metadata["web_article_zone"] = {
                    "zone_id": zone_id,
                    "candidate_role": "header",
                    "status": "candidate",
                }

    def _is_body_candidate(self, region: Region) -> bool:
        if region.type != RegionType.ARTICLE_BODY:
            return False

        if not region.text or not region.bbox:
            return False

        if region.exclude_from_article_text:
            return False

        scope = region.metadata.get("content_scope")

        return scope not in self.EXCLUDED_SCOPES

    def _group_body_regions(
        self,
        regions: list[Region],
    ) -> list[list[Region]]:

        ordered = sorted(
            regions,
            key=lambda r: (r.bbox[1], r.bbox[0]),
        )

        groups: list[list[Region]] = []

        for region in ordered:
            best_group = None
            best_gap = float("inf")

            for group in groups:
                last = group[-1]

                gap = region.bbox[1] - last.bbox[3]
                overlap = self._horizontal_overlap(
                    region.bbox, last.bbox
                )

                if (
                    0 <= gap <= self.MAX_PARAGRAPH_GAP
                    and overlap >= self.MIN_HORIZONTAL_OVERLAP
                    and gap < best_gap
                ):
                    best_group = group
                    best_gap = gap

            if best_group is not None:
                best_group.append(region)
            else:
                groups.append([region])

        return groups

    def _find_header_candidates(
        self,
        page: PageRecord,
        body_group: list[Region],
        body_bounds: tuple,
    ) -> list[Region]:

        first_body_top = min(r.bbox[1] for r in body_group)

        max_lookback = max(
            120.0,
            page.page_height * self.HEADER_LOOKBACK_RATIO,
        )

        header_start = max(
            0.0,
            first_body_top - max_lookback,
        )

        candidates = []

        for region in page.regions:
            if region in body_group:
                continue

            if region.type in self.EXCLUDED_TYPES:
                continue

            if region.exclude_from_article_text:
                continue

            if not region.text or not region.bbox:
                continue

            if region.metadata.get("content_scope") in {
                "navigation", "boilerplate",
                "related", "advertisement",
            }:
                continue

            if not (
                header_start <= region.bbox[1]
                and region.bbox[3] <= first_body_top + 4.0
            ):
                continue

            if self._horizontal_overlap(
                region.bbox, body_bounds
            ) < 0.30:
                continue

            if self._is_ui_text(region.text):
                continue

            candidates.append(region)

        return candidates

    @staticmethod
    def _is_ui_text(text: str) -> bool:
        normalized = " ".join(text.casefold().split())

        if normalized in {
            "login", "abbonati", "registrati",
            "home", "menu", "condividi",
        }:
            return True

        if re.match(r"^https?://", normalized):
            return True

        return False

    @staticmethod
    def _horizontal_overlap(a: tuple, b: tuple) -> float:
        intersection = max(
            0.0,
            min(a[2], b[2]) - max(a[0], b[0]),
        )

        min_width = max(
            1.0,
            min(a[2] - a[0], b[2] - b[0]),
        )

        return intersection / min_width

    @staticmethod
    def _bounds(regions: list[Region]) -> tuple:
        return (
            min(r.bbox[0] for r in regions),
            min(r.bbox[1] for r in regions),
            max(r.bbox[2] for r in regions),
            max(r.bbox[3] for r in regions),
        )
