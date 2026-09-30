from collections import defaultdict

from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.reconstruction.flow_models import (
    ArticleDraft,
    DraftSegment,
    FlowLink,
)


class ArticleDraftAssembler:
    BLOCKED_SCOPES = {
        "related", "advertisement", "boilerplate", "non_main"
    }

    def assemble(
        self,
        pages: list[PageRecord],
        links: list[FlowLink],
    ) -> list[ArticleDraft]:
        groups: dict[
            tuple[str, str], list[tuple[PageRecord, Region]]
        ] = defaultdict(list)

        for page in pages:
            for region in page.regions:
                candidate_id = region.metadata.get("article_candidate_id")

                if not candidate_id:
                    continue

                if (
                    region.exclude_from_article_text
                    or region.metadata.get("content_scope")
                    in self.BLOCKED_SCOPES
                ):
                    continue

                groups[(page.document_id, candidate_id)].append(
                    (page, region)
                )

        drafts: list[ArticleDraft] = []

        for (document_id, candidate_id), members in groups.items():
            candidate_links = [
                link
                for link in links
                if link.document_id == document_id
                and link.article_candidate_id == candidate_id
            ]

            # Non assemblare una catena contenente una contraddizione.
            # La suddivisione automatica in sottocatene è un passo separato.
            if any(link.status == "rejected" for link in candidate_links):
                continue

            titles = [
                region.text
                for _, region in members
                if region.type == RegionType.ARTICLE_TITLE
                and region.text
            ]

            body_members = [
                (page, region)
                for page, region in members
                if region.type == RegionType.ARTICLE_BODY
                and region.text
            ]

            body_members.sort(key=self.order_key)

            if not body_members:
                continue

            parts: list[str] = []
            segments: list[DraftSegment] = []
            warnings: list[str] = []
            cursor = 0

            for page, region in body_members:
                region_id = region.metadata.get("region_id")

                if not region_id:
                    warnings.append(
                        f"Skipped region without stable ID on page {page.pdf_page}"
                    )
                    continue

                if parts:
                    cursor += 2  # Separatore "\n\n".

                text = region.text
                start = cursor
                cursor += len(text)
                parts.append(text)

                segments.append(
                    DraftSegment(
                        region_id=region_id,
                        pdf_page=page.pdf_page,
                        bbox=region.bbox,
                        text=text,
                        article_charspan=(start, cursor),
                        selection_status=(
                            "main"
                            if region.metadata.get("content_scope") == "main"
                            else "candidate"
                        ),
                        provenance=region.provenance,
                    )
                )

            if not segments:
                continue

            for link in candidate_links:
                if link.status == "candidate":
                    warnings.append(
                        "Unresolved continuation: "
                        f"{link.from_pdf_page} -> {link.to_pdf_page}"
                    )

            warnings.append(
                "Draft body includes candidate regions; "
                "not approved for automatic reputation scoring"
            )

            if len(titles) != 1:
                warnings.append("Missing or ambiguous article title")

            drafts.append(
                ArticleDraft(
                    id=candidate_id,
                    document_id=document_id,
                    pdf_pages=sorted({
                        page.pdf_page for page, _ in members
                    }),
                    title=titles[0] if len(titles) == 1 else None,
                    body="\n\n".join(parts),
                    segments=segments,
                    links=candidate_links,
                    warnings=warnings,
                )
            )

        return drafts

    @staticmethod
    def order_key(item: tuple[PageRecord, Region]) -> tuple:
        page, region = item
        order = region.metadata.get("body_reading_order")

        if isinstance(order, int):
            return (page.pdf_page, 0, order, 0.0)

        box = region.bbox or [0.0, 0.0, 0.0, 0.0]
        return (page.pdf_page, 1, box[1], box[0])