from statistics import median

from press_reputation.features.geometry import horizontal_overlap
from press_reputation.core.models.page import PageRecord, Region, RegionType
from press_reputation.reconstruction.clustering.clustering_core import ClusteringCore


class ColumnClustering:
    def __init__(self, core: ClusteringCore) -> None:
        self.core = core
        
    def body_columns(self, page: PageRecord) -> list[list[Region]]:
        bodies = [
            region
            for region in page.regions
            if region.type == RegionType.ARTICLE_BODY
            and self.core.eligible(region)
            and region.has_valid_bbox
        ]
        if not bodies:
            return []

        widths = [region.bbox[2] - region.bbox[0] for region in bodies]
        typical_width = median(widths)

        # Sottotitoli o blocchi spanning classificati BODY non devono
        # fondere tutte le colonne in una sola.
        narrow = [
            region
            for region in bodies
            if len(bodies) < 3
            or region.bbox[2] - region.bbox[0]
            <= typical_width * self.core.config.wide_body_width_ratio
        ]

        columns: list[list[Region]] = []
        for region in sorted(narrow, key=lambda item: (item.bbox[0], item.bbox[1])):
            selected = None

            for column in columns:
                left = median(item.bbox[0] for item in column)
                right = median(item.bbox[2] for item in column)
                reference = Region(bbox=[left, 0.0, right, 1.0])

                if (
                    abs(region.bbox[0] - left) <= self.core.config.column_left_tolerance
                    and horizontal_overlap(region.bbox, reference.bbox)
                    >= self.core.config.min_within_column_overlap
                ):
                    selected = column
                    break

            if selected is None:
                columns.append([region])
            else:
                selected.append(region)

        return sorted(
            columns, key=lambda column: median(region.bbox[0] for region in column)
        )
        
    @staticmethod
    def column_box(column: list[Region]) -> tuple[float, float, float, float]:
        return (
            median(region.bbox[0] for region in column),
            min(region.bbox[1] for region in column),
            median(region.bbox[2] for region in column),
            max(region.bbox[3] for region in column),
        )
        
    @staticmethod
    def column_article_ids(column: list[Region]) -> set[str]:
        return {region.article_id for region in column if region.article_id}
    
    def article_header_box(
        self, page: PageRecord, title: Region
    ) -> tuple[float, float, float, float]:
        title_id = self.core.region_id(title)
        headers = [
            region
            for region in page.regions
            if (
                region is title
                or (
                    region.type == RegionType.ARTICLE_SUBTITLE
                    and region.metadata.get("subtitle_resolution", {}).get(
                        "title_region_id"
                    )
                    == title_id
                )
            )
            and region.has_valid_bbox
        ]

        return (
            min(region.bbox[0] for region in headers),
            min(region.bbox[1] for region in headers),
            max(region.bbox[2] for region in headers),
            max(region.bbox[3] for region in headers),
        )
        
    def competing_title_in_column(
        self,
        page: PageRecord,
        article_id: str,
        column: list[Region],
        anchor: Region | None = None,
    ) -> bool:
        box = self.column_box(column)

        for title in page.regions:
            if (
                title.type != RegionType.ARTICLE_TITLE
                or title.article_id == article_id
                or title.metadata.get("title_role") in {"not_main", "ambiguous"}
                or not title.has_valid_bbox
            ):
                continue

            title_box = tuple(title.bbox)
            if horizontal_overlap(title_box, box) < self.core.config.min_horizontal_overlap:
                continue

            # Un titolo di B che compare fra il titolo di A e il body
            # della colonna è una barriera, non un semplice vicino.
            lower_bound = anchor.bbox[1] if anchor and anchor.has_valid_bbox else 0.0
            if lower_bound <= title.bbox[1] <= box[3]:
                return True

        return False

    def column_styles_compatible(
        self, previous: list[Region], following: list[Region]
    ) -> tuple[bool, bool]:
        before = max(previous, key=lambda region: region.bbox[3])
        after = min(following, key=lambda region: region.bbox[1])

        before_font = before.style.get("dominant_font")
        after_font = after.style.get("dominant_font")
        if before_font and after_font and before_font != after_font:
            return False, False

        before_size = before.style.get("median_font_size")
        after_size = after.style.get("median_font_size")
        if before_size and after_size:
            difference = abs(before_size - after_size) / max(before_size, after_size)
            if difference > self.core.config.max_font_size_ratio_difference:
                return False, False
            return True, True

        # Stile OCR assente: non è evidenza negativa né positiva.
        return True, bool(before_font and after_font)
    
    def editorial_vertical_bounds(
        self,
        page: PageRecord,
        previous: list[Region],
        following: list[Region],
        title: Region | None,
    ) -> tuple[float, float]:
        top = (
            title.bbox[3]
            if title and title.has_valid_bbox
            else min(region.bbox[1] for region in previous + following)
        )

        corridor_left = min(region.bbox[0] for region in previous + following)
        corridor_right = max(region.bbox[2] for region in previous + following)
        relevant = [
            region
            for region in page.regions
            if region.type == RegionType.ARTICLE_BODY
            and self.core.eligible(region)
            and region.has_valid_bbox
            and region.bbox[1] >= top
            and region.bbox[0] < corridor_right
            and region.bbox[2] > corridor_left
        ]
        bottom = max((region.bbox[3] for region in relevant), default=top)
        return top, bottom

    def lexical_column_boundary(
        self, previous: list[Region], following: list[Region]
    ) -> tuple[float, str | None]:
        left = max(previous, key=lambda region: region.bbox[3])
        right = min(following, key=lambda region: region.bbox[1])
        end = (left.text or "").rstrip()[-self.core.config.lexical_boundary_chars :]
        start = (right.text or "").lstrip()[: self.core.config.lexical_boundary_chars]

        if not end or not start:
            return 0.0, None
        if end.endswith(("-", "–")):
            return 0.12, "hyphenated_column_boundary"
        if not end.endswith((".", "!", "?", "…")):
            return 0.08, "open_sentence_column_boundary"
        return 0.0, None

    def adjacent_column_score(
        self,
        page: PageRecord,
        previous: list[Region],
        following: list[Region],
        article_id: str,
        title: Region | None,
        has_index_match: bool,
    ) -> tuple[float, list[str]]:
        if not page.page_width or not page.page_height:
            return 0.0, ["missing_page_dimensions"]

        left = self.column_box(previous)
        right = self.column_box(following)
        horizontal_gap = (right[0] - left[2]) / page.page_width
        width_difference = (
            abs((left[2] - left[0]) - (right[2] - right[0])) / page.page_width
        )

        if not 0 <= horizontal_gap <= self.core.config.max_adjacent_column_gap_fraction:
            return 0.0, ["columns_not_adjacent"]
        if width_difference > self.core.config.max_column_width_difference_fraction:
            return 0.0, ["different_column_width"]
        if self.competing_title_in_column(page, article_id, following, title):
            return 0.0, ["competing_title_in_target_column"]
        if self.column_article_ids(following) - {article_id}:
            return 0.0, ["target_column_has_other_article"]

        compatible_style, style_evidence = self.column_styles_compatible(
            previous, following
        )
        if not compatible_style:
            return 0.0, ["incompatible_body_style"]

        if title is not None:
            if right[1] < title.bbox[3]:
                return 0.0, ["target_starts_inside_title"]

        same_source = self.core.same_source_continuation(previous, following)
        header = self.article_header_box(page, title) if title else None
        top, editorial_bottom = self.editorial_vertical_bounds(
            page, previous, following, title
        )

        area_height = max(editorial_bottom - top, 1.0)
        terminal_fraction = max(0.0, min((left[3] - top) / area_height, 1.0))
        near_editorial_end = (
            terminal_fraction >= self.core.config.min_editorial_terminal_fraction
        )

        target_near_header = False
        if header and right[1] >= header[3]:
            target_near_header = (
                right[1] - header[3]
            ) / page.page_height <= self.core.config.max_target_start_after_header_fraction

        lexical_score, lexical_reason = self.lexical_column_boundary(
            previous, following
        )

        # Senza sorgente Docling comune servono più indizi indipendenti.
        if not same_source and not (near_editorial_end and target_near_header):
            return 0.0, ["insufficient_column_transition_evidence"]

        score = 0.28  # adiacenza già verificata sopra
        evidence = [
            "adjacent_editorial_columns",
            "compatible_column_width",
            "no_competing_title",
        ]

        width_quality = max(
            0.0,
            1.0 - width_difference / self.core.config.max_column_width_difference_fraction,
        )
        score += 0.04 * width_quality
        evidence.append("column_width_similarity")

        if same_source:
            score += 0.32
            evidence.append("consecutive_spans_same_docling_item")
        if near_editorial_end:
            score += 0.12
            evidence.append("previous_column_near_editorial_end")
        if target_near_header:
            score += 0.15
            evidence.append("target_near_article_header")
        if lexical_reason:
            score += lexical_score
            evidence.append(lexical_reason)
        if style_evidence:
            score += 0.10
            evidence.append("compatible_body_typography")
        if has_index_match:
            score += 0.06
            evidence.append("matched_review_index_title")

        return min(score, 1.0), evidence

    def bootstrap_group_score(
        self,
        page: PageRecord,
        title: Region,
        columns: list[list[Region]],
        has_index_match: bool,
    ) -> tuple[float, list[str]]:
        if not columns or not page.page_height:
            return 0.0, ["missing_columns_or_page_height"]

        header = self.article_header_box(page, title)
        boxes = [self.column_box(column) for column in columns]

        if any(box[1] < header[3] for box in boxes):
            return 0.0, ["column_starts_inside_article_header"]

        overlaps = [horizontal_overlap(header, box) for box in boxes]
        if min(overlaps) < self.core.config.min_bootstrap_header_overlap:
            return 0.0, ["column_outside_article_header"]

        first_gap = (min(box[1] for box in boxes) - header[3]) / page.page_height
        if first_gap > self.core.config.max_bootstrap_first_body_gap_fraction:
            return 0.0, ["first_body_too_far_from_header"]

        for column in columns:
            if self.competing_title_in_column(page, title.article_id, column, title):
                return 0.0, ["competing_title_in_editorial_area"]
            if self.column_article_ids(column) - {title.article_id}:
                return 0.0, ["column_already_owned_by_other_article"]

        adjacency_scores = []
        for previous, following in zip(columns, columns[1:]):
            score, _ = self.adjacent_column_score(
                page, previous, following, title.article_id, title, has_index_match
            )
            if not score:
                return 0.0, ["incompatible_adjacent_columns"]
            adjacency_scores.append(score)

        # Le componenti restano distinguibili nei metadata:
        # non è una confidence OCR né un reading-order score.
        components = {
            "header_coverage": 0.38 * (sum(overlaps) / len(overlaps)),
            "near_article_header": 0.18
            * (1 - first_gap / self.core.config.max_bootstrap_first_body_gap_fraction),
            "adjacent_columns": 0.18 * (sum(adjacency_scores) / len(adjacency_scores))
            if adjacency_scores
            else 0.0,
            "matched_review_index": 0.14 if has_index_match else 0.0,
        }

        styles = [
            self.column_styles_compatible(previous, following)
            for previous, following in zip(columns, columns[1:])
        ]
        if styles and all(
            compatible and informative for compatible, informative in styles
        ):
            components["compatible_body_typography"] = 0.12

        return min(sum(components.values()), 1.0), list(components)

    def bootstrap_membership(
        self,
        page: PageRecord,
        anchors: dict[str, Region],
        matched_titles: set[tuple[int, str | None]],
    ) -> PageRecord:
        columns = self.body_columns(page)
        if not columns or not anchors:
            return page

        proposals: list[
            tuple[float, str, list[list[Region]], list[str], str | None]
        ] = []

        for article_id, title in anchors.items():
            header = self.article_header_box(page, title)
            candidate_columns = [
                column
                for column in columns
                if (
                    self.column_box(column)[1] >= header[3]
                    and horizontal_overlap(header, self.column_box(column))
                    >= self.core.config.min_bootstrap_header_overlap
                    and not self.column_article_ids(column) - {article_id}
                    and not self.competing_title_in_column(
                        page, article_id, column, title
                    )
                )
            ]

            if not candidate_columns:
                continue

            has_index_match = (page.pdf_page, self.core.region_id(title)) in matched_titles
            score, evidence = self.bootstrap_group_score(
                page, title, candidate_columns, has_index_match
            )
            if score:
                proposals.append(
                    (
                        score,
                        article_id,
                        candidate_columns,
                        evidence,
                        self.core.region_id(title),
                    )
                )

        proposals.sort(key=lambda item: (-item[0], item[1]))

        for score, article_id, candidate_columns, evidence, anchor_id in proposals:
            if score < self.core.config.min_bootstrap_group_score:
                continue

            candidate_region_ids = {
                id(region) for column in candidate_columns for region in column
            }
            competing_scores = [
                other_score
                for other_score, other_id, other_columns, _, _ in proposals
                if other_id != article_id
                and any(
                    id(region) in candidate_region_ids
                    for column in other_columns
                    for region in column
                )
            ]
            if (
                competing_scores
                and score - max(competing_scores)
                < self.core.config.min_bootstrap_group_margin
            ):
                for column in candidate_columns:
                    for region in column:
                        if region.article_id is None:
                            region.metadata.setdefault("membership_bootstrap", {})[
                                "status"
                            ] = "ambiguous_anchors"
                continue

            # Assegnare il gruppo in modo coerente, non una colonna
            # casuale che diventa poi un seed auto-rinforzante.
            for column in candidate_columns:
                for region in column:
                    if region.article_id is not None:
                        continue

                    previous = region.metadata.get("article_clustering")
                    if previous and previous.get("status") == "ambiguous":
                        region.metadata.setdefault(
                            "article_clustering_attempts", []
                        ).append(previous)

                    if self.core.assign(
                        region,
                        article_id,
                        method="header_column_bootstrap_v1",
                        score=score,
                        evidence=evidence,
                        anchor_region_id=anchor_id,
                    ):
                        region.metadata["membership_bootstrap"] = {
                            "status": "assigned",
                            "score": round(score, 4),
                            "evidence": evidence,
                            "column_bbox": self.column_box(column),
                            "review_index_matched": (page.pdf_page, anchor_id)
                            in matched_titles,
                        }

        return page

    def assign_multicolumn(
        self, page: PageRecord, matched_titles: set[tuple[int, str | None]]
    ) -> PageRecord:
        columns = self.body_columns(page)
        if len(columns) < 2:
            return page

        titles = {
            region.article_id: region
            for region in page.regions
            if (
                region.type == RegionType.ARTICLE_TITLE
                and region.article_id
                and region.has_valid_bbox
                and region.metadata.get("title_role") not in {"not_main", "ambiguous"}
            )
        }

        # Iterativo: dopo 1 → 2, la colonna 2 può sostenere 2 → 3.
        for _ in range(len(columns)):
            changed = False

            for index in range(1, len(columns)):
                target = columns[index]
                if self.column_article_ids(target):
                    continue

                proposals: list[
                    tuple[
                        float,
                        str,
                        list[str],
                        str | None,
                        tuple[float, float, float, float],
                    ]
                ] = []

                for source_index in (index - 1, index + 1):
                    if not 0 <= source_index < len(columns):
                        continue

                    source = columns[source_index]
                    source_ids = self.column_article_ids(source)
                    if len(source_ids) != 1:
                        continue

                    article_id = next(iter(source_ids))
                    title = titles.get(article_id)

                    # Si usa una transizione sinistra → destra.
                    if source_index > index:
                        continue

                    score, evidence = self.adjacent_column_score(
                        page,
                        source,
                        target,
                        article_id,
                        title,
                        bool(
                            title
                            and (page.pdf_page, self.core.region_id(title)) in matched_titles
                        ),
                    )
                    if score:
                        proposals.append(
                            (
                                score,
                                article_id,
                                evidence,
                                self.core.region_id(title) if title else None,
                                self.column_box(source),
                            )
                        )

                proposals.sort(key=lambda item: (-item[0], item[1]))
                if not proposals:
                    continue

                best_score, article_id, evidence, anchor_id, source_box = proposals[0]
                second_score = proposals[1][0] if len(proposals) > 1 else 0.0

                if (
                    best_score < self.core.config.min_column_continuation_score
                    or best_score - second_score
                    < self.core.config.min_column_continuation_margin
                ):
                    for region in target:
                        if region.article_id is None:
                            region.metadata["multicolumn_candidates"] = [
                                {
                                    "article_id": candidate_id,
                                    "score": round(candidate_score, 4),
                                }
                                for candidate_score, candidate_id, _, _, _ in proposals[
                                    :3
                                ]
                            ]
                    continue

                for region in target:
                    if region.article_id is not None:
                        continue

                    previous_attempt = region.metadata.get("article_clustering")
                    if (
                        previous_attempt
                        and previous_attempt.get("status") == "ambiguous"
                    ):
                        region.metadata.setdefault(
                            "article_clustering_attempts", []
                        ).append(previous_attempt)

                    if self.core.assign(
                        region,
                        article_id,
                        method="adjacent_body_column_v1",
                        score=best_score,
                        evidence=evidence,
                        anchor_region_id=anchor_id,
                    ):
                        region.metadata["clustering_column"] = {
                            "page": page.pdf_page,
                            "left": round(self.column_box(target)[0], 2),
                            "right": round(self.column_box(target)[2], 2),
                            "source_column_bbox": source_box,
                            "target_column_bbox": self.column_box(target),
                            "transition_evidence": evidence,
                            "membership_method": "adjacent_body_column_v1",
                        }
                        changed = True

            if not changed:
                break

        return page





