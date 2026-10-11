import re

from press_reputation.models.page import PageRecord, RegionType


class PullQuoteResolver:
    @staticmethod
    def content_tokens(text: str) -> set[str]:
        return {
            token for token in re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE)
            if len(token) >= 4
        }

    def enrich(self, page: PageRecord, pages: list[PageRecord]) -> PageRecord:
        article_bodies = [
            region for other_page in pages for region in other_page.regions
            if region.type == RegionType.ARTICLE_BODY and region.article_id
            and region.text and not region.exclude_from_article_text
        ]

        for region in page.regions:
            if (region.type != RegionType.ARTICLE_BODY or not region.article_id or
                not region.text or not region.bbox or region.exclude_from_article_text):
                continue

            tokens = self.content_tokens(region.text)
            if len(tokens) < 6 or len(tokens) > 40:
                continue

            matches = []
            for other in article_bodies:
                if other is region or other.article_id != region.article_id:
                    continue

                other_tokens = self.content_tokens(other.text)
                coverage = len(tokens & other_tokens) / len(tokens)
                if coverage >= 0.78:
                    matches.append((coverage, other))

            if not matches:
                continue

            coverage, reference = max(matches, key=lambda item: item[0])
            nearby = [
                other for other in page.regions
                if other is not region and other.type == RegionType.ARTICLE_BODY
                and other.article_id == region.article_id and other.bbox
                and abs(other.bbox[0] - region.bbox[0]) <= 20.0
            ]

            region_font = region.style.get("dominant_font")
            reference_font = reference.style.get("dominant_font")
            style_distinct = bool(region_font and reference_font and region_font != reference_font)

            # Indizio geometrico prudente, non prova universale.
            vertical_isolation = all(
                min(abs(region.bbox[1] - other.bbox[3]), abs(other.bbox[1] - region.bbox[3])) >= 12.0
                for other in nearby
            )

            status = "accepted" if style_distinct or vertical_isolation else "candidate"
            region.metadata["pull_quote_resolution"] = {
                "method": "same_article_text_and_layout_v1",
                "status": status,
                "matched_body_region_id": reference.metadata.get("region_id"),
                "content_token_coverage": round(coverage, 4),
                "style_distinct": style_distinct,
                "vertical_isolation": vertical_isolation,
                "previous_type": region.type.value,
            }
            if status == "accepted":
                region.type = RegionType.PULL_QUOTE

        return page