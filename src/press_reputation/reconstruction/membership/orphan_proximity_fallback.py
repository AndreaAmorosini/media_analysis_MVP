from __future__ import annotations

from press_reputation.classification.scopes import is_blocked_scope
from press_reputation.core.config import ArticleClusteringConfig
from press_reputation.features.geometry import horizontal_overlap
from press_reputation.core.models.page import PageRecord, Region, RegionType


class OrphanProximityFallbackResolver:
    """Ultima risorsa per il body orfano su pagine con identità titolo.

    Se un body non attribuito ha, nella sua banda orizzontale e sopra di sé,
    esattamente un titolo candidato con article_id, lo attribuisce a quella
    identità con metodo proximity_fallback_v1. Con più candidati o nessun
    candidato la regione resta orfana. Ogni assegnazione è marcata: il
    finalizer forza lo stato review_required per i record che la usano.
    """

    def __init__(self, config: ArticleClusteringConfig | None = None) -> None:
        self.config = config or ArticleClusteringConfig()

    def eligible_body(self, region: Region) -> bool:
        return (
            region.type == RegionType.ARTICLE_BODY
            and region.article_id is None
            and not region.exclude_from_article_text
            and bool(region.text)
            and region.has_valid_bbox
            and not is_blocked_scope(region)
        )

    def eligible_title(self, region: Region) -> bool:
        return (
            region.type == RegionType.ARTICLE_TITLE
            and bool(region.article_id)
            and region.metadata.get("title_role") not in {"not_main", "ambiguous"}
            and not region.exclude_from_article_text
            and region.has_valid_bbox
        )

    def enrich_page(self, page: PageRecord) -> list[dict]:
        report: list[dict] = []
        titles = [region for region in page.regions if self.eligible_title(region)]
        orphans = [region for region in page.regions if self.eligible_body(region)]
        if not titles or not orphans:
            return report

        for region in orphans:
            qualifying = []
            for title in titles:
                if title.bbox[3] > region.bbox[1]:
                    continue
                if (
                    horizontal_overlap(region.bbox, title.bbox)
                    < self.config.min_horizontal_overlap
                ):
                    continue
                qualifying.append(title)

            if len(qualifying) != 1:
                continue

            title = qualifying[0]
            region.article_id = title.article_id
            region.metadata["article_clustering"] = {
                "status": "assigned",
                "method": "proximity_fallback_v1",
                "score": 0.55,
                "evidence": ["single_title_above", "proximity_fallback"],
                "alternatives": [],
                "anchor_region_id": title.metadata.get("region_id"),
            }
            report.append(
                {
                    "document_id": page.document_id,
                    "pdf_page": page.pdf_page,
                    "region_id": region.metadata.get("region_id"),
                    "article_id": title.article_id,
                    "anchor_region_id": title.metadata.get("region_id"),
                    "chars": len((region.text or "").strip()),
                }
            )

        return report

    def enrich_document(self, pages: list[PageRecord]) -> list[dict]:
        return [
            item
            for page in sorted(
                pages, key=lambda item: (item.document_id, item.pdf_page)
            )
            for item in self.enrich_page(page)
        ]
