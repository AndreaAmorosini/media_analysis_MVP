from __future__ import annotations

from collections import defaultdict
from press_reputation.models.page import PageRecord, RegionType
from press_reputation.reconstruction.flow_models import FlowLink


class ArticleIntegrityReconciler:
    """Checks consistency before reading order and assembly, never invents text.

    An accepted link without actual body on each side becomes a candidate;
    orphan editorial text remains available with actionable diagnostics.
    """

    @staticmethod
    def valid_body(r) -> bool:
        return (r.type == RegionType.ARTICLE_BODY and r.text and
                not r.exclude_from_article_text and r.metadata.get("content_scope")
                not in {"related", "advertisement", "boilerplate", "non_main", "navigation"})

    def reconcile(self, pages: list[PageRecord], links: list[FlowLink]) -> list[dict]:
        document = defaultdict(list)
        page_index = {}
        for page in pages:
            document[page.document_id].append(page)
            page_index[(page.document_id, page.pdf_page)] = page
        report = []
        for link in links:
            if link.status != "accepted":
                continue
            left = page_index.get((link.document_id, link.from_pdf_page))
            right = page_index.get((link.document_id, link.to_pdf_page))
            expected = link.article_candidate_id
            if not left or not right:
                reason = "link_missing_page"
            elif not any(self.valid_body(r) and r.article_id == expected for r in left.regions):
                reason = "missing_source_body_membership"
            elif not any(self.valid_body(r) and r.article_id == expected for r in right.regions):
                reason = "missing_target_body_membership"
            else:
                continue
            link.status = "candidate"
            link.evidence.append("integrity_downgrade")
            if reason not in link.contradictions:
                link.contradictions.append(reason)
            report.append({"document_id":link.document_id,
                            "pdf_pages":[link.from_pdf_page,link.to_pdf_page],
                            "article_id":expected,"issue":reason})

        for page in pages:
            unassigned = [r for r in page.regions if self.valid_body(r) and not r.article_id]
            if not unassigned:
                continue
            titles = [r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                        and r.article_id and not r.exclude_from_article_text]
            item = {"document_id":page.document_id, "pdf_page":page.pdf_page,
                    "issue":"unassigned_body", "orphan_chars":sum(len(r.text) for r in unassigned),
                    "orphan_region_ids":[r.metadata.get("region_id") for r in unassigned],
                    "title_article_ids":[r.article_id for r in titles]}
            report.append(item)
            for r in unassigned:
                r.metadata["article_integrity"] = {
                    "status":"unresolved", "reason":"body_without_article_identity",
                    "candidate_article_ids":item["title_article_ids"]}
        return report