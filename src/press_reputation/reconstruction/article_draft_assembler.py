from collections import defaultdict

from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.reconstruction.flow_models import ArticleReadingOrder, ReadingOrderSegment
from press_reputation.reconstruction.flow_models import (
    ArticleDraft,
    DraftSegment,
    FlowLink,
)


class ArticleDraftAssembler:
    BLOCKED_SCOPES = {
        "related", "advertisement", "boilerplate", "non_main", "navigation"
    }

    def assemble(self, pages: list[PageRecord], links: list[FlowLink], reading_orders: dict[tuple[str, str], ArticleReadingOrder] | None = None) -> list[ArticleDraft]:
        groups: dict[tuple[str, str], list[tuple[PageRecord, Region]]] = defaultdict(list)

        for page in pages:
            for region in page.regions:
                article_id = region.article_id or region.metadata.get("article_candidate_id")
                
                if not article_id:
                    continue
                
                legacy_id = region.metadata.get("article_candidate_id")
                
                if (legacy_id and region.article_id and legacy_id != region.article_id):
                    continue
                
                if (region.exclude_from_article_text or region.metadata.get("content_scope") in self.BLOCKED_SCOPES):
                    continue
                
                groups[(page.document_id, article_id)].append((page, region))

        drafts: list[ArticleDraft] = []


        for (document_id, article_id), members in groups.items():
            candidate_links = [link for link in links if link.document_id == document_id and
                                link.article_candidate_id == article_id]

            # Non assemblare una catena contenente una contraddizione.
            # La suddivisione automatica in sottocatene è un passo separato.
            if any(link.status == "rejected" for link in candidate_links):
                continue

            titles = [region.text for _, region in members if region.type == RegionType.ARTICLE_TITLE and region.text]
            
            order_record = (reading_orders.get((document_id, article_id)) if reading_orders is not None else None)
            
            warnings: list[str] = []
            if order_record is not None:
                ordered_segments = [segment for segment in order_record.segments
                                    if segment.type in {RegionType.ARTICLE_BODY, RegionType.ARTICLE_SECTION_HEADER}]
                ordered_segments.sort(key=lambda segment: segment.order)
                warnings.extend(order_record.warnings)
            else:
                warnings.append("Missing article reading order; using default ordering")
                body_members = [(page, region) for page, region in members if region.type == RegionType.ARTICLE_BODY and region.text]
                body_members.sort(key=self.order_key)
                
                ordered_segments = []
                for position, (page, region) in enumerate(body_members, start=1):
                    region_id = region.metadata.get("region_id")
                    if not region_id:
                        warnings.append(f"Skipped region without stable ID on page {page.pdf_page}")
                        continue
                        
                    ordered_segments.append(
                        ReadingOrderSegment(
                            article_id=article_id,
                            region_id=region_id,
                            type=RegionType.ARTICLE_BODY,
                            pdf_page=page.pdf_page,
                            column=None,
                            order=position,
                            bbox=region.bbox,
                            text=region.text,
                            confidence=0.25,
                            method="legacy_page_order",
                            provenance=region.provenance,
                        )
                    )

            if not any(segment.type == RegionType.ARTICLE_BODY for segment in ordered_segments):
                continue
            
            parts: list[str] = []
            segments: list[DraftSegment] = []
            cursor = 0
            
            for segment in ordered_segments:
                if parts:
                    cursor += 2
                    
                start = cursor
                cursor += len(segment.text)
                parts.append(segment.text)
                
                region = next((region for page, region in members 
                                if page.pdf_page == segment.pdf_page and region.metadata.get("region_id") == segment.region_id), None)
                
                selection_status = ("main" if (region is not None and region.metadata.get("content_scope") == "main") else "candidate")
                
                segments.append(
                    DraftSegment(
                        region_id = segment.region_id,
                        pdf_page = segment.pdf_page,
                        bbox = segment.bbox,
                        text=segment.text,
                        article_charspan=(start, cursor),
                        selection_status = selection_status,
                        provenance=segment.provenance,
                        type=segment.type,
                        column=segment.column,
                        order=segment.order,
                        confidence=segment.confidence,
                    )
                )
                
            if not segments:
                continue

            for link in candidate_links:
                if link.status == "candidate":
                    warnings.append(
                        f"Unresolved continuation: {link.from_pdf_page} -> {link.to_pdf_page}"
                    )

            warnings.append("Draft body includes candidate regions; not approved for automatic reputation scoring")

            if len(titles) != 1:
                warnings.append("Missing or ambiguous article title")

            drafts.append(
                ArticleDraft(
                    id=article_id,
                    document_id=document_id,
                    pdf_pages=sorted({page.pdf_page for page, _ in members} |
                                        {page_number for link in candidate_links if link.status == "accepted" 
                                            for page_number in (link.from_pdf_page, link.to_pdf_page)}),
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