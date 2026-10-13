from __future__ import annotations

from dataclasses import dataclass

from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.reconstruction.article_clustering import ArticleClusteringResolver


@dataclass
class MembershipDecision:
    article_id: str
    score: float
    evidence: list[str]
    title_region_id: str | None


class GlobalArticleMembershipResolver:
    """Group-level recovery for unassigned ARTICLE_BODY, independent of publication.

    Existing assignments are immutable. A group is assigned only with a unique
    owner, independent structural support and no competing article title.
    """
    BLOCKED = {"related", "advertisement", "boilerplate", "navigation", "non_main"}
    MIN_SCORE = 0.68
    MIN_MARGIN = 0.14

    def __init__(self) -> None:
        self.cluster = ArticleClusteringResolver()

    @staticmethod
    def horiz(a: list[float], b: list[float]) -> float:
        overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        return overlap / max(1.0, min(a[2] - a[0], b[2] - b[0]))

    @staticmethod
    def box(group: list[Region]) -> list[float]:
        return [min(r.bbox[0] for r in group), min(r.bbox[1] for r in group),
                max(r.bbox[2] for r in group), max(r.bbox[3] for r in group)]

    @classmethod
    def usable(cls, r: Region) -> bool:
        return (r.type == RegionType.ARTICLE_BODY and r.bbox is not None
                and r.bbox[2] > r.bbox[0] and r.bbox[3] > r.bbox[1]
                and bool((r.text or "").strip()) and not r.exclude_from_article_text
                and r.metadata.get("content_scope") not in cls.BLOCKED)

    @staticmethod
    def zone(r: Region) -> str | None:
        return (r.metadata.get("web_article_zone") or {}).get("zone_id")

    def score(self, page: PageRecord, group: list[Region], title: Region,
                owner_regions: list[Region]) -> MembershipDecision | None:
        owner = title.article_id
        if not owner or not title.bbox:
            return None
        b = self.box(group)
        # Do not extend an existing article across a *later* headline on the
        # same page: that often denotes a distinct news item (web related).
        if any(r.article_id == owner and r.type == RegionType.ARTICLE_BODY
                and r.bbox and r.bbox[3] < title.bbox[1] - 5.0
                and self.horiz(r.bbox, title.bbox) >= 0.25
                for r in owner_regions):
            return None
        owner_ids = {r.article_id for r in group if r.article_id}
        if owner_ids - {owner}:
            return None
        candidate_ids = {r.metadata.get("article_candidate_id") for r in group
                            if r.metadata.get("article_candidate_id")}
        if candidate_ids - {owner}:
            return None
        # A confirmed competing headline in or immediately above the orphan
        # column is a hard barrier, not a soft negative weight.
        for other in page.regions:
            if other is title or other.type != RegionType.ARTICLE_TITLE or not other.bbox:
                continue
            if other.exclude_from_article_text or other.metadata.get("title_role") in {
                "teaser", "not_main", "rejected_non_main"}:
                continue
            if other.article_id not in (None, owner) and (
                self.horiz(other.bbox, b) > 0.3 and other.bbox[1] < b[3]
                and other.bbox[3] > title.bbox[1]
            ):
                return None

        score = 0.0
        evidence: list[str] = []
        zone = self.zone(title)
        group_zones = {self.zone(r) for r in group if self.zone(r)}
        if len(group_zones) > 1 or (zone and group_zones and group_zones != {zone}):
            return None
        if zone and group_zones == {zone}:
            score += 0.28
            evidence.append("same_editorial_zone")
        if candidate_ids == {owner}:
            score += 0.25
            evidence.append("same_article_candidate")

        # Header-to-first-column support.
        hb = self.horiz(b, title.bbox)
        vertical_distance = b[1] - title.bbox[3]
        if hb >= 0.45 and -6 <= vertical_distance <= 320:
            score += 0.28 * hb + 0.13 * max(0.0, 1 - max(0, vertical_distance) / 320)
            evidence.append("header_to_body_geometry")

        # Evidence from an existing owned body, including neighbouring columns.
        for r in owner_regions:
            if r not in group and r.bbox:
                xgap = max(0.0, b[0] - r.bbox[2], r.bbox[0] - b[2])
                width = max(1.0, min(b[2] - b[0], r.bbox[2] - r.bbox[0]))
                vertical = max(0.0, min(b[3], r.bbox[3]) - max(b[1], r.bbox[1]))
                v_ratio = vertical / max(1.0, min(b[3] - b[1], r.bbox[3] - r.bbox[1]))
                if (xgap <= width * 0.48 and v_ratio >= 0.45):
                    score += 0.38
                    evidence.append("adjacent_owned_column")
                    break
                if (self.horiz(b, r.bbox) >= 0.65 and
                    max(0.0, b[1] - r.bbox[3], r.bbox[1] - b[3]) <= 75):
                    score += 0.35
                    evidence.append("same_column_continuity")
                    break

        unambiguous = [r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                        and r.bbox and not r.exclude_from_article_text
                        and r.metadata.get("title_role") not in
                        {"teaser", "not_main", "rejected_non_main"}]
        if (len(unambiguous) == 1 and unambiguous[0] is title
            and 0 <= vertical_distance <= 320
            and hb >= 0.45 and sum(len(r.text or "") for r in group) >= 200):
            score += 0.32
            evidence.append("sole_main_title_with_substantial_body")
        # A photo between the title/deck and the first narrative column
        # is common in magazine/newspaper layouts and explains a large gap.
        if 120 < vertical_distance <= 320:
            images = [r for r in page.regions if r.type == RegionType.IMAGE
                        and r.bbox and title.bbox[3] <= r.bbox[1] <= b[1]
                        and self.horiz(r.bbox, title.bbox) >= .30]
            if images:
                score += 0.12
                evidence.append("intervening_editorial_image")
        if (title.metadata.get("title_candidate") or {}).get("index_entry_id"):
            score += 0.07
            evidence.append("review_index_support")
        if any((r.metadata.get("body_seed_evaluation") or {}).get("accepted") for r in group):
            score += 0.07
            evidence.append("body_seed_support")
        # The same group must have at least one *structural* link to a title.
        if not any(e in evidence for e in (
            "same_editorial_zone", "header_to_body_geometry",
            "adjacent_owned_column", "same_column_continuity")):
            return None
        return MembershipDecision(owner, round(min(score, 1.0), 4), evidence,
                                    title.metadata.get("region_id"))

    def enrich_page(self, page: PageRecord) -> list[dict]:
        columns = self.cluster.body_columns(page)
        titles = [r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                    and r.article_id and r.bbox and not r.exclude_from_article_text
                    and r.metadata.get("title_role") not in {
                        "teaser", "ambiguous", "not_main", "rejected_non_main"}]
        report: list[dict] = []
        # Multiple rounds allow recovery of a second column after the first.
        for _ in range(min(4, len(columns))):
            changed = False
            for column in columns:
                group = [r for r in column if self.usable(r) and not r.article_id]
                if not group:
                    continue
                if any(r.article_id for r in column):
                    # This mixed column is left to the local-body resolver.
                    continue
                ranked = []
                for title in titles:
                    refs = [r for r in page.regions if self.usable(r) and
                            r.article_id == title.article_id]
                    result = self.score(page, group, title, refs)
                    if result:
                        ranked.append(result)
                ranked.sort(key=lambda d: (-d.score, d.article_id))
                accepted = (bool(ranked) and ranked[0].score >= self.MIN_SCORE
                            and (len(ranked) == 1 or ranked[0].score - ranked[1].score >= self.MIN_MARGIN))
                status = "assigned" if accepted else "unresolved"
                for r in group:
                    r.metadata["global_membership"] = {
                        "status": status,
                        "candidates": [{"article_id": d.article_id, "score": d.score}
                                        for d in ranked[:3]],
                        "method": "global_group_membership_v1",
                    }
                if not accepted:
                    continue
                winner = ranked[0]
                for r in group:
                    r.article_id = winner.article_id
                    # Avoid stale candidate ID being different from article_id.
                    r.metadata["article_candidate_id"] = winner.article_id
                    r.metadata["content_scope"] = "main"
                    r.metadata["include_in_main_body"] = True
                    r.metadata["article_clustering"] = {
                        "status": "assigned", "method": "global_group_membership_v1",
                        "score": winner.score, "evidence": winner.evidence,
                        "anchor_region_id": winner.title_region_id, "alternatives": []}
                changed = True
                report.append({"document_id": page.document_id, "pdf_page": page.pdf_page,
                                "article_id": winner.article_id,
                                "chars": sum(len(r.text or "") for r in group),
                                "regions": len(group)})
            if not changed:
                break
        return report

    def enrich_document(self, pages: list[PageRecord]) -> list[dict]:
        return [item for page in pages for item in self.enrich_page(page)]