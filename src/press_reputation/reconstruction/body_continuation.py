from press_reputation.models.page import PageRecord, Region, RegionType, PageType


class BodyContinuationResolver:
    def __init__(self, threshold: float = 0.62) -> None:
        self.threshold = threshold

    def enrich(self, page: PageRecord) -> PageRecord:
        body_regions = [
            region
            for region in page.regions
            if region.type == RegionType.ARTICLE_BODY
            and region.bbox
            and not region.exclude_from_article_text
            and region.metadata.get("content_scope") not in {"related", "advertisement", "boilerplate", "non_main"}
            and (page.page_type != PageType.WEB or region.metadata.get("content_scope") == "main")
        ]

        if not body_regions:
            return page

        candidates = [
            region
            for region in page.regions
            if region.type == RegionType.UNKNOWN
            and region.bbox
            and region.text
            and not region.exclude_from_article_text
            and region.metadata.get("content_scope") not in {"related", "advertisement", "boilerplate", "non_main"}
            and (page.page_type != PageType.WEB or region.metadata.get("content_scope") == "main")
        ]

        for candidate in candidates:
            best_score = max(
                self.continuation_score(candidate, body)
                for body in body_regions
            )

            if best_score >= self.threshold:
                candidate.type = RegionType.ARTICLE_BODY
                candidate.metadata["body_continuation_score"] = round(best_score, 4)
                
                if page.page_type == PageType.WEB:
                    candidate.metadata["include_in_main_body"] = {
                        candidate.metadata.get("content_scope") == "main" and not candidate.exclude_from_article_text
                    }

        return page

    def continuation_score(self, candidate: Region, body: Region) -> float:
        return (
            self.column_similarity(candidate, body) * 0.35
            + self.width_similarity(candidate, body) * 0.20
            + self.font_size_similarity(candidate, body) * 0.25
            + self.vertical_continuity(candidate, body) * 0.20
        )

    @staticmethod
    def column_similarity(a: Region, b: Region) -> float:
        ax0, _, ax1, _ = a.bbox
        bx0, _, bx1, _ = b.bbox

        overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        base = max(min(ax1 - ax0, bx1 - bx0), 1.0)

        return overlap / base

    @staticmethod
    def width_similarity(a: Region, b: Region) -> float:
        aw = a.bbox[2] - a.bbox[0]
        bw = b.bbox[2] - b.bbox[0]

        return min(aw, bw) / max(aw, bw, 1.0)

    @staticmethod
    def font_size_similarity(a: Region, b: Region) -> float:
        a_size = a.style.get("median_font_size")
        b_size = b.style.get("median_font_size")

        if not a_size or not b_size:
            return 0.5

        return min(a_size, b_size) / max(a_size, b_size)

    @staticmethod
    def vertical_continuity(a: Region, b: Region) -> float:
        gap = abs(a.bbox[1] - b.bbox[3])

        if gap <= 20:
            return 1.0

        if gap <= 60:
            return 0.7

        if gap <= 120:
            return 0.3

        return 0.0