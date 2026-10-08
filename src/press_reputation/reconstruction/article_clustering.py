from __future__ import annotations

import hashlib
from math import isfinite

from press_reputation.config import ArticleClusteringConfig
from press_reputation.models.page import (PageRecord,PageType,Region,RegionType,)
from press_reputation.reconstruction.flow_models import FlowLink
from press_reputation.review_index.models import ReviewIndexMatch


class ArticleClusteringResolver:
    EXCLUDED_TYPES = {
        RegionType.HEADER_METADATA,
        RegionType.SOURCE_NAME,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
        RegionType.FOOTER,
        RegionType.ADVERTISEMENT,
        RegionType.NAVIGATION,
        RegionType.RELATED_CONTENT,
    }

    EXCLUDED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation"
    }

    MEDIA_TYPES = {
        RegionType.IMAGE,
        RegionType.INFOGRAPHIC,
        RegionType.TABLE,
        RegionType.PULL_QUOTE,
    }

    def __init__(self,config: ArticleClusteringConfig | None = None,) -> None:
        self.config = config or ArticleClusteringConfig()

    @staticmethod
    def valid_box(region: Region) -> bool:
        box = region.bbox
        return (
            box is not None and len(box) == 4 and all(isfinite(value) for value in box) and
            box[2] > box[0] and box[3] > box[1])

    @staticmethod
    def region_id(region: Region) -> str | None:
        value = region.metadata.get("region_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def overlap(a: Region, b: Region) -> float:
        shared = max(0.0, min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]))
        width = min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
        return shared / width if width > 0 else 0.0

    @staticmethod
    def vertical_gap(a: Region, b: Region) -> float:
        return max(0.0, a.bbox[1] - b.bbox[3], b.bbox[1] - a.bbox[3])

    def eligible(self, region: Region) -> bool:
        return (
            region.type not in self.EXCLUDED_TYPES and not region.exclude_from_article_text
            and not region.metadata.get("inside_article_position_thumbnail")
            and not region.metadata.get("in_header_metadata_zone")
            and region.metadata.get("content_scope") not in self.EXCLUDED_SCOPES
        )

    @staticmethod
    def title_id(page: PageRecord,title: Region,) -> str | None:
        region_id = title.metadata.get("region_id")
        if not isinstance(region_id, str) or not region_id:
            return None

        raw = (f"{page.document_id}\0{region_id}").encode("utf-8")
        return ("article_"+ hashlib.sha256(raw).hexdigest()[:16])

    @staticmethod
    def record(region: Region, *, status: str, method: str, score: float | None = None,
                evidence: list[str] | None = None, alternatives: list[dict] | None = None,
                anchor_region_id: str | None = None) -> None:
        
        region.metadata["article_clustering"] = {
            "status": status,
            "method": method,
            "score": score,
            "evidence": evidence or [],
            "alternatives": alternatives or [],
            "anchor_region_id": anchor_region_id,
        }

    def assign(self, region: Region, article_id: str, *, method: str, score: float, evidence: list[str],
                anchor_region_id: str | None) -> bool:
        
        if region.article_id and region.article_id != article_id:
            self.record(region, status="conflict", method=method,
                        evidence=[
                            f"existing:{region.article_id}", f"proposed:{article_id}",
                        ],
                        anchor_region_id=anchor_region_id)
            return False

        region.article_id = article_id
        self.record(region, status="assigned", method=method, score=round(score, 4), evidence=evidence,
                    anchor_region_id=anchor_region_id)
        
        return True

    def title_barrier(self, page: PageRecord, anchor: Region, candidate: Region,) -> bool:
        for other in page.regions:
            if (
                other is anchor or other is candidate or other.type != RegionType.ARTICLE_TITLE or
                other.metadata.get("title_role") in {"not_main", "ambiguous"} or
                not self.valid_box(other) or self.overlap(other, candidate) < self.config.min_horizontal_overlap):
                continue

            if (anchor.bbox[3] <= other.bbox[1] <= candidate.bbox[1]):
                return True

        return False

    def score_body(self, candidate: Region, anchor: Region, assigned_bodies: list[Region], page: PageRecord, 
                    article_id: str, has_index_match: bool) -> tuple[float, list[str]]:
        if not (self.valid_box(candidate) and self.valid_box(anchor) and
                page.page_height and page.page_width):
            return 0.0, ["missing_geometry"]

        if (candidate.bbox[1] < anchor.bbox[1] or self.title_barrier(page, anchor, candidate)):
            return 0.0, ["title_barrier"]

        references = [body for body in assigned_bodies if self.valid_box(body)] or [anchor]

        best = max(references, key=lambda ref: (self.overlap(candidate, ref),
                                                -self.vertical_gap(candidate, ref)))
        
        horizontal = self.overlap(candidate, best)
        if (horizontal < self.config.min_horizontal_overlap):
            return 0.0, ["different_column"]

        gap = (self.vertical_gap(candidate, best) / page.page_height)
        if gap > self.config.max_local_gap_fraction:
            return 0.0, ["too_distant"]

        score = (0.25 * horizontal + 0.24 * (1 - gap / self.config.max_local_gap_fraction))
        evidence = ["horizontal_overlap", "local_vertical_continuity"]

        left_difference = (abs(candidate.bbox[0] - best.bbox[0]) / page.page_width)
        if (left_difference <= self.config.max_column_edge_difference):
            score += 0.16
            evidence.append("compatible_column_edge")

        if candidate.bbox[1] >= anchor.bbox[3]:
            score += 0.12
            evidence.append("after_title")

        candidate_font = candidate.style.get("dominant_font")
        reference_font = best.style.get("dominant_font")
        
        if (assigned_bodies and candidate_font and reference_font and candidate_font == reference_font):
            score += 0.08
            evidence.append("same_body_font")

        candidate_size = candidate.style.get("median_font_size")
        reference_size = best.style.get("median_font_size")
        
        if (assigned_bodies and candidate_size and reference_size and
            abs(candidate_size - reference_size) / max(candidate_size,reference_size) <= self.config.max_font_size_ratio_difference):
            score += 0.06
            evidence.append("compatible_body_font_size")

        web_id = candidate.metadata.get("article_candidate_id")
        
        if web_id:
            if web_id != article_id:
                return 0.0, ["different_web_candidate_id"]
            
            score += 0.18
            evidence.append("same_web_candidate_id")

        if has_index_match:
            score += 0.04
            evidence.append("matched_review_index_title")

        return min(score, 1.0), evidence

    def choose(self, region: Region, scored: list[tuple[float, str, list[str], str | None]], *, method: str) -> bool:
        ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
        if not ranked:
            return False

        best_score, best_id, evidence, anchor_id = (ranked[0])
        second_score = (ranked[1][0] if len(ranked) > 1 else 0.0)

        if (best_score < self.config.min_assignment_score or best_score - second_score < self.config.min_winning_margin):
            self.record(region, status="ambiguous", method=method, alternatives=[
                            {
                                "article_id": article_id,
                                "score": round(score, 4),
                                "evidence": reasons,
                            }
                        for score, article_id, reasons, _ in ranked[:3]])
            
            return False

        return self.assign(region, best_id, method=method, score=best_score, evidence=evidence, anchor_region_id=anchor_id)

    def assign_local(self, pages: list[PageRecord], index_matches: list[ReviewIndexMatch]) -> list[PageRecord]:
        matched_titles = {(match.pdf_page, match.title_region_id) for match in index_matches if match.status == "matched"}

        for page in pages:
            titles = [
                region for region in page.regions
                if (region.type == RegionType.ARTICLE_TITLE and self.valid_box(region) and self.eligible(region) and
                    region.metadata.get("title_role") not in {"not_main", "ambiguous"})
                ]

            anchors: dict[str, Region] = {}
            for title in titles:
                article_id = (title.metadata.get("article_candidate_id") or self.title_id(page, title))
                if not article_id:
                    self.record(title, status="unresolved", method="title_anchor_v1", evidence=["missing_stable_region_id"])
                    continue

                if self.assign(title, article_id, method="title_anchor_v1", score=1.0, evidence=["article_title"], 
                                anchor_region_id=(self.region_id(title))):
                    anchors[article_id] = title

            title_by_region_id = {self.region_id(title): title for title in titles if self.region_id(title)}

            for region in page.regions:
                if (region.type != RegionType.ARTICLE_SUBTITLE or not self.eligible(region)):
                    continue

                parent_id = (region.metadata.get("subtitle_resolution", {}).get("title_region_id"))
                parent = title_by_region_id.get(parent_id)
                if parent and parent.article_id:
                    self.assign(region, parent.article_id, method="subtitle_anchor_v1", score=1.0,
                                evidence=["resolved_title_reference"], anchor_region_id=parent_id)

            header_by_region_id = {
                self.region_id(region): region
                for region in page.regions
                if (region.type in {RegionType.ARTICLE_TITLE, RegionType.ARTICLE_SUBTITLE} and self.region_id(region))
            }
            for region in page.regions:
                if (region.type != RegionType.AUTHOR or not self.eligible(region)):
                    continue

                parent_id = (region.metadata.get("author_resolution", {}).get("header_region_id"))
                parent = (header_by_region_id.get(parent_id))
                if parent and parent.article_id:
                    self.assign(region, parent.article_id, method="author_anchor_v1", score=1.0,
                                evidence=["resolved_header_reference"], anchor_region_id=parent_id)

            # I body marcati dal vecchio resolver web
            # ereditano la sua identità senza assegnare
            # tutte le regioni della pagina.
            for region in page.regions:
                web_id = region.metadata.get("article_candidate_id")
                if (page.page_type == PageType.WEB and web_id and region.type == RegionType.ARTICLE_BODY and
                    self.eligible(region) and region.metadata.get("content_scope") == "main" and (not anchors or web_id in anchors)):
                    
                    self.assign(region, web_id, method="web_chain_v1", score=0.95, 
                                evidence=["existing_web_chain", "main_content_scope"], 
                                anchor_region_id=(self.region_id(anchors[web_id]) if web_id in anchors else None))

            bodies = [region for region in page.regions if (region.type == RegionType.ARTICLE_BODY and self.eligible(region) and
                                                            region.article_id is None and self.valid_box(region))]
            bodies.sort(key=lambda region: (region.bbox[1], region.bbox[0]))

            # Ogni body assegnato diventa riferimento
            # per i successivi, ma soltanto se supera
            # soglia e margine.
            for body in bodies:
                scored = []
                for article_id, title in (anchors.items()):
                    same_article_bodies = [other for other in page.regions if (other is not body and
                                                                                other.type == RegionType.ARTICLE_BODY and
                                                                                other.article_id == article_id and
                                                                                self.eligible(other))]
                    
                    score, reasons = (self.score_body(body, title, same_article_bodies, page, article_id,
                                                        (page.pdf_page, self.region_id(title)) in matched_titles))
                    
                    if score:
                        scored.append((score, article_id, reasons, self.region_id(title)))

                self.choose(body, scored, method="local_body_v1")

            # Un frammento già attribuito a una
            # continuazione web senza titolo su questa
            # pagina resta nella stessa catena.
            if not titles:
                for region in page.regions:
                    web_id = region.metadata.get("article_candidate_id")
                    if (page.page_type == PageType.WEB and web_id and
                        region.type in {RegionType.ARTICLE_BODY, RegionType.UNKNOWN} and self.eligible(region) and
                        region.article_id is None and region.metadata.get("content_scope") == "main"):
                        
                        self.assign(region, web_id, method=("web_continuation_v1"), score=0.90,
                                    evidence=["existing_web_chain"], anchor_region_id=None)

        return pages

    def link_accepted_flows(self, pages: list[PageRecord], links: list[FlowLink]) -> list[PageRecord]:
        regions_by_id = {
            (page.document_id, self.region_id(region)): region
            for page in pages
            for region in page.regions
            if self.region_id(region)
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
            existing = {article_id for article_id in (left.article_id, right.article_id) if article_id is not None}

            if existing - {expected}:
                link.status = "rejected"
                link.contradictions.append("conflicting_article_ids")
                continue

            for region in (left, right):
                if region.article_id is None:
                    self.assign(region, expected, method="accepted_flow_v1", score=1.0, evidence=["accepted_flow_link"],
                                anchor_region_id=link.from_region_id)

        return pages

    def assign_recovered_body(self, page: PageRecord) -> PageRecord:
        # BodyContinuationResolver assegna l'ID durante UNKNOWN → BODY.
        # Si annotano i body che rimangono senza identità.
        for region in page.regions:
            if (region.type == RegionType.ARTICLE_BODY and region.article_id is None and self.eligible(region)):
                region.metadata.setdefault(
                    "article_clustering",
                    {
                        "status": "unresolved",
                        "method": "post_body_continuation_v1",
                        "evidence": ["no_unique_article_id"],
                    },
                )

        return page

    def assign_section_headers_and_media(self, page: PageRecord) -> PageRecord:
        by_region_id = {self.region_id(region): region for region in page.regions if self.region_id(region)}

        for region in page.regions:
            if (region.type != RegionType.ARTICLE_SECTION_HEADER or region.article_id or not self.eligible(region)):
                continue

            resolution = region.metadata.get("section_header_resolution", {})
            before = by_region_id.get(resolution.get("previous_body_region_id"))
            after = by_region_id.get(resolution.get("following_body_region_id"))

            if (before and after and before.article_id and before.article_id == after.article_id):
                self.assign(region, before.article_id, method="body_sandwich_v1", score=0.95,
                            evidence=["same_article_before_after"], anchor_region_id=self.region_id(before))

        assigned_bodies = [region for region in page.regions if (region.type == RegionType.ARTICLE_BODY and
                                                                    region.article_id and self.valid_box(region) and
                                                                    self.eligible(region))]

        for media in page.regions:
            if (media.type not in self.MEDIA_TYPES or media.article_id or not self.valid_box(media) or
                media.metadata.get("exclude_from_article_media")):
                continue

            scored = []

            for body in assigned_bodies:
                gap = (self.vertical_gap(media, body) / page.page_height if page.page_height else 1.0)
                overlap = self.overlap(media, body)

                if (gap > self.config.max_media_gap_fraction or overlap < self.config.min_horizontal_overlap):
                    continue

                score = (0.40 * overlap + 0.40 * (1 - gap / self.config.max_media_gap_fraction))
                scored.append((score, body.article_id, ["near_assigned_body", "media_body_overlap"], self.region_id(body)))

            # Due body dello stesso articolo non sono concorrenti distinti.
            best_by_article = {}
            for item in scored:
                article_id = item[1]
                if (article_id not in best_by_article or item[0] > best_by_article[article_id][0]):
                    best_by_article[article_id] = item

            self.choose(media, list(best_by_article.values()), method="local_media_v1")

        assigned_media = [region for region in page.regions if (region.type in self.MEDIA_TYPES and
                                                                region.article_id and self.valid_box(region))]

        for caption in page.regions:
            if (caption.type != RegionType.CAPTION or caption.article_id or not self.valid_box(caption) or
                not self.eligible(caption)):
                continue

            scored = []

            for media in assigned_media:
                gap = (self.vertical_gap(caption, media) / page.page_height if page.page_height else 1.0)
                if gap > self.config.max_caption_image_gap_fraction:
                    continue

                overlap = self.overlap(caption, media)
                if overlap < self.config.min_horizontal_overlap:
                    continue

                score = (0.45 * overlap + 0.45 * (1 - gap / self.config.max_caption_image_gap_fraction))
                scored.append((score, media.article_id, ["near_assigned_media"], self.region_id(media)))

            best_by_article = {}
            for item in scored:
                article_id = item[1]
                if (article_id not in best_by_article or item[0] > best_by_article[article_id][0]):
                    best_by_article[article_id] = item

            self.choose(caption, list(best_by_article.values()), method="caption_media_v1")

        return page