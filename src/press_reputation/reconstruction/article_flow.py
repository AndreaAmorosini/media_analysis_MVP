import re
from math import isfinite

from pydantic import BaseModel, Field

from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.reconstruction.flow_models import FlowLink


class ArticleFlowConfig(BaseModel):
    minimum_boundary_words: int = Field(default=5, ge=1)
    maximum_column_difference: float = Field(default=0.04, gt=0, lt=1)

    # Segnali di frase non conclusa, non prove sufficienti da soli.
    unfinished_endings: tuple[str, ...] = (
        "in", "di", "a", "da", "con", "per",
        "tra", "fra", "che", "del", "della", "delle", "dei",
    )


def valid_box(region: Region) -> bool:
    box = region.bbox
    return (
        box is not None
        and len(box) == 4
        and all(isfinite(value) for value in box)
        and box[2] > box[0]
        and box[3] > box[1]
    )


class ArticleFlowResolver:
    BLOCKED_SCOPES = {
        "related", "advertisement", "boilerplate", "non_main"
    }

    REQUIRED_EVIDENCE = {
        "adjacent_pdf_pages",
        "same_publication_date",
        "compatible_body_column",
        "no_new_article_title",
    }

    def __init__(self, config: ArticleFlowConfig | None = None) -> None:
        self.config = config or ArticleFlowConfig()

    def resolve(self, pages: list[PageRecord]) -> list[FlowLink]:
        page_index = {
            (page.document_id, page.pdf_page): page
            for page in pages
        }
        links: list[FlowLink] = []

        for page in sorted(
            pages, key=lambda item: (item.document_id, item.pdf_page)
        ):
            groups: dict[tuple[str, int], list[Region]] = {}

            for region in page.regions:
                metadata = region.metadata
                candidate_id = metadata.get("article_candidate_id")
                previous_page = metadata.get("continuation_from_pdf_page")

                if not candidate_id or not isinstance(previous_page, int):
                    continue

                groups.setdefault(
                    (candidate_id, previous_page), []
                ).append(region)

            for (candidate_id, previous_number), regions in groups.items():
                previous = page_index.get(
                    (page.document_id, previous_number)
                )

                evidence = sorted({
                    value
                    for region in regions
                    for value in region.metadata.get(
                        "continuation_evidence", []
                    )
                })

                link = FlowLink(
                    document_id=page.document_id,
                    article_candidate_id=candidate_id,
                    from_pdf_page=previous_number,
                    to_pdf_page=page.pdf_page,
                    status="candidate",
                    evidence=evidence,
                )

                if previous is None or previous_number + 1 != page.pdf_page:
                    link.status = "rejected"
                    link.contradictions.append("missing_or_non_adjacent_page")
                    links.append(link)
                    continue

                if any(
                    region.type == RegionType.ARTICLE_TITLE
                    and self.eligible(region)
                    for region in page.regions
                ):
                    link.status = "rejected"
                    link.contradictions.append("new_article_title")
                    links.append(link)
                    continue

                if (
                    previous.source.publication_date is not None
                    and page.source.publication_date is not None
                    and previous.source.publication_date
                    != page.source.publication_date
                ):
                    link.status = "rejected"
                    link.contradictions.append("different_publication_dates")
                    links.append(link)
                    continue

                source_evidence = {
                    "same_source_header", "same_source_name"
                } & set(evidence)

                if (
                    not self.REQUIRED_EVIDENCE.issubset(evidence)
                    or not source_evidence
                ):
                    links.append(link)
                    continue

                left_regions = self.flow_regions(previous, candidate_id)
                right_regions = self.flow_regions(page, candidate_id)

                if not left_regions or not right_regions:
                    links.append(link)
                    continue

                # Prima versione: flusso web a colonna singola.
                left = max(left_regions, key=lambda region: region.bbox[3])
                right = min(right_regions, key=lambda region: region.bbox[1])

                link.from_region_id = left.metadata.get("region_id")
                link.to_region_id = right.metadata.get("region_id")

                if self.compatible_sentence_boundary(
                    left, right, previous, page
                ):
                    link.status = "accepted"
                    link.evidence.extend([
                        "unfinished_sentence_at_page_end",
                        "compatible_lowercase_start",
                        "aligned_boundary_regions",
                    ])

                    # Recupero limitato ai due frammenti del collegamento.
                    self.recover_fragment(left)
                    self.recover_fragment(right)

                links.append(link)

        return links

    def eligible(self, region: Region) -> bool:
        return (
            valid_box(region)
            and bool(region.text)
            and not region.exclude_from_article_text
            and region.metadata.get("content_scope") not in self.BLOCKED_SCOPES
            and not region.metadata.get("inside_article_position_thumbnail")
        )

    def flow_regions(
        self, page: PageRecord, candidate_id: str
    ) -> list[Region]:
        return [
            region
            for region in page.regions
            if self.eligible(region)
            and region.metadata.get("article_candidate_id") == candidate_id
            and (
                region.type == RegionType.ARTICLE_BODY
                or (
                    region.type == RegionType.UNKNOWN
                    and region.metadata.get("continuation_local_role")
                    == "body_fragment_candidate"
                )
            )
        ]

    def compatible_sentence_boundary(
        self,
        left: Region,
        right: Region,
        previous: PageRecord,
        current: PageRecord,
    ) -> bool:
        if not previous.page_width or not current.page_width:
            return False

        left_text = (left.text or "").strip()
        right_text = (right.text or "").strip()

        left_words = re.findall(r"\w+", left_text.casefold())
        right_words = re.findall(r"\w+", right_text.casefold())

        if min(len(left_words), len(right_words)) < (
            self.config.minimum_boundary_words
        ):
            return False

        if left_text.endswith((".", "!", "?", "…", ":", ";")):
            return False

        if left_words[-1] not in self.config.unfinished_endings:
            return False

        first_letter = next(
            (char for char in right_text if char.isalpha()),
            None,
        )
        if first_letter is None or not first_letter.islower():
            return False

        if any(
            token in f"{left_text} {right_text}".casefold()
            for token in ("http://", "https://")
        ):
            return False

        for coordinate in (0, 2):
            difference = abs(
                left.bbox[coordinate] / previous.page_width
                - right.bbox[coordinate] / current.page_width
            )
            if difference > self.config.maximum_column_difference:
                return False

        return True

    @staticmethod
    def recover_fragment(region: Region) -> None:
        if (
            region.type == RegionType.UNKNOWN
            and region.metadata.get("continuation_local_role")
            == "body_fragment_candidate"
        ):
            region.metadata["type_before_flow"] = region.type.value
            region.type = RegionType.ARTICLE_BODY
            region.metadata["body_detection_method"] = (
                "cross_page_sentence_continuation"
            )

        region.metadata["flow_boundary_status"] = "accepted"