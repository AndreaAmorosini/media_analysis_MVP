from collections import Counter

from press_reputation.config import NewspaperContinuationConfig
from press_reputation.lookup import normalize_entity_key
from press_reputation.models.page import PageRecord, PageType, Region, RegionType
from press_reputation.reconstruction.article_clustering import ArticleClusteringResolver
from press_reputation.reconstruction.flow_models import FlowLink
from press_reputation.review_index.matcher import title_similarity
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch


class NewspaperContinuationResolver:
    def __init__(self, config: NewspaperContinuationConfig | None = None) -> None:
        self.config = config or NewspaperContinuationConfig()
        self.clustering = ArticleClusteringResolver()
        self.decisions: list[dict] = []

    @staticmethod
    def usable(page: PageRecord) -> bool:
        return page.page_type not in {PageType.WEB, PageType.INDEX}

    def article_ids(self, page: PageRecord) -> set[str]:
        return {
            region.article_id for region in page.regions
            if (region.article_id and region.type in {RegionType.ARTICLE_TITLE, RegionType.ARTICLE_BODY} and
                self.clustering.eligible(region))
        }

    def previous_article_ids(self, page: PageRecord) -> set[str]:
        ids = self.article_ids(page)
        if ids:
            return ids

        # Un foglio intermedio può contenere solo immagini.
        return {
            region.article_id for region in page.regions
            if (region.article_id and region.metadata.get("newspaper_continuation", {}).get("status") == "accepted")
        }

    @staticmethod
    def titles(page: PageRecord) -> list[Region]:
        return [
            region for region in page.regions
            if (region.type == RegionType.ARTICLE_TITLE and region.text and
                not region.exclude_from_article_text and
                region.metadata.get("title_role") not in {"not_main", "ambiguous"})
        ]

    @staticmethod
    def source_key(page: PageRecord) -> str:
        # Se il lookup ha riconosciuto un alias, preferire il nome canonico.
        for region in page.regions:
            if region.type != RegionType.SOURCE_NAME:
                continue

            match = region.metadata.get("entity_lookup") or {}
            if match.get("kind") == "source" and not match.get("ambiguous"):
                return normalize_entity_key(match.get("canonical_name") or "")

        return normalize_entity_key(page.source.name or "")

    @staticmethod
    def sheet_pair(left: PageRecord, right: PageRecord) -> bool:
        a, b = left.clipping, right.clipping
        return bool(
            a and b and a.sheet_current is not None and b.sheet_current is not None
            and a.sheet_total is not None and b.sheet_total is not None
            and a.sheet_total >= 2 and a.sheet_total == b.sheet_total
            and 1 <= a.sheet_current < a.sheet_total
            and b.sheet_current == a.sheet_current + 1
        )

    @staticmethod
    def sheet_conflict(left: PageRecord, right: PageRecord) -> bool:
        a, b = left.clipping, right.clipping
        if not a or not b:
            return False

        if a.sheet_total is not None and b.sheet_total is not None and a.sheet_total != b.sheet_total:
            return True

        return bool(
            a.sheet_current is not None and b.sheet_current is not None
            and b.sheet_current != a.sheet_current + 1
        )

    def index_entry(self, anchor: PageRecord, article_id: str, entries: dict[str, ReviewIndexEntry], matches: list[ReviewIndexMatch]) -> ReviewIndexEntry | None:
        title_ids = {
            region.metadata.get("region_id") for region in self.titles(anchor)
            if region.article_id == article_id
        }

        for match in matches:
            if (match.status == "matched" and match.pdf_page == anchor.pdf_page and
                match.title_region_id in title_ids):
                entry = entries.get(match.entry_id)
                if entry and entry.document_id == anchor.document_id:
                    return entry

        # Il prior del TitleResolver è più debole di un match confermato.
        for title in self.titles(anchor):
            if title.article_id != article_id:
                continue

            prior = title.metadata.get("title_candidate") or {}
            entry = entries.get(prior.get("index_entry_id"))
            if (entry and entry.document_id == anchor.document_id and
                title_similarity(title.text or "", entry.title) >= self.config.index_title_similarity):
                return entry

        return None

    def title_conflict(self, anchor: PageRecord, right: PageRecord) -> bool:
        anchor_titles = self.titles(anchor)
        right_titles = self.titles(right)
        if not right_titles:
            return False

        if len(anchor_titles) != 1 or len(right_titles) != 1:
            return True

        return (
            title_similarity(anchor_titles[0].text or "", right_titles[0].text or "") < self.config.repeated_title_similarity
        )

    def evaluate(self, left: PageRecord, right: PageRecord, anchor: PageRecord, article_id: str, entry: ReviewIndexEntry | None) -> tuple[float, list[str], list[str]]:
        evidence = ["adjacent_pdf_pages", "unique_previous_article"]
        contradictions: list[str] = []
        score = 0.05

        if self.sheet_conflict(left, right):
            contradictions.append("incompatible_clipping_sheets")
        elif self.sheet_pair(left, right):
            score += 0.74
            evidence.append("consecutive_clipping_sheets")
        elif left.clipping and left.clipping.sheet_current is not None:
            score += 0.30
            evidence.append("previous_sheet_number_only")

        for reference in (anchor, left):
            a, b = self.source_key(reference), self.source_key(right)
            if a and b and a != b:
                contradictions.append("different_sources")

            a_date, b_date = reference.source.publication_date, right.source.publication_date
            if a_date and b_date and a_date != b_date:
                contradictions.append("different_publication_dates")

        if not contradictions:
            if self.source_key(anchor) and self.source_key(anchor) == self.source_key(right):
                score += 0.10
                evidence.append("same_source")

            if (anchor.source.publication_date and anchor.source.publication_date == right.source.publication_date):
                score += 0.07
                evidence.append("same_publication_date")

        anchor_page = anchor.source.original_page
        current_page = right.source.original_page
        if anchor_page is not None and current_page is not None:
            if anchor_page == current_page:
                score += 0.04
                evidence.append("same_original_page")
            else:
                evidence.append("original_page_differs")

        if entry:
            score += 0.05
            evidence.append("review_index_anchor")

            # L'indice è un prior, non un metadata da scrivere su page.source.
            if (entry.source and self.source_key(right) and normalize_entity_key(entry.source) == self.source_key(right)):
                evidence.append("index_source_compatible")

        if self.titles(right):
            if self.title_conflict(anchor, right):
                contradictions.append("new_unrelated_article_title")
            else:
                score += 0.08
                evidence.append("repeated_title_fingerprint")

        right_ids = self.article_ids(right)
        if len(right_ids) > 1:
            contradictions.append("multiple_articles_on_continuation_page")
        elif right_ids and article_id not in right_ids and not self.titles(right):
            contradictions.append("conflicting_article_id")

        return round(min(score, 1.0), 4), evidence, sorted(set(contradictions))

    def attach_page(self, page: PageRecord, article_id: str, score: float, evidence: list[str], repeated_title: bool) -> int:
        assigned = 0
        local_ids = self.article_ids(page)
        replaceable_id = next(iter(local_ids)) if repeated_title and len(local_ids) == 1 else None

        for region in page.regions:
            if not self.clustering.eligible(region):
                continue

            legacy_id = region.metadata.get("article_candidate_id")
            if legacy_id and legacy_id != article_id:
                continue

            if region.article_id and region.article_id != article_id:
                if region.article_id != replaceable_id:
                    continue

                region.metadata["article_id_before_newspaper_link"] = region.article_id
                region.metadata["article_clustering_before_newspaper_link"] = (region.metadata.get("article_clustering"))
                region.article_id = None

            if self.clustering.assign(region, article_id, method="newspaper_sheet_v1", score=score, evidence=evidence, anchor_region_id=None):
                region.metadata["newspaper_continuation"] = {
                    "status": "accepted", "score": score, "evidence": list(evidence),
                    "original_article_id": region.metadata.get("article_id_before_newspaper_link"),
                }
                assigned += 1

        return assigned

    def resolve(self, pages: list[PageRecord], entries: list[ReviewIndexEntry] | None = None, matches: list[ReviewIndexMatch] | None = None) -> list[FlowLink]:
        self.decisions = []
        entries_by_id = {entry.id: entry for entry in entries or []}
        matches = matches or []
        links: list[FlowLink] = []
        anchors: dict[tuple[str, str], PageRecord] = {}
        ordered = sorted(pages, key=lambda page: (page.document_id, page.pdf_page))

        for left, right in zip(ordered, ordered[1:]):
            if (left.document_id != right.document_id or right.pdf_page - left.pdf_page != self.config.max_pdf_page_gap or 
                not self.usable(left) or not self.usable(right)):
                continue

            ids = self.previous_article_ids(left)
            if len(ids) != 1:
                continue

            article_id = next(iter(ids))
            anchor = anchors.get((left.document_id, article_id), left)
            entry = self.index_entry(anchor, article_id, entries_by_id, matches)
            score, evidence, contradictions = self.evaluate(left, right, anchor, article_id, entry)

            decision = {
                "document_id": left.document_id,
                "from_pdf_page": left.pdf_page,
                "to_pdf_page": right.pdf_page,
                "article_id": article_id,
                "score": score,
                "evidence": evidence,
                "contradictions": contradictions,
            }

            if contradictions:
                decision["status"] = "rejected"
                self.decisions.append(decision)
                continue

            if score < self.config.min_candidate_score:
                continue

            status = "accepted" if score >= self.config.min_accept_score else "candidate"
            decision["status"] = status
            self.decisions.append(decision)

            left_regions = [region for region in left.regions if region.article_id == article_id]
            right_regions = [region for region in right.regions if self.clustering.eligible(region)]
            link = FlowLink(
                document_id=left.document_id,
                article_candidate_id=article_id,
                from_pdf_page=left.pdf_page,
                to_pdf_page=right.pdf_page,
                status=status,
                method="newspaper_sheet_v1",
                confidence=score,
                evidence=evidence,
                from_region_id=(self.clustering.region_id(left_regions[0]) if left_regions else None),
                to_region_id=(self.clustering.region_id(right_regions[0]) if right_regions else None),
            )
            links.append(link)

            if status == "accepted":
                anchors[(left.document_id, article_id)] = anchor
                self.attach_page(right, article_id, score, evidence, repeated_title=bool(self.titles(right)))

        return links