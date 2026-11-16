

from press_reputation.core.models.page import PageRecord
from press_reputation.reconstruction.clustering.clustering_core import ClusteringCore
from press_reputation.reconstruction.assembly.flow_models import FlowLink


class FlowClustering:
    def __init__(self, core: ClusteringCore):
        self.core = core
        
    def link_accepted_flows(
        self, pages: list[PageRecord], links: list[FlowLink]
    ) -> list[PageRecord]:
        regions_by_id = {
            (page.document_id, self.core.region_id(region)): region
            for page in pages
            for region in page.regions
            if self.core.region_id(region)
        }

        for link in links:
            if link.status != "accepted":
                continue

            left = regions_by_id.get((link.document_id, link.from_region_id))
            right = regions_by_id.get((link.document_id, link.to_region_id))

            if left is None or right is None:
                link.status = "candidate"
                link.contradictions.append("missing_boundary_region_id")
                continue

            expected = link.article_candidate_id
            existing = {
                article_id
                for article_id in (left.article_id, right.article_id)
                if article_id is not None
            }

            if existing - {expected}:
                link.status = "rejected"
                link.contradictions.append("conflicting_article_ids")
                continue

            for region in (left, right):
                if region.article_id is None:
                    self.core.assign(
                        region,
                        expected,
                        method="accepted_flow_v1",
                        score=1.0,
                        evidence=["accepted_flow_link"],
                        anchor_region_id=link.from_region_id,
                    )

        return pages

    def propagate_web_article_metadata(
        self, pages: list[PageRecord], links: list[FlowLink]
    ) -> None:
        page_index = {(page.document_id, page.pdf_page): page for page in pages}

        for link in links:
            if link.method != "boundary_rules_v1" or link.status not in {
                "accepted",
                "candidate",
            }:
                continue

            anchor = page_index.get((link.document_id, link.from_pdf_page))
            current = page_index.get((link.document_id, link.to_pdf_page))
            if anchor is None or current is None:
                continue

            name = anchor.source.name
            date = anchor.source.publication_date
            url = anchor.source.url

            if name and current.source.name and current.source.name != name:
                continue
            if (
                date
                and current.source.publication_date
                and current.source.publication_date != date
            ):
                continue
            if url and current.source.url and current.source.url != url:
                continue

            for region in current.regions:
                if (
                    region.article_id == link.article_candidate_id
                    or region.metadata.get("article_candidate_id")
                    == link.article_candidate_id
                ) and self.core.eligible(region):
                    region.metadata["article_metadata_prior"] = {
                        "source": name,
                        "publication_date": date.isoformat() if date else None,
                        "url": url,
                        "from_pdf_page": anchor.pdf_page,
                        "link_status": link.status,
                        "method": "linked_web_page_prior",
                    }
                    
    def detach_rejected_web_pages(
        self, pages: list[PageRecord], links: list[FlowLink]
    ) -> None:
        for link in sorted(
            links,
            key=lambda item: (
                item.document_id,
                item.article_candidate_id,
                item.to_pdf_page,
            ),
        ):
            if link.method != "boundary_rules_v1" or link.status != "rejected":
                continue

            old_id = link.article_candidate_id
            new_id = f"{old_id}:from_page_{link.to_pdf_page:03d}"

            for page in pages:
                if (
                    page.document_id != link.document_id
                    or page.pdf_page < link.to_pdf_page
                ):
                    continue

                for region in page.regions:
                    if (
                        region.article_id != old_id
                        and region.metadata.get("article_candidate_id") != old_id
                    ):
                        continue

                    region.metadata["article_id_before_rejected_flow"] = old_id
                    region.metadata["detached_by_flow"] = {
                        "from_pdf_page": link.from_pdf_page,
                        "to_pdf_page": link.to_pdf_page,
                        "reason": list(link.contradictions),
                    }
                    if region.article_id == old_id:
                        region.article_id = new_id
                    if region.metadata.get("article_candidate_id") == old_id:
                        region.metadata["article_candidate_id"] = new_id

            # I link interni alla componente destra devono usare
            # lo stesso ID delle regioni ora separate.
            for following in links:
                if (
                    following is not link
                    and following.document_id == link.document_id
                    and following.article_candidate_id == old_id
                    and following.from_pdf_page >= link.to_pdf_page
                ):
                    following.article_candidate_id = new_id