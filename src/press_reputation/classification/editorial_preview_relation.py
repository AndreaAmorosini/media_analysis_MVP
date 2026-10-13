"""Identify a newspaper clipping teaser printed ABOVE its main article.

Do not merge real articles just because their keywords overlap: this resolver
requires an explicit preview-to-body layout and corroborating common tokens.
"""
from __future__ import annotations

import re
from press_reputation.models.page import PageRecord, RegionType


class EditorialPreviewRelationResolver:
    STOP = {"il","lo","la","i","gli","le","un","una","con","per","del","della",
            "al","alla","e","di","da","nel","nell","alle","dei","dai","sul","sulla"}

    @classmethod
    def tokens(cls, value: str) -> set[str]:
        return {s for s in re.findall(r"[\wà-ÿ]+", value.casefold())
                if len(s) > 2 and s not in cls.STOP}

    @staticmethod
    def y(region):
        return region.bbox[1] if region.bbox else 10**9

    def enrich(self, page: PageRecord) -> list[dict]:
        decisions = []
        if page.page_type.value == "web":
            return decisions
        titles = sorted([r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                            and r.bbox and r.text and not r.exclude_from_article_text], key=self.y)
        if len(titles) != 2 or not page.page_height:
            return decisions
        upper, lower = titles
        if not (upper.bbox[3] < lower.bbox[1] and upper.bbox[1] < page.page_height * .45
                and lower.bbox[1] < page.page_height * .72):
            return decisions
        a, b = self.tokens(upper.text), self.tokens(lower.text)
        overlap = len(a & b)
        similarity = overlap / max(1,min(len(a),len(b)))
        if overlap < 3 or similarity < .46:
            return decisions
        # Main headline must be visually larger or backed by more main-body text.
        upper_h = upper.bbox[3]-upper.bbox[1]
        lower_h = lower.bbox[3]-lower.bbox[1]
        below_body = [r for r in page.regions if r.type == RegionType.ARTICLE_BODY
                        and r.bbox and r.bbox[1]>=lower.bbox[3] and not r.exclude_from_article_text]
        if lower_h < upper_h*1.12 and not below_body:
            return decisions
        # Strong explicit preview signal for newspaper clippings: see-page pointer.
        between_text = " ".join(r.text or "" for r in page.regions if r.bbox and
                                upper.bbox[1] <= r.bbox[1] <= lower.bbox[1])
        has_page_pointer = bool(re.search(r"\b(?:a|alle|pag\.?|pagina)\s*pag\.?\s*\d+|\ba\s+pag\.?\s*\d+", between_text, re.I))
        if not has_page_pointer and (similarity < .64 or lower_h < upper_h*1.5):
            return decisions
        # Preserve this relation, but keep preview text available for audit.
        upper.metadata["editorial_preview_relation"] = {
            "status": "teaser", "main_title_region_id": lower.metadata.get("region_id"),
            "token_overlap": overlap, "relative_similarity": round(similarity, 3),
            "page_pointer": has_page_pointer,
        }
        upper.metadata["title_role"] = "teaser"
        upper.type = RegionType.RELATED_CONTENT
        upper.exclude_from_article_text = True
        upper.article_id = None
        # Only body blocks strictly BETWEEN the two titles may be preview copy.
        for r in page.regions:
            if (r.type == RegionType.ARTICLE_BODY and r.bbox and r.bbox[1] >= upper.bbox[3]
                    and r.bbox[3] <= lower.bbox[1] and not r.article_id):
                r.metadata["editorial_preview_relation"] = {"status":"teaser_body"}
                r.type = RegionType.RELATED_CONTENT
                r.exclude_from_article_text = True
        decisions.append({"document_id":page.document_id,"pdf_page":page.pdf_page,
                            "status":"teaser_connected_to_main", "upper":upper.text,
                            "main":lower.text,"similarity":round(similarity,3)})
        return decisions