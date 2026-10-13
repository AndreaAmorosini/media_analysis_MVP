from __future__ import annotations

from collections import defaultdict
import re
from urllib.parse import urlsplit, urlunsplit

from press_reputation.models.page import PageRecord, PageType, RegionType
from press_reputation.reconstruction.flow_models import FlowLink


class GeneralContinuationResolver:
    """Second-chance cross-page links using several independent signals.

    Does not override a conflicting assigned owner or a strong new headline.
    Never infers continuity from page adjacency alone.
    """
    BLOCKED = {"related", "advertisement", "boilerplate", "non_main", "navigation"}
    MIN_SCORE = 0.72
    PRINT_ID = re.compile(r"\bDW\s*(\d{3,})\b", re.I)
    VISITORS = re.compile(r"visitatori\s+unici\s+giornalieri\s*:\s*([\d.,]+)", re.I)

    @staticmethod
    def marker(page, pattern):
        for region in page.regions:
            match = pattern.search(region.text or "")
            if match:
                return re.sub(r"\D", "", match.group(1))
        return None

    @staticmethod
    def canonical(url: str | None) -> str | None:
        if not url:
            return None
        p = urlsplit(url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."),
                           p.path.rstrip("/"), "", "")) if p.netloc else None

    @classmethod
    def bodies(cls, page: PageRecord):
        return [r for r in page.regions if r.type == RegionType.ARTICLE_BODY
                and r.text and r.bbox and not r.exclude_from_article_text
                and r.metadata.get("content_scope") not in cls.BLOCKED]

    @staticmethod
    def main_titles(page: PageRecord):
        return [r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                and not r.exclude_from_article_text
                and r.metadata.get("title_role") not in {
                    "teaser", "not_main", "rejected_non_main"}]

    def resolve(self, pages: list[PageRecord], links: list[FlowLink]) -> list[dict]:
        by_doc = defaultdict(list)
        for p in pages:
            by_doc[p.document_id].append(p)
        report = []
        for sequence in by_doc.values():
            sequence.sort(key=lambda x: x.pdf_page)
            for a, b in zip(sequence, sequence[1:]):
                if b.pdf_page != a.pdf_page + 1:
                    continue
                # Generic page types, but never propagate across an index.
                if PageType.INDEX in (a.page_type, b.page_type):
                    continue
                left, right = self.bodies(a), self.bodies(b)
                owners = {r.article_id for r in left if r.article_id}
                anchor_only = False
                if not owners and a.page_type == PageType.WEB and a.source.url:
                    # Browser print: headline is on the preceding page,
                    # narrative starts on the next page.
                    anchor_ids = {r.article_id for r in self.main_titles(a) if r.article_id}
                    if len(anchor_ids) == 1 and not left:
                        owners = anchor_ids
                        anchor_only = True
                if len(owners) != 1 or not right:
                    continue
                owner = next(iter(owners))
                if any(r.article_id not in (None, owner) for r in right):
                    continue
                if self.main_titles(b):
                    # No cross-page take-over of a confirmed local article.
                    continue
                url_a, url_b = self.canonical(a.source.url), self.canonical(b.source.url)
                if url_a and url_b and url_a != url_b:
                    continue
                if (a.source.name and b.source.name and
                    a.source.name.casefold() != b.source.name.casefold()):
                    continue
                if (a.source.publication_date and b.source.publication_date and
                    a.source.publication_date != b.source.publication_date):
                    continue
                if (a.clipping and b.clipping and a.clipping.sheet_total and
                    b.clipping.sheet_total and a.clipping.sheet_total != b.clipping.sheet_total):
                    continue

                score = 0.15
                signals = ["adjacent_pages"]
                independent = 0
                if url_a and url_b and url_a == url_b:
                    score += 0.36; independent += 1; signals.append("same_canonical_article_url")
                if a.source.name and b.source.name and a.source.name.casefold() == b.source.name.casefold():
                    score += 0.17; independent += 1; signals.append("same_source")
                if a.source.publication_date and b.source.publication_date == a.source.publication_date:
                    score += 0.09; independent += 1; signals.append("same_publication_date")
                if a.page_type == PageType.WEB and b.page_type == PageType.WEB:
                    score += 0.11; signals.append("both_web")
                # Site-independent print-wrapper evidence, never sufficient alone.
                print_a, print_b = self.marker(a, self.PRINT_ID), self.marker(b, self.PRINT_ID)
                visitors_a, visitors_b = self.marker(a, self.VISITORS), self.marker(b, self.VISITORS)
                if print_a and print_b and print_a != print_b:
                    continue
                if print_a and print_a == print_b:
                    score += 0.29; independent += 1; signals.append("same_print_wrapper_id")
                if visitors_a and visitors_a == visitors_b:
                    score += 0.13; independent += 1; signals.append("same_print_audience")
                if (a.clipping and b.clipping and a.clipping.sheet_current and
                    b.clipping.sheet_current == a.clipping.sheet_current + 1 and
                    a.clipping.sheet_total == b.clipping.sheet_total):
                    score += 0.38; independent += 1; signals.append("clipping_sheet_sequence")
                if all(r.article_id in (None, owner) for r in right):
                    score += 0.05
                left_main = [r for r in left if r.article_id == owner]
                if anchor_only:
                    left_main = [r for r in self.main_titles(a) if r.article_id == owner]
                    if not (url_a and url_b and url_a == url_b):
                        continue
                    signals.append("title_only_web_anchor")
                # A dominant text lane is evidence; not an ownership decision on its own.
                left_x = min(r.bbox[0] for r in left_main)
                right_x = min(r.bbox[0] for r in right)
                if abs(left_x / (a.page_width or 595) - right_x / (b.page_width or 595)) <= 0.11:
                    score += 0.08; independent += 1; signals.append("compatible_body_lane")
                score = min(1.0, score)
                if independent < 2 or score < self.MIN_SCORE:
                    continue
                existing = [link for link in links if link.document_id == a.document_id
                            and link.from_pdf_page == a.pdf_page and link.to_pdf_page == b.pdf_page
                            and link.article_candidate_id == owner]
                if any(link.contradictions and any(c not in {"low_continuation_score", "insufficient_evidence"}
                                                        for c in link.contradictions) for link in existing):
                    continue
                # Do not silently import sidebar or content from a different zone.
                zones = {(r.metadata.get("web_article_zone") or {}).get("zone_id") for r in right}
                if len(zones - {None}) > 1:
                    # WebArticleZoneResolver can split one long narrative lane
                    # at pictures/large paragraph gaps. Multiple IDs are okay
                    # only if all body blocks share the same horizontal lane.
                    x_lefts = [r.bbox[0] / (b.page_width or 595) for r in right]
                    if max(x_lefts) - min(x_lefts) > .085:
                        continue
                unassigned = [r for r in right if not r.article_id]
                if not unassigned:
                    continue
                for r in unassigned:
                    candidate = r.metadata.get("article_candidate_id")
                    if candidate and candidate != owner:
                        continue
                    r.article_id = owner
                    r.metadata["article_candidate_id"] = owner
                    r.metadata["content_scope"] = "main"
                    r.metadata["include_in_main_body"] = True
                    r.metadata["general_continuation"] = {
                        "method": "generic_document_continuation_v1", "score": round(score, 3),
                        "evidence": signals, "from_pdf_page": a.pdf_page}
                if not any(r.article_id == owner for r in unassigned):
                    continue
                if existing:
                    # A previously rejected diagnostic remains preserved in metadata.
                    link = existing[0]
                    link.status = "accepted"
                    link.method = "generic_document_continuation_v1"
                    link.confidence = round(score, 3)
                    link.evidence = list(dict.fromkeys([*link.evidence, *signals]))
                    link.contradictions = []
                else:
                    links.append(FlowLink(
                        document_id=a.document_id, article_candidate_id=owner,
                        from_pdf_page=a.pdf_page, to_pdf_page=b.pdf_page,
                        status="accepted", method="generic_document_continuation_v1",
                        confidence=round(score, 3), evidence=signals,
                        from_region_id=max(left_main, key=lambda r:r.bbox[3]).metadata.get("region_id"),
                        to_region_id=min([r for r in right if r.article_id == owner],
                                         key=lambda r:r.bbox[1]).metadata.get("region_id"),
                    ))
                report.append({"from": a.pdf_page, "to": b.pdf_page,
                                "article_id": owner, "score": round(score, 3),
                                "recovered_chars": sum(len(r.text or "") for r in unassigned if r.article_id == owner)})
        return report