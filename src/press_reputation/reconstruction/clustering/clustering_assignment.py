

from press_reputation.features.geometry import horizontal_overlap, vertical_gap
from press_reputation.core.models.page import PageRecord, PageType, Region, RegionType
from press_reputation.reconstruction.clustering.clustering_columns import ColumnClustering
from press_reputation.reconstruction.clustering.clustering_core import ClusteringCore
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch


class AssignmentClustering:
    def __init__(self, core: ClusteringCore, columns: ColumnClustering) -> None:
        self.core = core
        self.columns = columns
        
    def choose(
        self,
        region: Region,
        scored: list[tuple[float, str, list[str], str | None]],
        *,
        method: str,
    ) -> bool:
        ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
        if not ranked:
            return False

        best_score, best_id, evidence, anchor_id = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0

        if (
            best_score < self.core.config.min_assignment_score
            or best_score - second_score < self.core.config.min_winning_margin
        ):
            self.core.record(
                region,
                status="ambiguous",
                method=method,
                alternatives=[
                    {
                        "article_id": article_id,
                        "score": round(score, 4),
                        "evidence": reasons,
                    }
                    for score, article_id, reasons, _ in ranked[:3]
                ],
            )

            return False

        return self.core.assign(
            region,
            best_id,
            method=method,
            score=best_score,
            evidence=evidence,
            anchor_region_id=anchor_id,
        )

    def assign_index_prior_bodies(
        self,
        page: PageRecord,
        anchors: dict[str, Region],
        prior_entries: dict[tuple[str, str | None], ReviewIndexEntry],
    ) -> None:
        """Attribuisce body orfani usando il prior del Review Index.

        Un titolo con match d'indice valido diventa owner di default del body
        nella sua banda orizzontale, dalla pagina di partenza in avanti,
        purché l'original page non contraddica. Ogni assegnazione è marcata
        index_prior_v1 con score ed evidence conservati.
        """
        candidates = []
        for article_id, title in anchors.items():
            entry = prior_entries.get((page.document_id, self.core.region_id(title)))
            if entry is not None:
                candidates.append((article_id, title, entry))

        if not candidates:
            return

        for region in page.regions:
            if (
                region.type != RegionType.ARTICLE_BODY
                or region.article_id is not None
                or not self.core.eligible(region)
                or not region.has_valid_bbox
            ):
                continue

            qualifying = []
            for article_id, title, entry in candidates:
                if (
                    entry.review_start_page is not None
                    and page.pdf_page < entry.review_start_page
                ):
                    continue
                if (
                    entry.original_page is not None
                    and page.source.original_page is not None
                    and entry.original_page != page.source.original_page
                ):
                    continue
                if not title.has_valid_bbox or region.bbox[1] < title.bbox[1]:
                    continue
                if (
                    horizontal_overlap(region.bbox, title.bbox)
                    < self.core.config.index_prior_min_horizontal_overlap
                ):
                    continue
                if self.core.title_barrier(page, title, region):
                    continue
                qualifying.append((article_id, title))

            if len(qualifying) == 1:
                article_id, title = qualifying[0]
                self.core.assign(
                    region,
                    article_id,
                    method="index_prior_v1",
                    score=self.core.config.index_prior_score,
                    evidence=["review_index_prior", "start_page_constraint"],
                    anchor_region_id=self.core.region_id(title),
                )
            elif len(qualifying) > 1:
                self.core.record(
                    region,
                    status="ambiguous",
                    method="index_prior_v1",
                    evidence=["multiple_index_prior_owners"],
                )

    def assign_local(
        self,
        pages: list[PageRecord],
        index_matches: list[ReviewIndexMatch],
        entries: list[ReviewIndexEntry] | None = None,
    ) -> list[PageRecord]:
        entry_by_id = {entry.id: entry for entry in (entries or [])}
        prior_entries: dict[tuple[str, str | None], ReviewIndexEntry] = {}
        for match in index_matches:
            if match.status != "matched":
                continue
            entry = entry_by_id.get(match.entry_id)
            if entry is not None and match.title_region_id:
                prior_entries.setdefault(
                    (entry.document_id, match.title_region_id), entry
                )
        matched_titles = {
            (match.pdf_page, match.title_region_id)
            for match in index_matches
            if (
                match.status == "matched"
                and match.title_similarity is not None
                and match.title_similarity >= self.core.config.min_bootstrap_index_similarity
            )
        }

        for page in pages:
            titles = [
                region
                for region in page.regions
                if (
                    region.type == RegionType.ARTICLE_TITLE
                    and region.has_valid_bbox
                    and self.core.eligible(region)
                    and region.metadata.get("title_role")
                    not in {"not_main", "ambiguous"}
                    and
                    # Una riga-link non è un headline: l'URL dell'articolo
                    # stampato non apre una nuova identità.
                    not region.metadata.get("article_url_candidate")
                    and not (region.text or "")
                    .strip()
                    .lower()
                    .startswith(("http://", "https://", "www."))
                )
            ]

            anchors: dict[str, Region] = {}
            for title in titles:
                # Titolo ripetuto nello stesso printout (layout di stampa che
                # replica l'headline a meta' colonna): stessa identita', non
                # un nuovo articolo.
                duplicate_of = next(
                    (
                        other_id
                        for other_id, other_title in anchors.items()
                        if (
                            other_title is not title
                            and self.core._same_headline(title.text, other_title.text)
                            and horizontal_overlap(title.bbox, other_title.bbox)
                            >= self.core.config.min_horizontal_overlap
                        )
                    ),
                    None,
                )
                if duplicate_of is not None:
                    self.core.assign(
                        title,
                        duplicate_of,
                        method="duplicate_headline_v1",
                        score=1.0,
                        evidence=["repeated_headline_same_page"],
                        anchor_region_id=self.core.region_id(anchors[duplicate_of]),
                    )
                    continue

                article_id = title.metadata.get(
                    "article_candidate_id"
                ) or self.core.title_id(page, title)
                if not article_id:
                    self.core.record(
                        title,
                        status="unresolved",
                        method="title_anchor_v1",
                        evidence=["missing_stable_region_id"],
                    )
                    continue

                if self.core.assign(
                    title,
                    article_id,
                    method="title_anchor_v1",
                    score=1.0,
                    evidence=["article_title"],
                    anchor_region_id=(self.core.region_id(title)),
                ):
                    anchors[article_id] = title

            title_by_region_id = {
                self.core.region_id(title): title
                for title in titles
                if self.core.region_id(title)
            }

            for region in page.regions:
                if region.type != RegionType.ARTICLE_SUBTITLE or not self.core.eligible(
                    region
                ):
                    continue

                parent_id = region.metadata.get("subtitle_resolution", {}).get(
                    "title_region_id"
                )
                parent = title_by_region_id.get(parent_id)
                if parent and parent.article_id:
                    self.core.assign(
                        region,
                        parent.article_id,
                        method="subtitle_anchor_v1",
                        score=1.0,
                        evidence=["resolved_title_reference"],
                        anchor_region_id=parent_id,
                    )

            for region in page.regions:
                if region.type not in {RegionType.ARTICLE_KICKER, RegionType.LOCATION}:
                    continue

                header = region.metadata.get("editorial_header_resolution") or {}
                parent = title_by_region_id.get(header.get("title_region_id"))
                if not parent or not parent.article_id:
                    continue

                if (
                    region.type == RegionType.LOCATION
                    and region.metadata.get("location_role") != "dateline"
                ):
                    continue

                self.core.assign(
                    region,
                    parent.article_id,
                    method="editorial_header_anchor_v1",
                    score=0.95,
                    evidence=[header.get("method", "resolved_title_reference")],
                    anchor_region_id=self.core.region_id(parent),
                )

            header_by_region_id = {
                self.core.region_id(region): region
                for region in page.regions
                if (
                    region.type
                    in {RegionType.ARTICLE_TITLE, RegionType.ARTICLE_SUBTITLE}
                    and self.core.region_id(region)
                )
            }
            for region in page.regions:
                if region.type != RegionType.AUTHOR or not self.core.eligible(region):
                    continue

                parent_id = region.metadata.get("author_resolution", {}).get(
                    "header_region_id"
                )
                parent = header_by_region_id.get(parent_id)
                if parent and parent.article_id:
                    self.core.assign(
                        region,
                        parent.article_id,
                        method="author_anchor_v1",
                        score=1.0,
                        evidence=["resolved_header_reference"],
                        anchor_region_id=parent_id,
                    )

            # I body marcati dal vecchio resolver web
            # ereditano la sua identità senza assegnare
            # tutte le regioni della pagina.
            for region in page.regions:
                web_id = region.metadata.get("article_candidate_id")
                if (
                    page.page_type == PageType.WEB
                    and web_id
                    and region.type == RegionType.ARTICLE_BODY
                    and self.core.eligible(region)
                    and region.metadata.get("content_scope") == "main"
                    and (not anchors or web_id in anchors)
                ):
                    self.core.assign(
                        region,
                        web_id,
                        method="web_chain_v1",
                        score=0.95,
                        evidence=["existing_web_chain", "main_content_scope"],
                        anchor_region_id=(
                            self.core.region_id(anchors[web_id])
                            if web_id in anchors
                            else None
                        ),
                    )

            bodies = [
                region
                for region in page.regions
                if (
                    region.type == RegionType.ARTICLE_BODY
                    and self.core.eligible(region)
                    and region.article_id is None
                    and region.has_valid_bbox
                )
            ]
            bodies.sort(key=lambda region: (region.bbox[1], region.bbox[0]))

            # Ogni body assegnato diventa riferimento
            # per i successivi, ma soltanto se supera
            # soglia e margine.
            for body in bodies:
                scored = []
                for article_id, title in anchors.items():
                    same_article_bodies = [
                        other
                        for other in page.regions
                        if (
                            other is not body
                            and other.type == RegionType.ARTICLE_BODY
                            and other.article_id == article_id
                            and self.core.eligible(other)
                        )
                    ]

                    score, reasons = self.core.score_body(
                        body,
                        title,
                        same_article_bodies,
                        page,
                        article_id,
                        (page.pdf_page, self.core.region_id(title)) in matched_titles,
                    )

                    if score:
                        scored.append(
                            (score, article_id, reasons, self.core.region_id(title))
                        )

                self.choose(body, scored, method="local_body_v1")

            self.columns.bootstrap_membership(page, anchors, matched_titles)

            self.columns.assign_multicolumn(page, matched_titles)

            self.assign_index_prior_bodies(page, anchors, prior_entries)

            # Un frammento già attribuito a una
            # continuazione web senza titolo su questa
            # pagina resta nella stessa catena.
            if not titles:
                for region in page.regions:
                    web_id = region.metadata.get("article_candidate_id")
                    if (
                        page.page_type == PageType.WEB
                        and web_id
                        and region.type in {RegionType.ARTICLE_BODY, RegionType.UNKNOWN}
                        and self.core.eligible(region)
                        and region.article_id is None
                        and region.metadata.get("content_scope") == "main"
                    ):
                        self.core.assign(
                            region,
                            web_id,
                            method=("web_continuation_v1"),
                            score=0.90,
                            evidence=["existing_web_chain"],
                            anchor_region_id=None,
                        )

        return pages
    
    def assign_recovered_body(self, page: PageRecord) -> PageRecord:
        # BodyContinuationResolver assegna l'ID durante UNKNOWN → BODY.
        # Si annotano i body che rimangono senza identità.
        for region in page.regions:
            if (
                region.type == RegionType.ARTICLE_BODY
                and region.article_id is None
                and self.core.eligible(region)
            ):
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
        by_region_id = {
            self.core.region_id(region): region
            for region in page.regions
            if self.core.region_id(region)
        }

        for region in page.regions:
            if (
                region.type != RegionType.ARTICLE_SECTION_HEADER
                or region.article_id
                or not self.core.eligible(region)
            ):
                continue

            resolution = region.metadata.get("section_header_resolution", {})
            before = by_region_id.get(resolution.get("previous_body_region_id"))
            after = by_region_id.get(resolution.get("following_body_region_id"))

            if (
                before
                and after
                and before.article_id
                and before.article_id == after.article_id
            ):
                self.core.assign(
                    region,
                    before.article_id,
                    method="body_sandwich_v1",
                    score=0.95,
                    evidence=["same_article_before_after"],
                    anchor_region_id=self.core.region_id(before),
                )

        assigned_bodies = [
            region
            for region in page.regions
            if (
                region.type == RegionType.ARTICLE_BODY
                and region.article_id
                and region.has_valid_bbox
                and self.core.eligible(region)
            )
        ]

        for media in page.regions:
            if (
                media.type not in self.core.MEDIA_TYPES
                or media.article_id
                or not media.has_valid_bbox
                or media.metadata.get("exclude_from_article_media")
            ):
                continue

            scored = []

            for body in assigned_bodies:
                gap = (
                    vertical_gap(media.bbox, body.bbox) / page.page_height
                    if page.page_height
                    else 1.0
                )
                overlap = horizontal_overlap(media.bbox, body.bbox)

                if (
                    gap > self.core.config.max_media_gap_fraction
                    or overlap < self.core.config.min_horizontal_overlap
                ):
                    continue

                score = 0.40 * overlap + 0.40 * (
                    1 - gap / self.core.config.max_media_gap_fraction
                )
                scored.append(
                    (
                        score,
                        body.article_id,
                        ["near_assigned_body", "media_body_overlap"],
                        self.core.region_id(body),
                    )
                )

            # Due body dello stesso articolo non sono concorrenti distinti.
            best_by_article = {}
            for item in scored:
                article_id = item[1]
                if (
                    article_id not in best_by_article
                    or item[0] > best_by_article[article_id][0]
                ):
                    best_by_article[article_id] = item

            self.choose(media, list(best_by_article.values()), method="local_media_v1")

        assigned_media = [
            region
            for region in page.regions
            if (
                region.type in self.core.MEDIA_TYPES
                and region.article_id
                and region.has_valid_bbox
            )
        ]

        for caption in page.regions:
            if (
                caption.type != RegionType.CAPTION
                or caption.article_id
                or not caption.has_valid_bbox
                or not self.core.eligible(caption)
            ):
                continue

            scored = []

            for media in assigned_media:
                gap = (
                    vertical_gap(caption.bbox, media.bbox) / page.page_height
                    if page.page_height
                    else 1.0
                )
                if gap > self.core.config.max_caption_image_gap_fraction:
                    continue

                overlap = horizontal_overlap(caption.bbox, media.bbox)
                if overlap < self.core.config.min_horizontal_overlap:
                    continue

                score = 0.45 * overlap + 0.45 * (
                    1 - gap / self.core.config.max_caption_image_gap_fraction
                )
                scored.append(
                    (
                        score,
                        media.article_id,
                        ["near_assigned_media"],
                        self.core.region_id(media),
                    )
                )

            best_by_article = {}
            for item in scored:
                article_id = item[1]
                if (
                    article_id not in best_by_article
                    or item[0] > best_by_article[article_id][0]
                ):
                    best_by_article[article_id] = item

            self.choose(
                caption, list(best_by_article.values()), method="caption_media_v1"
            )

        return page
    
    def assign_unassigned_body_in_owned_columns(self, page: PageRecord) -> PageRecord:
        for column in self.columns.body_columns(page):
            owners = self.columns.column_article_ids(column)
            if len(owners) != 1:
                continue

            article_id = next(iter(owners))
            assigned = [region for region in column if region.article_id == article_id]
            pending = [region for region in column if region.article_id is None]

            for candidate in sorted(pending, key=lambda region: region.bbox[1]):
                references = [
                    reference
                    for reference in assigned
                    if horizontal_overlap(candidate.bbox, reference.bbox)
                    >= self.core.config.min_same_column_body_overlap
                    and abs(candidate.bbox[0] - reference.bbox[0])
                    <= self.core.config.column_left_tolerance
                    and vertical_gap(candidate.bbox, reference.bbox)
                    <= self.core.config.max_same_column_body_gap
                ]

                if not references:
                    continue

                reference = min(
                    references,
                    key=lambda region: vertical_gap(candidate.bbox, region.bbox),
                )
                if self.core.title_barrier(page, reference, candidate):
                    continue

                previous = candidate.metadata.get("article_clustering")
                if previous and previous.get("status") in {"ambiguous", "unresolved"}:
                    candidate.metadata.setdefault(
                        "article_clustering_attempts", []
                    ).append(previous)

                if self.core.assign(
                    candidate,
                    article_id,
                    method="owned_column_body_v1",
                    score=0.90,
                    evidence=["same_owned_column", "local_body_continuity"],
                    anchor_region_id=self.core.region_id(reference),
                ):
                    assigned.append(candidate)

        return page

    def recover_column_continuations(self, page: PageRecord) -> PageRecord:
        matched_titles = {
            (page.pdf_page, self.core.region_id(region))
            for region in page.regions
            if region.type == RegionType.ARTICLE_TITLE
            and region.metadata.get("review_index_prior", {}).get(
                "title_similarity", 0.0
            )
            >= self.core.config.min_bootstrap_index_similarity
        }
        return self.columns.assign_multicolumn(page, matched_titles)


