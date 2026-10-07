from press_reputation.models.page import PageRecord, Region, RegionType, PageType


class BodyContinuationResolver:
    def __init__(self, threshold: float = 0.62) -> None:
        self.threshold = threshold

    def enrich(self, page: PageRecord) -> PageRecord:
        blocked_scopes = {"related", "advertisement", "boilerplate", "non_main"}

        body_regions = [
            region for region in page.regions
            if (region.type == RegionType.ARTICLE_BODY and region.article_id and region.bbox and
                not region.exclude_from_article_text and region.metadata.get("content_scope") not in blocked_scopes and
                (page.page_type != PageType.WEB or region.metadata.get("content_scope") == "main"))
        ]

        candidates = [
            region for region in page.regions
            if (region.type == RegionType.UNKNOWN and region.bbox and region.text and not region.exclude_from_article_text and
                region.metadata.get("content_scope") not in blocked_scopes and
                (page.page_type != PageType.WEB or region.metadata.get("content_scope") == "main"))
        ]

        if not body_regions or not candidates:
            return page

        # Ogni UNKNOWN recuperato diventa un riferimento per
        # recuperare eventuali frammenti successivi dello stesso articolo.
        changed = True

        while changed:
            changed = False

            for candidate in list(candidates):
                scores_by_article: dict[str, float] = {}

                for body in body_regions:
                    if (candidate.article_id and candidate.article_id != body.article_id):
                        continue

                    score = self.continuation_score(candidate, body)
                    article_id = body.article_id
                    scores_by_article[article_id] = max(score, scores_by_article.get(article_id, 0.0))

                ranked = sorted(scores_by_article.items(), key=lambda item: (-item[1], item[0]))

                if not ranked:
                    continue

                best_id, best_score = ranked[0]
                second_score = ranked[1][1] if len(ranked) > 1 else 0.0

                if (best_score < self.threshold or (not candidate.article_id and best_score - second_score < 0.12)):
                    candidate.metadata["body_continuation_candidates"] = [
                        {
                            "article_id": article_id,
                            "score": round(score, 4),
                        }
                        for article_id, score in ranked[:3]
                    ]
                    continue

                candidate.type = RegionType.ARTICLE_BODY
                candidate.article_id = best_id
                candidate.metadata["body_continuation_score"] = round(best_score, 4)
                candidate.metadata["body_detection_method"] = ("iterative_article_aware_v1")

                if page.page_type == PageType.WEB:
                    candidate.metadata["include_in_main_body"] = (candidate.metadata.get("content_scope") == "main" and
                                                                    not candidate.exclude_from_article_text)

                body_regions.append(candidate)
                candidates.remove(candidate)
                changed = True

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