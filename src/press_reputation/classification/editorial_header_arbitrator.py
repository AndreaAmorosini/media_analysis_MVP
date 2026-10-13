
from __future__ import annotations

from press_reputation.models.page import (
    PageRecord,
    Region,
    RegionType,
)


class EditorialHeaderArbitrator:
    """
    Risolve coppie di headline appartenenti allo
    stesso blocco editoriale PRIMA del clustering.

    Non genera article_id.
    Non elimina il testo originale.
    Lascia irrisolti i casi ambigui.
    """

    BLOCKED_SCOPES = {
        "non_main",
        "related",
        "advertisement",
        "boilerplate",
        "navigation",
    }

    @staticmethod
    def horizontal_overlap(
        a: Region,
        b: Region,
    ) -> float:
        intersection = max(
            0.0,
            min(a.bbox[2], b.bbox[2])
            - max(a.bbox[0], b.bbox[0]),
        )

        width = min(
            a.bbox[2] - a.bbox[0],
            b.bbox[2] - b.bbox[0],
        )

        return intersection / width if width > 0 else 0.0

    @staticmethod
    def region_id(region: Region) -> str | None:
        return region.metadata.get("region_id")

    def resolve_header_stacks(
        self,
        pages: list[PageRecord],
    ) -> None:
        for page in pages:
            titles = sorted(
                [
                    r for r in page.regions
                    if (
                        r.type == RegionType.ARTICLE_TITLE
                        and r.bbox
                        and not r.exclude_from_article_text
                    )
                ],
                key=lambda r: r.bbox[1],
            )

            visited = set()

            for upper, lower in zip(titles, titles[1:]):
                if (
                    id(upper) in visited
                    or id(lower) in visited
                ):
                    continue

                if upper.bbox[3] > lower.bbox[1] + 3:
                    continue

                gap = max(
                    0.0,
                    lower.bbox[1] - upper.bbox[3],
                )

                if gap > max(
                    28.0,
                    0.04 * (page.page_height or 842.0),
                ):
                    continue

                if self.horizontal_overlap(
                    upper, lower
                ) < 0.60:
                    continue

                # Non unire due blocchi se un body
                # indipendente si trova tra i titoli.
                body_between = any(
                    r.type == RegionType.ARTICLE_BODY
                    and r.bbox
                    and r.bbox[1] >= upper.bbox[3]
                    and r.bbox[3] <= lower.bbox[1]
                    for r in page.regions
                )

                if body_between:
                    continue

                upper_candidate = (
                    upper.metadata.get("title_candidate") or {}
                )
                lower_candidate = (
                    lower.metadata.get("title_candidate") or {}
                )

                upper_index = upper_candidate.get(
                    "index_entry_id"
                )
                lower_index = lower_candidate.get(
                    "index_entry_id"
                )

                upper_height = (
                    upper.bbox[3] - upper.bbox[1]
                )
                lower_height = (
                    lower.bbox[3] - lower.bbox[1]
                )

                # Caso: titolo principale supportato
                # dal ReviewIndex, seguito dal deck.
                if upper_index and not lower_index:
                    main = upper
                    auxiliary = lower
                    role = "subtitle"
                    reason = "index_supported_upper_headline"

                # Caso: occhiello sopra un titolo che
                # corrisponde alla voce dell'indice.
                elif lower_index and not upper_index:
                    main = lower
                    auxiliary = upper
                    role = "kicker"
                    reason = "index_supported_lower_headline"

                # Titolo inferiore visivamente
                # predominante rispetto all'occhiello.
                elif (
                    lower_height >= 1.65 * upper_height
                    and upper_height <= 0.04
                    * (page.page_height or 842.0)
                ):
                    main = lower
                    auxiliary = upper
                    role = "kicker"
                    reason = "short_upper_kicker"

                # Titolo superiore predominante
                # rispetto al sottotitolo inferiore.
                elif (
                    upper_height >= 1.65 * lower_height
                    and lower_height <= 0.10
                    * (page.page_height or 842.0)
                ):
                    main = upper
                    auxiliary = lower
                    role = "subtitle"
                    reason = "upper_prominent_lower_deck"

                else:
                    for region in (upper, lower):
                        region.metadata.setdefault(
                            "editorial_header_arbitration",
                            {
                                "status": "ambiguous",
                                "reason": "no_unique_headline_role",
                            },
                        )
                    continue

                # Questo resolver deve essere eseguito
                # prima della creazione degli article_id.
                if main.article_id or auxiliary.article_id:
                    continue

                main_id = self.region_id(main)

                auxiliary.metadata[
                    "editorial_header_arbitration"
                ] = {
                    "status": "resolved",
                    "role": role,
                    "main_title_region_id": main_id,
                    "reason": reason,
                    "original_type": auxiliary.type.value,
                }

                auxiliary.metadata["title_role"] = role

                if role == "subtitle":
                    auxiliary.type = (
                        RegionType.ARTICLE_SUBTITLE
                    )
                    auxiliary.metadata[
                        "subtitle_resolution"
                    ] = {
                        "status": "accepted",
                        "method": "editorial_header_arbitration_v1",
                        "title_region_id": main_id,
                        "previous_type": "article_title",
                    }

                else:
                    auxiliary.type = (
                        RegionType.ARTICLE_KICKER
                    )
                    auxiliary.metadata[
                        "editorial_subrole"
                    ] = "kicker"

                    auxiliary.metadata[
                        "editorial_header_resolution"
                    ] = {
                        "method": "editorial_header_arbitration_v1",
                        "title_region_id": main_id,
                        "previous_type": "article_title",
                    }

                main.metadata[
                    "editorial_header_arbitration"
                ] = {
                    "status": "resolved",
                    "role": "main_title",
                    "related_region_id": self.region_id(
                        auxiliary
                    ),
                    "reason": reason,
                }

                visited.update(
                    [id(main), id(auxiliary)]
                )

    def suppress_non_main_titles(
        self,
        pages: list[PageRecord],
    ) -> None:
        """
        Evita che una regione già identificata come
        navigation/related/non-main crei un article_id.
        """

        for page in pages:
            for region in page.regions:
                if (
                    region.type != RegionType.ARTICLE_TITLE
                    or region.article_id
                ):
                    continue

                scope = region.metadata.get(
                    "content_scope"
                )

                if (
                    not region.exclude_from_article_text
                    and scope not in self.BLOCKED_SCOPES
                ):
                    continue

                region.metadata[
                    "editorial_header_arbitration"
                ] = {
                    "status": "rejected",
                    "reason": "excluded_or_non_main_content",
                    "previous_type": region.type.value,
                    "previous_scope": scope,
                }

                region.metadata[
                    "title_role"
                ] = "rejected_non_main"

                region.type = RegionType.UNKNOWN
