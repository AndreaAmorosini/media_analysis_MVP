from collections import defaultdict
from statistics import median

from press_reputation.config import (ArticleReadingOrderConfig)
from press_reputation.models.page import (PageRecord, Region, RegionType)
from press_reputation.reconstruction.body_grouping import (horizontal_overlap, valid_bbox)
from press_reputation.reconstruction.flow_models import (ArticleReadingOrder, ReadingOrderSegment)


class ArticleReadingOrderResolver:
    TEXT_TYPES = {
        RegionType.ARTICLE_BODY,
        RegionType.ARTICLE_SECTION_HEADER,
    }

    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation"
    }

    def __init__(self, config: ArticleReadingOrderConfig | None = None) -> None:
        self.config = (config or ArticleReadingOrderConfig())

    @staticmethod
    def region_id(region: Region) -> str | None:
        value = region.metadata.get("region_id")
        return (value if isinstance(value, str) and value else None)

    def eligible(self, region: Region) -> bool:
        return (region.article_id is not None and region.type in self.TEXT_TYPES and bool((region.text or "").strip()) and
                not region.exclude_from_article_text and not region.metadata.get("in_header_metadata_zone") and
                not region.metadata.get("inside_article_position_thumbnail") and region.metadata.get("content_scope") not in self.BLOCKED_SCOPES)

    def detect_columns(self, regions: list[Region]) -> list[list[Region]]:
        bodies = [region for region in regions if region.type == RegionType.ARTICLE_BODY and valid_bbox(region)]
        if not bodies:
            return []

        widths = [region.bbox[2] - region.bbox[0] for region in bodies]
        typical_width = median(widths)

        # Non usare un body evidentemente largo per
        # definire due colonne come un'unica colonna.
        narrow = [region for region in bodies if (len(bodies) < 3 or
                                                  region.bbox[2] - region.bbox[0] <= typical_width * self.config.wide_region_width_ratio)]

        columns: list[list[Region]] = []

        for region in sorted(narrow, key=lambda item: (item.bbox[0], item.bbox[1])):
            selected = None
            best_overlap = 0.0

            for column in columns:
                left = median(item.bbox[0] for item in column)
                right = median(item.bbox[2] for item in column)
                reference = [left, 0.0, right, 1.0]
                overlap = horizontal_overlap(region.bbox, reference)
                tolerance = max(self.config.column_left_tolerance, (region.bbox[2] - region.bbox[0]) * 0.12)

                if (abs(region.bbox[0] - left) <= tolerance and overlap >= self.config.min_column_overlap and overlap > best_overlap):
                    selected = column
                    best_overlap = overlap

            if selected is None:
                columns.append([region])
            else:
                selected.append(region)

        return sorted(columns, key=lambda column: median(region.bbox[0] for region in column))

    def column_boxes(self, columns: list[list[Region]]) -> list[list[float]]:
        return [[median(item.bbox[0] for item in column), 0.0, median(item.bbox[2] for item in column), 1.0] for column in columns]

    def is_spanning(self, region: Region, boxes: list[list[float]]) -> bool:
        if len(boxes) < 2:
            return False

        widths = [box[2] - box[0] for box in boxes]
        width = region.bbox[2] - region.bbox[0]

        if width < (median(widths) * self.config.wide_region_width_ratio):
            return False

        covered_columns = sum(horizontal_overlap(region.bbox, box) >= self.config.spanning_column_overlap for box in boxes)
        return covered_columns >= 2

    @staticmethod
    def sort_key(region: Region) -> tuple:
        return (region.bbox[1], region.bbox[0], region.metadata.get("region_id") or "")

    def ordered_page_regions(self, page: PageRecord, regions: list[Region], warnings: list[str]) -> list[tuple[Region, int | None, float]]:
        with_box = [region for region in regions if valid_bbox(region)]
        without_box = [region for region in regions if not valid_bbox(region)]

        columns = self.detect_columns(with_box)
        boxes = self.column_boxes(columns)

        spanning = sorted((region for region in with_box if self.is_spanning(region, boxes)), key=self.sort_key)
        spanning_ids = {
            id(region) for region in spanning
        }

        # I segmenti spanning dividono la pagina in
        # bande. Dentro ogni banda: colonne da sinistra
        # a destra, poi regioni dall'alto in basso.
        bands: list[list[tuple[Region, int | None, float]]] = [[] for _ in range(len(spanning) + 1)]

        for region in with_box:
            if id(region) in spanning_ids:
                continue

            overlaps = [horizontal_overlap(region.bbox, box) for box in boxes]
            if overlaps:
                column_index = max(range(len(overlaps)), key=lambda index: (overlaps[index], -index))
                column_number = column_index + 1
                confidence = (0.85 if overlaps[column_index] >= self.config.min_column_overlap else 0.45)
            else:
                column_number = None
                confidence = 0.35

            center_y = (region.bbox[1] + region.bbox[3]) / 2
            band_index = 0

            for separator in spanning:
                separator_center = (separator.bbox[1] + separator.bbox[3]) / 2
                if center_y > separator_center:
                    band_index += 1

                if (region.bbox[1] < separator.bbox[3] and region.bbox[3] > separator.bbox[1]):
                    confidence = min(confidence, 0.40)
                    warnings.append(f"Region overlaps spanning region on page {page.pdf_page}")

            if (region.type == RegionType.ARTICLE_SECTION_HEADER and
                region.metadata.get("section_header_resolution", {}).get("association_status") == "provisional_local_body_pair"):
                
                confidence = min(confidence, 0.65)

            bands[band_index].append((region, column_number, confidence))

        ordered: list[tuple[Region, int | None, float]] = []

        for band_index, band in enumerate(bands):
            ordered.extend(
                sorted(band, key=lambda item: (item[1] if item[1] is not None else
                                                len(boxes) + 1, item[0].bbox[1], item[0].bbox[0], self.region_id(item[0]) or ""))
            )

            if band_index < len(spanning):
                ordered.append((spanning[band_index], None, 0.70))

        if without_box:
            warnings.append(
                f"{len(without_box)} text regions without valid bbox on page {page.pdf_page}; appended with low confidence"
            )
            ordered.extend(
                (region, None, 0.15)
                for region in sorted(
                    without_box,
                    key=lambda item: (
                        self.region_id(item)
                        or "",
                        item.text or "",
                    ),
                )
            )

        return ordered

    def resolve(self, pages: list[PageRecord]) -> dict[tuple[str, str], ArticleReadingOrder]:
        groups: dict[tuple[str, str], list[tuple[PageRecord, Region]]] = defaultdict(list)

        for page in pages:
            for region in page.regions:
                region.metadata.pop("article_reading_order", None)
                if self.eligible(region):
                    groups[(page.document_id, region.article_id)].append((page, region))

        results = {}

        for (document_id, article_id), members in groups.items():
            warnings: list[str] = []
            segments: list[ReadingOrderSegment] = []
            order = 0

            article_pages = sorted({page.pdf_page for page, _ in members})

            for pdf_page in article_pages:
                page = next(page for page, _ in members if page.pdf_page == pdf_page)
                page_regions = [region for member_page, region in members if member_page.pdf_page == pdf_page]
                ordered = (self.ordered_page_regions(page, page_regions, warnings))
                
                previous_column: int | None = None
                previous: Region | None = None

                for (region, column, confidence) in ordered:
                    region_id = self.region_id(region)
                    if not region_id:
                        warnings.append(
                            f"Skipped region without stable ID on page {pdf_page}"
                        )
                        continue

                    if (previous is not None and column is not None and previous_column == column and valid_bbox(previous) and
                        valid_bbox(region) and (region.bbox[1] - previous.bbox[3]) > self.config.suspicious_vertical_gap):
                        warnings.append(
                            f"Large vertical gap before {region_id}"
                        )
                        confidence = min(confidence, 0.55)

                    order += 1
                    segment = ReadingOrderSegment(
                        article_id=article_id,
                        region_id=region_id,
                        type=region.type,
                        pdf_page=pdf_page,
                        column=column,
                        order=order,
                        bbox=region.bbox,
                        text=region.text,
                        confidence=confidence,
                        provenance=region.provenance,
                    )
                    segments.append(segment)

                    region.metadata["article_reading_order"] = {
                        "article_id": article_id,
                        "page": pdf_page,
                        "column": column,
                        "order": order,
                        "confidence": confidence,
                        "method": ("article_columns_v1"),
                    }
                    previous = region
                    previous_column = column
                    
            results[(document_id, article_id)] = ArticleReadingOrder(
                document_id=document_id,
                article_id=article_id,
                segments=segments,
                warnings=warnings,
            )

        return results