import re
from dataclasses import dataclass
from math import isfinite
from statistics import median
from urllib.parse import urlsplit, urlunsplit

from press_reputation.classification.web_content_config import (
    WebContentConfig,
)
from press_reputation.models.page import PageRecord, PageType, Region, RegionType


def normalize_text(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def word_count(text: str | None) -> int:
    return len(re.findall(r"\w+", text or ""))


def valid_bbox(region: Region) -> bool:
    box = region.bbox

    return (box is not None and len(box) == 4 and all(isfinite(value) for value in box) and box[2] > box[0] and box[3] > box[1])


def horizontal_overlap(a: tuple, b: tuple) -> float:
    intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    denominator = min(a[2] - a[0], b[2] - b[0])

    return intersection / denominator if denominator > 0 else 0.0


def normalized_box(region: Region, page: PageRecord) -> tuple:
    x0, y0, x1, y1 = region.bbox
    return (
        x0 / page.page_width,
        y0 / page.page_height,
        x1 / page.page_width,
        y1 / page.page_height,
    )


def article_url_key(url: str | None) -> str | None:
    """
    Conserva path e query: non usare il solo dominio per unire articoli.
    """
    if not url:
        return None

    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.path in {"", "/"}
    ):
        return None

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


@dataclass(frozen=True)
class WebPageContext:
    pdf_page: int
    url_key: str | None
    column: tuple[float, float, float, float]
    layout_area_id: str


class WebMainContentResolver:
    TECHNICAL_TYPES = {
        RegionType.HEADER_METADATA,
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.FOOTER,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
    }

    EXTERNAL_TYPES = {
        RegionType.NAVIGATION,
        RegionType.RELATED_CONTENT,
        RegionType.ADVERTISEMENT,
    }

    def __init__(self, config: WebContentConfig | None = None) -> None:
        self.config = config or WebContentConfig()

    def enrich_document(self, pages: list[PageRecord]) -> list[PageRecord]:
        # Stato isolato per documento, anche se il chiamante passa più documenti.
        previous: dict[str, WebPageContext] = {}

        for page in sorted(
            pages,
            key=lambda item: (item.document_id, item.pdf_page),
        ):
            if page.page_type != PageType.WEB:
                previous.pop(page.document_id, None)
                continue

            context = self.enrich_page(
                page,
                previous.get(page.document_id),
            )

            if context is None:
                previous.pop(page.document_id, None)
            else:
                previous[page.document_id] = context

        return pages

    def enrich_page(
        self,
        page: PageRecord,
        previous: WebPageContext | None = None,
    ) -> WebPageContext | None:
        for region in page.regions:
            region.metadata["content_scope"] = "unknown"
            region.metadata["content_scope_reason"] = "not_resolved"
            region.metadata["include_in_main_body"] = False
            region.metadata.pop("layout_area_id", None)

            # if (
            #     region.type in self.TECHNICAL_TYPES
            #     or region.exclude_from_article_text
            #     or region.metadata.get("inside_article_position_thumbnail")
            # ):
            #     self.assign(region, "boilerplate", "technical_or_excluded")

            # elif region.type in self.EXTERNAL_TYPES:
            #     scope = {
            #         RegionType.ADVERTISEMENT: "advertisement",
            #         RegionType.RELATED_CONTENT: "related",
            #         RegionType.NAVIGATION: "non_main",
            #     }[region.type]
            #     self.assign(region, scope, "existing_semantic_label")
            
            if region.type in self.EXTERNAL_TYPES:
                scope = {
                    RegionType.ADVERTISEMENT: "advertisement",
                    RegionType.RELATED_CONTENT: "related",
                    RegionType.NAVIGATION: "non_main",
                }[region.type]

                self.assign(region, scope, "existing_semantic_label")

            elif (
                region.type in self.TECHNICAL_TYPES
                or region.exclude_from_article_text
                or region.metadata.get("inside_article_position_thumbnail")
            ):
                self.assign(region, "boilerplate", "technical_or_excluded")

        if (
            not page.page_width
            or not page.page_height
            or page.page_width <= 0
            or page.page_height <= 0
        ):
            return None

        usable = [
            region
            for region in page.regions
            if valid_bbox(region)
            and region.metadata["content_scope"] == "unknown"
        ]

        module_starts = [
            normalized_box(region, page)[1]
            for region in usable
            if self.is_related_module_start(region, usable, page)
        ]
        stop_y = min(module_starts) if module_starts else None

        # Se c'è una struttura di modulo correlato, escludiamo il modulo,
        # non tutto ciò che è genericamente sotto il titolo.
        if stop_y is not None:
            for region in usable:
                if normalized_box(region, page)[1] >= stop_y:
                    self.assign(
                        region,
                        "related",
                        "related_module_with_image_and_short_text",
                    )

        seeds = [
            region
            for region in usable
            if self.is_body_seed(region, page)
            and region.metadata["content_scope"] == "unknown"
        ]

        if not seeds:
            return None

        column, column_seeds = self.select_column(seeds, page)

        titles = [
            region
            for region in usable
            if region.type == RegionType.ARTICLE_TITLE
            and region.metadata["content_scope"] == "unknown"
            and horizontal_overlap(
                normalized_box(region, page), column
            ) >= self.config.minimum_horizontal_overlap
            and normalized_box(region, page)[3]
            <= max(normalized_box(seed, page)[1] for seed in column_seeds)
        ]

        # Con più titoli concorrenti non scegliamo arbitrariamente
        # il primo articolo della pagina.
        if len(titles) > 1:
            for seed in column_seeds:
                self.assign(seed, "unknown", "multiple_title_candidates")
            return None

        current_url = article_url_key(page.source.url)
        confirmed = False

        if len(titles) == 1:
            start_y = normalized_box(titles[0], page)[1]
            area_id = f"web_area_page_{page.pdf_page:03d}"
            confirmed = True
            reason = "title_and_body_column"
        else:
            start_y = min(
                normalized_box(seed, page)[1]
                for seed in column_seeds
            )
            area_id = f"web_candidate_page_{page.pdf_page:03d}"
            reason = "body_column_without_confirmed_article_header"

            if (
                previous is not None
                and previous.pdf_page + 1 == page.pdf_page
                and current_url is not None
                and current_url == previous.url_key
                and abs(column[0] - previous.column[0])
                <= self.config.left_alignment_tolerance
                and abs(column[2] - previous.column[2])
                <= self.config.width_tolerance
            ):
                confirmed = True
                area_id = previous.layout_area_id
                reason = "adjacent_page_same_article_url_and_column"

        for region in usable:
            if region.metadata["content_scope"] != "unknown":
                continue

            box = normalized_box(region, page)

            if box[3] < start_y:
                self.assign(region, "non_main", "before_main_content")
                continue

            overlap = horizontal_overlap(box, column)

            if overlap == 0:
                self.assign(region, "non_main", "outside_main_column")
                continue

            if overlap < self.config.minimum_horizontal_overlap:
                continue

            region.metadata["layout_area_id"] = area_id

            if region in column_seeds:
                self.assign(
                    region,
                    "main" if confirmed else "unknown",
                    reason,
                )
                if not confirmed:
                    region.metadata["main_content_candidate"] = True

            elif region.type in {
                RegionType.ARTICLE_TITLE,
                RegionType.ARTICLE_SUBTITLE,
                RegionType.AUTHOR,
            } and confirmed:
                self.assign(region, "main", "article_header_in_main_column")

        if not confirmed:
            return None

        return WebPageContext(
            pdf_page=page.pdf_page,
            url_key=current_url,
            column=column,
            layout_area_id=area_id,
        )

    def is_body_seed(self, region: Region, page: PageRecord) -> bool:
        if region.type != RegionType.ARTICLE_BODY:
            return False

        text = region.text or ""

        if word_count(text) < self.config.seed_min_words:
            return False

        if "http://" in text or "https://" in text:
            return False

        box = normalized_box(region, page)
        return box[2] - box[0] >= self.config.minimum_column_width

    def select_column(
        self,
        seeds: list[Region],
        page: PageRecord,
    ) -> tuple[tuple, list[Region]]:
        clusters: list[list[Region]] = []

        for seed in sorted(seeds, key=lambda region: region.bbox[0]):
            box = normalized_box(seed, page)

            for cluster in clusters:
                left = median(
                    normalized_box(region, page)[0]
                    for region in cluster
                )
                width = median(
                    normalized_box(region, page)[2]
                    - normalized_box(region, page)[0]
                    for region in cluster
                )

                if (
                    abs(box[0] - left)
                    <= self.config.left_alignment_tolerance
                    and abs((box[2] - box[0]) - width)
                    <= self.config.width_tolerance
                ):
                    cluster.append(seed)
                    break
            else:
                clusters.append([seed])

        selected = max(
            clusters,
            key=lambda cluster: sum(
                word_count(region.text) for region in cluster
            ),
        )

        left = median(
            normalized_box(region, page)[0] for region in selected
        )
        right = median(
            normalized_box(region, page)[2] for region in selected
        )

        return (left, 0.0, right, 1.0), selected

    def is_related_module_start(
        self,
        marker: Region,
        regions: list[Region],
        page: PageRecord,
    ) -> bool:
        text = normalize_text(marker.text).strip(" :›>")

        if text not in self.config.module_markers:
            return False

        marker_box = normalized_box(marker, page)

        nearby = [
            region
            for region in regions
            if region is not marker
            and 0 <= (
                normalized_box(region, page)[1] - marker_box[3]
            ) <= self.config.module_search_height
            and horizontal_overlap(
                normalized_box(region, page), marker_box
            ) > 0
        ]

        has_image = any(
            region.type == RegionType.IMAGE
            for region in nearby
        )

        has_short_text = any(
            region.type != RegionType.IMAGE
            and 4 <= word_count(region.text) <= 35
            for region in nearby
        )

        # "VIDEO" da solo non basta.
        return has_image and has_short_text

    @staticmethod
    def assign(region: Region, scope: str, reason: str) -> None:
        region.metadata["content_scope"] = scope
        region.metadata["content_scope_reason"] = reason
        region.metadata["include_in_main_body"] = (
            scope == "main"
            and region.type == RegionType.ARTICLE_BODY
        )