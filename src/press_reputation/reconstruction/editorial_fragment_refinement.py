from __future__ import annotations

import re
from press_reputation.models.page import PageRecord, RegionType


class EditorialFragmentRefinementResolver:
    """Recovers only UNKNOWN fragments enclosed by coherent assigned body.

    Does not relabel known section headings or replace OCR text.
    """
    BLOCKED = {"related", "advertisement", "boilerplate", "non_main", "navigation"}
    UI = re.compile(r"^(south|chevron_(?:right|left)|menu|oggi|next article)$", re.I)

    @staticmethod
    def overlap(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0])) / max(1, min(a[2]-a[0], b[2]-b[0]))

    def enrich_page(self, page: PageRecord) -> list[dict]:
        report = []
        bodies = [r for r in page.regions if r.type == RegionType.ARTICLE_BODY and r.article_id
                    and r.bbox and not r.exclude_from_article_text]
        for fragment in page.regions:
            if (fragment.type != RegionType.UNKNOWN or not fragment.text or not fragment.bbox
                or fragment.article_id or fragment.exclude_from_article_text
                or fragment.metadata.get("content_scope") in self.BLOCKED
                or fragment.metadata.get("in_header_metadata_zone")
                or self.UI.fullmatch(fragment.text.strip())):
                continue
            # Never treat Docling's heading as body on proximity alone.
            if fragment.raw_label == "section_header":
                continue
            text = fragment.text.strip()
            if len(text) < 24 or len(text.split()) < 4:
                continue
            options = []
            for body in bodies:
                if self.overlap(fragment.bbox, body.bbox) < .72:
                    continue
                gap = max(0, fragment.bbox[1] - body.bbox[3], body.bbox[1] - fragment.bbox[3])
                if gap > 28:
                    continue
                # High precision: a candidate must continue a sentence OR be
                # enclosed by two body segments with the same owner.
                neighbours = [r for r in bodies if r.article_id == body.article_id
                                and r is not body and self.overlap(fragment.bbox,r.bbox) >= .72]
                before = any(r.bbox[3] <= fragment.bbox[1] and
                                fragment.bbox[1]-r.bbox[3] <= 35 for r in [body,*neighbours])
                after = any(r.bbox[1] >= fragment.bbox[3] and
                            r.bbox[1]-fragment.bbox[3] <= 35 for r in [body,*neighbours])
                continuation = (before and after) or (before and text[0].islower())
                if continuation:
                    options.append(body.article_id)
            if len(set(options)) != 1:
                continue
            owner = options[0]
            fragment.type = RegionType.ARTICLE_BODY
            fragment.article_id = owner
            fragment.metadata["article_candidate_id"] = owner
            fragment.metadata["content_scope"] = "main"
            fragment.metadata["include_in_main_body"] = True
            fragment.metadata["editorial_fragment_refinement"] = {
                "status":"assigned", "method":"enclosed_sentence_fragment_v1"}
            report.append({"page":page.pdf_page, "article_id":owner,
                            "region_id":fragment.metadata.get("region_id"), "chars":len(text)})
        return report

    def enrich_document(self, pages: list[PageRecord]) -> list[dict]:
        return [entry for page in pages for entry in self.enrich_page(page)]