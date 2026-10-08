import re

from press_reputation.classification.web_content_config import InlineIntrusionConfig
from press_reputation.classification.web_main_content import valid_bbox, word_count
from press_reputation.models.page import PageRecord, PageType, Region, RegionType


class InlineIntrusionDetector:
    BLOCKED_TYPES = {
        RegionType.HEADER_METADATA, RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER, RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE, RegionType.CLIPPING_SHEET,
        RegionType.WATERMARK, RegionType.RIGHTS_NOTICE,
        RegionType.ADVERTISEMENT, RegionType.RELATED_CONTENT,
        RegionType.NAVIGATION, RegionType.FOOTER,
        RegionType.ARTICLE_TITLE, RegionType.ARTICLE_SUBTITLE,
        RegionType.ARTICLE_SECTION_HEADER, RegionType.AUTHOR,
        RegionType.IMAGE, RegionType.CAPTION, RegionType.TABLE,
        RegionType.INFOGRAPHIC, RegionType.ARTICLE_POSITION_THUMBNAIL,
    }

    def __init__(self, config: InlineIntrusionConfig | None = None) -> None:
        self.config = config or InlineIntrusionConfig()

    @staticmethod
    def overlap(left: Region, right: Region) -> float:
        shared = max(0.0, min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0]))
        width = min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0])
        return shared / width if width > 0 else 0.0

    def eligible(self, region: Region) -> bool:
        return (
            region.type in {RegionType.UNKNOWN, RegionType.ARTICLE_BODY} and
            bool((region.text or "").strip()) and valid_bbox(region) and not region.exclude_from_article_text and
            bool(region.metadata.get("layout_area_id")) and region.metadata.get("content_scope") in {"main", "unknown"} and
            not region.metadata.get("in_header_metadata_zone") and not region.metadata.get("inside_article_position_thumbnail")
        )

    def body_pair(self, page: PageRecord, candidate: Region) -> tuple[Region, Region] | None:
        area_id = candidate.metadata.get("layout_area_id")
        bodies = [
            region for region in page.regions
            if (region is not candidate and region.type == RegionType.ARTICLE_BODY and region.metadata.get("content_scope") == "main" and
                region.metadata.get("layout_area_id") == area_id and valid_bbox(region) and not region.exclude_from_article_text and
                self.overlap(region, candidate) >= self.config.min_horizontal_overlap)
            ]
        before = [region for region in bodies if 0 <= candidate.bbox[1] - region.bbox[3] <= self.config.max_body_gap]
        after = [region for region in bodies if 0 <= region.bbox[1] - candidate.bbox[3] <= self.config.max_body_gap]

        if not before or not after:
            return None

        return (
            max(before, key=lambda region: region.bbox[3]),
            min(after, key=lambda region: region.bbox[1]),
        )

    def style_difference(self, candidate: Region, before: Region, after: Region) -> bool:
        font = candidate.style.get("dominant_font")
        body_fonts = {
            region.style.get("dominant_font") for region in (before, after)
            if region.style.get("dominant_font")
        }
        if font and body_fonts and font not in body_fonts:
            return True

        size = candidate.style.get("median_font_size")
        body_sizes = [
            region.style.get("median_font_size") for region in (before, after)
            if region.style.get("median_font_size")
        ]
        if size and body_sizes:
            reference = sum(body_sizes) / len(body_sizes)
            if abs(size - reference) / reference >= self.config.min_font_size_difference:
                return True

        for trait in ("bold", "italic"):
            value = candidate.style.get(f"{trait}_ratio")
            evidence = candidate.style.get(f"{trait}_evidence_fraction", 0.0)
            body_values = [
                region.style.get(f"{trait}_ratio") for region in (before, after)
                if region.style.get(f"{trait}_ratio") is not None
                    and region.style.get(f"{trait}_evidence_fraction", 0.0) >= self.config.min_style_evidence_fraction
            ]
            if (value is not None and body_values and evidence >= self.config.min_style_evidence_fraction and
                abs(value - sum(body_values) / len(body_values)) >= self.config.min_trait_difference):
                return True

        return False

    @staticmethod
    def lexical_similarity(candidate: Region, before: Region, after: Region) -> float:
        def tokens(text: str | None) -> set[str]:
            return {
                token for token in re.findall(r"\w+", (text or "").casefold())
                if len(token) >= 4
            }

        current = tokens(candidate.text)
        context = tokens(before.text) | tokens(after.text)
        return len(current & context) / len(current | context) if current and context else 0.0

    @staticmethod
    def marker_kind(text: str) -> str | None:
        lowered = text.casefold().strip()
        if re.search(r"\b(?:pubblicità|pubblicita|sponsorizzato|sponsored|adv|advertisement)\b", lowered):
            return "advertisement"

        if re.search(
            r"^(?:leggi anche|articoli correlati|notizie correlate|approfondimenti|"
            r"potrebbe interessarti|consigliati per te)\b",
            lowered,
        ):
            return "related"

        return None

    @staticmethod
    def link_like(text: str) -> bool:
        return bool(re.search(
            r"https?://|www\.|(?:^|\n)\s*(?:leggi|scopri|vai all'articolo)\b|[→›]\s*$",
            text, flags=re.IGNORECASE,
        ))

    def enrich(self, page: PageRecord) -> PageRecord:
        if page.page_type != PageType.WEB:
            return page

        candidates = sorted(
            (region for region in page.regions if self.eligible(region)),
            key=lambda region: (region.bbox[1], region.bbox[0]),
        )

        for region in candidates:
            # Un candidato escluso poco prima non può fungere da body anchor.
            if region.metadata.get("content_scope") not in {"main", "unknown"}:
                continue

            pair = self.body_pair(page, region)
            if pair is None:
                continue

            before, after = pair
            text = region.text or ""
            marker = self.marker_kind(text)
            short = word_count(text) <= self.config.max_words_for_short_signal
            link = self.link_like(text)
            different_style = self.style_difference(region, before, after)
            lexical = self.lexical_similarity(region, before, after)
            gap = max(region.bbox[1] - before.bbox[3], after.bbox[1] - region.bbox[3])

            components = {"body_before_and_after": 0.18}
            if marker:
                components["editorial_marker"] = 0.70 if marker == "advertisement" else 0.55
            if link:
                components["link_like"] = 0.22
            if short:
                components["short_text"] = 0.10
            if different_style:
                components["style_difference"] = 0.13
            if gap <= self.config.max_body_gap / 2:
                components["close_to_body"] = 0.08
            if lexical <= self.config.max_lexical_similarity:
                components["lexical_discontinuity"] = 0.06

            score = round(min(sum(components.values()), 1.0), 4)
            supported = bool(marker or (link and different_style))
            accepted = supported and score >= self.config.min_score

            region.metadata["inline_intrusion"] = {
                "accepted": accepted, 
                "score": score, 
                "components": components,
                "marker_kind": marker, 
                "lexical_similarity": round(lexical, 4),
                "before_body_region_id": before.metadata.get("region_id"),
                "after_body_region_id": after.metadata.get("region_id"),
                "method": "body_sandwich_inline_v1",
            }

            if not accepted:
                continue

            kind = marker or "related"
            region.metadata["type_before_inline_intrusion"] = region.type.value
            region.type = RegionType.ADVERTISEMENT if kind == "advertisement" else RegionType.RELATED_CONTENT
            region.exclude_from_article_text = True
            region.metadata["content_scope"] = kind
            region.metadata["content_scope_reason"] = "inline_intrusion_detector"
            region.metadata["include_in_main_body"] = False

        return page