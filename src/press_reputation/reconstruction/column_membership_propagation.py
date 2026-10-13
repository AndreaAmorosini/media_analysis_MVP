
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from press_reputation.models.page import (
    PageRecord,
    Region,
    RegionType,
)


def horizontal_overlap(a, b) -> float:
    intersection = max(
        0.0,
        min(a[2], b[2]) - max(a[0], b[0]),
    )
    width = min(a[2] - a[0], b[2] - b[0])
    return intersection / width if width > 0 else 0.0


@dataclass
class EditorialColumn:
    regions: list[Region] = field(default_factory=list)

    @property
    def left(self) -> float:
        return median(r.bbox[0] for r in self.regions)

    @property
    def right(self) -> float:
        return median(r.bbox[2] for r in self.regions)

    @property
    def top(self) -> float:
        return min(r.bbox[1] for r in self.regions)

    @property
    def bottom(self) -> float:
        return max(r.bbox[3] for r in self.regions)

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def owners(self) -> set[str]:
        return {
            r.article_id
            for r in self.regions
            if r.article_id
        }


class ColumnMembershipPropagationResolver:
    """
    Propaga article_id da una colonna editoriale
    a quella adiacente.

    Non dipende dal tipo di pagina o dalla testata.

    Richiede:
    - una colonna già associata a un articolo;
    - un titolo principale affidabile;
    - colonne geometricamente compatibili;
    - assenza di titoli concorrenti.

    Non modifica regioni già assegnate.
    """

    MIN_SCORE = 0.78
    MIN_MARGIN = 0.12
    MAX_GAP_RATIO = 0.45
    MIN_VERTICAL_OVERLAP = 0.60
    MIN_WIDTH_RATIO = 0.72

    HARD_SCOPES = {
        "non_main",
        "related",
        "advertisement",
        "boilerplate",
        "navigation",
    }

    @classmethod
    def eligible(cls, region: Region) -> bool:
        b = region.bbox

        return (
            region.type == RegionType.ARTICLE_BODY
            and bool((region.text or "").strip())
            and b is not None
            and len(b) == 4
            and b[2] > b[0]
            and b[3] > b[1]
            and not region.exclude_from_article_text
            and region.metadata.get("content_scope")
            not in cls.HARD_SCOPES
            and not region.metadata.get(
                "inside_article_position_thumbnail"
            )
        )

    @staticmethod
    def title_barrier(
        page: PageRecord,
        previous_bottom: float,
        next_top: float,
        x0: float,
        x1: float,
    ) -> bool:

        if next_top <= previous_bottom:
            return False

        for region in page.regions:
            if (
                region.type != RegionType.ARTICLE_TITLE
                or not region.bbox
                or region.exclude_from_article_text
            ):
                continue

            if (
                region.bbox[1] >= previous_bottom - 3.0
                and region.bbox[3] <= next_top + 3.0
                and horizontal_overlap(
                    [x0, 0.0, x1, 1.0],
                    region.bbox,
                ) >= 0.30
            ):
                return True

        return False

    def columns(
        self,
        page: PageRecord,
    ) -> list[EditorialColumn]:

        bodies = sorted(
            (
                r for r in page.regions
                if self.eligible(r)
            ),
            key=lambda r: (
                r.bbox[0],
                r.bbox[1],
            ),
        )

        columns: list[EditorialColumn] = []

        for region in bodies:
            suitable = []

            for column in columns:

                if (
                    column.owners
                    and region.article_id
                    and region.article_id not in column.owners
                ):
                    continue

                if abs(region.bbox[0] - column.left) > 18:
                    continue

                if horizontal_overlap(
                    region.bbox,
                    [
                        column.left,
                        0.0,
                        column.right,
                        1.0,
                    ],
                ) < 0.72:
                    continue

                if self.title_barrier(
                    page,
                    column.bottom,
                    region.bbox[1],
                    column.left,
                    column.right,
                ):
                    continue

                suitable.append(column)

            if suitable:
                selected = min(
                    suitable,
                    key=lambda c: abs(
                        c.left - region.bbox[0]
                    ),
                )
                selected.regions.append(region)
            else:
                columns.append(
                    EditorialColumn([region])
                )

        return sorted(
            columns,
            key=lambda c: c.left,
        )

    def score(
        self,
        page: PageRecord,
        source: EditorialColumn,
        target: EditorialColumn,
        owner: str,
        title: Region,
    ) -> float | None:

        if target.owners or source.owners != {owner}:
            return None

        # La propagazione avviene verso la
        # colonna successiva da sinistra a destra.
        gap = target.left - source.right

        max_gap = max(
            24.0,
            self.MAX_GAP_RATIO * median([
                source.width,
                target.width,
            ]),
        )

        if gap < -8.0 or gap > max_gap:
            return None

        width_ratio = (
            min(source.width, target.width)
            / max(source.width, target.width)
        )

        if width_ratio < self.MIN_WIDTH_RATIO:
            return None

        # Le colonne devono condividere una parte
        # significativa della fascia verticale.
        vertical_overlap = max(
            0.0,
            min(source.bottom, target.bottom)
            - max(source.top, target.top),
        ) / min(source.height, target.height)

        if vertical_overlap < self.MIN_VERTICAL_OVERLAP:
            return None

        # Non attraversare verso l'alto il titolo
        # che identifica l'articolo.
        if target.top < title.bbox[3] - 8.0:
            return None

        for region in target.regions:
            candidate = region.metadata.get(
                "article_candidate_id"
            )
            if candidate and candidate != owner:
                return None

        # Blocca titoli concorrenti collocati sopra
        # la colonna candidata.
        for region in page.regions:
            if (
                region.type != RegionType.ARTICLE_TITLE
                or not region.bbox
                or region.exclude_from_article_text
                or region.article_id == owner
            ):
                continue

            if (
                region.bbox[3] <= target.top + 12.0
                and region.bbox[1]
                >= title.bbox[1] - 8.0
                and horizontal_overlap(
                    region.bbox,
                    [
                        target.left,
                        0.0,
                        target.right,
                        1.0,
                    ],
                ) >= 0.30
            ):
                return None

        top_alignment = max(
            0.0,
            1.0 - abs(source.top - target.top)
            / max(
                90.0,
                0.15 * (page.page_height or 842.0),
            ),
        )

        score = (
            0.32 * (
                1.0 - max(0.0, gap) / max_gap
            )
            + 0.25 * width_ratio
            + 0.30 * min(1.0, vertical_overlap)
            + 0.13 * top_alignment
        )

        return round(score, 4)

    def enrich_page(self, page: PageRecord) -> None:

        columns = self.columns(page)

        if len(columns) < 2:
            return

        titles_by_owner = {
            r.article_id: r
            for r in page.regions
            if (
                r.type == RegionType.ARTICLE_TITLE
                and r.article_id
                and r.bbox
                and not r.exclude_from_article_text
                and r.metadata.get("title_role")
                not in {
                    "teaser",
                    "ambiguous",
                    "not_main",
                }
            )
        }

        if not titles_by_owner:
            return

        for index, target in enumerate(columns):

            if target.owners:
                continue

            # Un owner può avere più colonne già
            # riconosciute: usa l'evidenza migliore.
            candidates = {}

            for source in columns[:index]:

                for owner in source.owners:

                    title = titles_by_owner.get(owner)

                    if title is None:
                        continue

                    value = self.score(
                        page,
                        source,
                        target,
                        owner,
                        title,
                    )

                    if value is None:
                        continue

                    if (
                        owner not in candidates
                        or value > candidates[owner][0]
                    ):
                        candidates[owner] = (
                            value,
                            source,
                        )

            ranked = sorted(
                (
                    (score, owner, source)
                    for owner, (
                        score, source
                    ) in candidates.items()
                ),
                key=lambda item: item[0],
                reverse=True,
            )

            if not ranked:
                reason = "no_compatible_left_column"

            elif ranked[0][0] < self.MIN_SCORE:
                reason = "weak_column_evidence"

            elif (
                len(ranked) > 1
                and ranked[0][0] - ranked[1][0]
                < self.MIN_MARGIN
            ):
                reason = "ambiguous_owner"

            else:
                reason = "accepted"

            accepted = reason == "accepted"

            for region in target.regions:
                region.metadata[
                    "column_membership_propagation"
                ] = {
                    "status": (
                        "assigned"
                        if accepted else "unresolved"
                    ),
                    "reason": reason,
                    "candidates": [
                        {
                            "article_id": owner,
                            "score": score,
                        }
                        for score, owner, _
                        in ranked[:3]
                    ],
                }

            if not accepted:
                continue

            best_score, owner, source = ranked[0]
            title = titles_by_owner[owner]

            for region in target.regions:

                if region.article_id:
                    continue

                previous = region.metadata.get(
                    "article_clustering"
                )

                if previous:
                    region.metadata.setdefault(
                        "article_clustering_attempts",
                        [],
                    ).append(previous)

                region.article_id = owner

                region.metadata["article_clustering"] = {
                    "status": "assigned",
                    "method": (
                        "column_membership_propagation_v1"
                    ),
                    "score": best_score,
                    "evidence": [
                        "adjacent_editorial_columns",
                        "compatible_column_width",
                        "vertical_band_overlap",
                        "no_competing_title",
                    ],
                    "alternatives": [],
                    "anchor_region_id": (
                        title.metadata.get("region_id")
                    ),
                }

                region.metadata[
                    "column_membership_propagation"
                ]["source_column_left"] = (
                    source.left
                )

                region.metadata["content_scope"] = "main"
                region.metadata[
                    "content_scope_reason"
                ] = "column_membership_propagation"

                region.metadata[
                    "include_in_main_body"
                ] = True

    def enrich_document(
        self,
        pages: list[PageRecord],
    ) -> None:
        for page in pages:
            self.enrich_page(page)
