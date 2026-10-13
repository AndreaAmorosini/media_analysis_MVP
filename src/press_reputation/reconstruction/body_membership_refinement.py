
from __future__ import annotations

from collections import defaultdict
from math import isfinite

from press_reputation.models.page import (
    PageRecord,
    Region,
    RegionType,
)


class BodyMembershipRefinementResolver:
    """
    Recupera regioni ARTICLE_BODY rimaste senza article_id.

    Utilizza:
    - continuita' geometrica e tipografica
    - article_id dei body gia' assegnati
    - article_candidate_id quando disponibile
    - ArticleZone quando disponibile
    - controllo dei titoli concorrenti

    Non inventa articoli e non collega pagine differenti.
    """

    HARD_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "navigation",
    }

    MIN_SCORE = 0.70
    MIN_MARGIN = 0.14
    MIN_OVERLAP = 0.52
    MAX_GROUP_GAP = 44.0

    @staticmethod
    def box(region: Region) -> list[float] | None:
        b = region.bbox

        if (
            b is None
            or len(b) != 4
            or not all(isfinite(x) for x in b)
        ):
            return None

        if b[2] <= b[0] or b[3] <= b[1]:
            return None

        return b

    @staticmethod
    def overlap(
        a: list[float],
        b: list[float],
    ) -> float:
        intersection = max(
            0.0,
            min(a[2], b[2]) - max(a[0], b[0]),
        )

        width = min(
            a[2] - a[0],
            b[2] - b[0],
        )

        return intersection / width if width > 0 else 0.0

    @staticmethod
    def vertical_gap(
        a: list[float],
        b: list[float],
    ) -> float:
        return max(
            0.0,
            a[1] - b[3],
            b[1] - a[3],
        )

    @staticmethod
    def zone(region: Region) -> str | None:
        zone = region.metadata.get("web_article_zone") or {}
        return zone.get("zone_id")

    @classmethod
    def group_box(
        cls,
        group: list[Region],
    ) -> list[float]:
        boxes = [cls.box(r) for r in group]

        return [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]

    @classmethod
    def eligible(cls, region: Region) -> bool:
        return (
            region.type == RegionType.ARTICLE_BODY
            and bool((region.text or "").strip())
            and cls.box(region) is not None
            and not region.exclude_from_article_text
            and region.metadata.get("content_scope")
            not in cls.HARD_SCOPES
        )

    def groups(
        self,
        regions: list[Region],
    ) -> list[list[Region]]:

        groups: list[list[Region]] = []

        for region in sorted(
            regions,
            key=lambda r: (r.bbox[1], r.bbox[0]),
        ):
            placed = False

            for group in reversed(groups):
                previous = group[-1]

                if (
                    self.zone(region)
                    and self.zone(previous)
                    and self.zone(region) != self.zone(previous)
                ):
                    continue

                left_id = region.metadata.get(
                    "article_candidate_id"
                )
                right_id = previous.metadata.get(
                    "article_candidate_id"
                )

                if (
                    left_id
                    and right_id
                    and left_id != right_id
                ):
                    continue

                if (
                    self.overlap(
                        region.bbox,
                        previous.bbox,
                    ) >= self.MIN_OVERLAP
                    and 0 <= (
                        region.bbox[1] - previous.bbox[3]
                    ) <= self.MAX_GROUP_GAP
                ):
                    group.append(region)
                    placed = True
                    break

            if not placed:
                groups.append([region])

        return groups

    def blocks_by_competing_title(
        self,
        page: PageRecord,
        group_box: list[float],
        ref: Region,
        owner: str,
    ) -> bool:

        start = min(group_box[1], ref.bbox[1])
        end = max(group_box[3], ref.bbox[3])

        for region in page.regions:
            if (
                region.type != RegionType.ARTICLE_TITLE
                or self.box(region) is None
                or region.article_id == owner
                or region.metadata.get("title_role")
                in {"not_main", "ambiguous"}
            ):
                continue

            if (
                start <= region.bbox[1] <= end
                and self.overlap(
                    group_box,
                    region.bbox,
                ) >= 0.30
            ):
                return True

        return False

    def score(
        self,
        page: PageRecord,
        group: list[Region],
        ref: Region,
        owner: str,
    ) -> tuple[float, list[str]] | None:

        bounds = self.group_box(group)

        if self.blocks_by_competing_title(
            page, bounds, ref, owner
        ):
            return None

        candidate_ids = {
            r.metadata.get("article_candidate_id")
            for r in group
            if r.metadata.get("article_candidate_id")
        }

        if (
            len(candidate_ids) > 1
            or (
                candidate_ids
                and owner not in candidate_ids
            )
        ):
            return None

        zones = {
            self.zone(r)
            for r in group
            if self.zone(r)
        }

        ref_zone = self.zone(ref)

        if (
            len(zones) > 1
            or (
                zones
                and ref_zone
                and ref_zone not in zones
            )
        ):
            return None

        same_zone = bool(
            zones and ref_zone in zones
        )

        same_candidate = owner in candidate_ids

        # Un content_scope=non_main preesistente
        # si puo' correggere soltanto con evidenze
        # esplicite di appartenenza.
        if any(
            r.metadata.get("content_scope") == "non_main"
            for r in group
        ):
            if not (same_zone or same_candidate):
                return None

        horizontal = self.overlap(
            bounds,
            ref.bbox,
        )

        max_gap = max(
            56.0,
            min(
                100.0,
                0.13 * (page.page_height or 842.0),
            ),
        )

        gap = self.vertical_gap(
            bounds,
            ref.bbox,
        )

        if (
            horizontal < self.MIN_OVERLAP
            or gap > max_gap
        ):
            return None

        evidence = [
            "horizontal_body_continuity",
            "vertical_body_proximity",
        ]

        score = (
            0.43 * horizontal
            + 0.29 * (1.0 - gap / max_gap)
        )

        if same_zone:
            score += 0.20
            evidence.append("same_article_zone")

        if same_candidate:
            score += 0.20
            evidence.append("same_article_candidate")

        font = ref.style.get("median_font_size")

        group_fonts = [
            r.style.get("median_font_size")
            for r in group
        ]

        if (
            isinstance(font, (int, float))
            and font > 0
            and group_fonts
            and all(
                isinstance(f, (int, float))
                and f > 0
                and abs(f - font) / max(f, font) <= 0.25
                for f in group_fonts
            )
        ):
            score += 0.10
            evidence.append("compatible_font_size")

        return (
            min(1.0, round(score, 4)),
            evidence,
        )
        
    
    def bootstrap_from_title(self, page: PageRecord) -> None:
        """
        Inizializza un body seed usando un unico titolo
        principale gia' associato a un article_id.

        Non agisce quando esistono piu' titoli concorrenti
        o quando esistono gia' body assegnati.
        """

        if any(
            self.eligible(r) and r.article_id
            for r in page.regions
        ):
            return

        titles = [
            r for r in page.regions
            if (
                r.type == RegionType.ARTICLE_TITLE
                and r.article_id
                and self.box(r) is not None
                and r.metadata.get("title_role") == "main"
                and not r.exclude_from_article_text
            )
        ]

        # Per ora gestiamo soltanto identita' non ambigue.
        if len(titles) != 1:
            return

        title = titles[0]
        owner = title.article_id

        orphans = [
            r for r in page.regions
            if (
                self.eligible(r)
                and not r.article_id
                and r.metadata.get("content_scope") != "non_main"
                and (
                    not r.metadata.get("article_candidate_id")
                    or r.metadata["article_candidate_id"] == owner
                )
            )
        ]

        if not orphans:
            return

        max_gap = min(
            320.0,
            0.40 * (page.page_height or 842.0),
        )

        candidates = []

        for group in self.groups(orphans):
            bounds = self.group_box(group)

            gap = bounds[1] - title.bbox[3]
            horizontal = self.overlap(bounds, title.bbox)

            if not (-4.0 <= gap <= max_gap):
                continue

            if horizontal < 0.35:
                continue

            if self.blocks_by_competing_title(
                page, bounds, title, owner
            ):
                continue

            score = (
                0.40 * horizontal
                + 0.24 * (
                    1.0 - max(0.0, gap) / max_gap
                )
            )

            evidence = [
                "single_main_title",
                "header_body_geometry",
            ]

            if any(
                (r.metadata.get("body_seed_evaluation") or {})
                .get("accepted") is True
                for r in group
            ):
                score += 0.12
                evidence.append("accepted_body_seed")

            page_width = page.page_width or 595.0

            if (
                abs(bounds[0] - title.bbox[0])
                <= 0.12 * page_width
            ):
                score += 0.14
                evidence.append("aligned_left_edge")

            title_zone = self.zone(title)

            if (
                title_zone
                and all(self.zone(r) == title_zone for r in group)
            ):
                score += 0.10
                evidence.append("same_article_zone")

            if (
                (title.metadata.get("title_candidate") or {})
                .get("index_entry_id")
            ):
                score += 0.08
                evidence.append("review_index_title")

            candidates.append(
                (min(score, 1.0), group, evidence)
            )

        candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        if not candidates:
            return

        best_score, best_group, evidence = candidates[0]
        second_score = (
            candidates[1][0]
            if len(candidates) > 1
            else 0.0
        )

        # Non inizializzare un seed quando due gruppi
        # sono ugualmente plausibili.
        if (
            best_score < 0.64
            or (
                len(candidates) > 1
                and best_score - second_score < 0.12
            )
        ):
            return

        for region in best_group:
            if region.article_id:
                continue

            region.article_id = owner

            region.metadata["article_clustering"] = {
                "status": "assigned",
                "method": "anchor_body_bootstrap_v1",
                "score": round(best_score, 4),
                "evidence": evidence,
                "alternatives": [],
                "anchor_region_id": title.metadata.get(
                    "region_id"
                ),
            }

            region.metadata["anchor_body_bootstrap"] = {
                "status": "assigned",
                "score": round(best_score, 4),
                "title_region_id": title.metadata.get(
                    "region_id"
                ),
                "previous_content_scope": (
                    region.metadata.get("content_scope")
                ),
            }

            region.metadata["content_scope"] = "main"
            region.metadata[
                "content_scope_reason"
            ] = "anchor_body_bootstrap"

            region.metadata[
                "include_in_main_body"
            ] = True


    def enrich_page(self, page: PageRecord) -> None:
        
        self.bootstrap_from_title(page)

        orphan = [
            r for r in page.regions
            if self.eligible(r) and not r.article_id
        ]

        if not orphan:
            return

        seeds = [
            r for r in page.regions
            if self.eligible(r) and r.article_id
        ]

        seeds_by_id: dict[str, list[Region]] = defaultdict(list)

        for seed in seeds:
            seeds_by_id[seed.article_id].append(seed)

        for group in self.groups(orphan):

            ranked = []

            for owner, references in seeds_by_id.items():
                candidates = [
                    self.score(page, group, ref, owner)
                    for ref in references
                ]

                candidates = [
                    c for c in candidates
                    if c is not None
                ]

                if candidates:
                    score, evidence = max(
                        candidates,
                        key=lambda item: item[0],
                    )

                    ranked.append(
                        (score, owner, evidence)
                    )

            ranked.sort(reverse=True)

            accepted = bool(
                ranked
                and ranked[0][0] >= self.MIN_SCORE
                and (
                    len(ranked) == 1
                    or ranked[0][0] - ranked[1][0]
                    >= self.MIN_MARGIN
                )
            )

            for region in group:

                region.metadata[
                    "body_membership_refinement"
                ] = {
                    "status": (
                        "assigned" if accepted
                        else "unresolved"
                    ),
                    "reason": (
                        "unique_local_body_owner"
                        if accepted
                        else "no_assigned_body_seed"
                        if not seeds_by_id
                        else "ambiguous_or_insufficient_evidence"
                    ),
                    "candidates": [
                        {
                            "article_id": aid,
                            "score": score,
                        }
                        for score, aid, _ in ranked[:3]
                    ],
                }

                if not accepted:
                    continue

                score, owner, evidence = ranked[0]

                previous = region.metadata.get(
                    "article_clustering"
                )

                if previous:
                    region.metadata.setdefault(
                        "article_clustering_attempts", []
                    ).append(previous)

                region.article_id = owner

                region.metadata["article_clustering"] = {
                    "status": "assigned",
                    "method": "body_membership_refinement_v1",
                    "score": score,
                    "evidence": evidence,
                    "alternatives": [],
                    "anchor_region_id": None,
                }

                old_scope = region.metadata.get(
                    "content_scope"
                )

                region.metadata[
                    "body_membership_refinement"
                ]["previous_content_scope"] = old_scope

                region.metadata["content_scope"] = "main"

                region.metadata[
                    "content_scope_reason"
                ] = "recovered_local_body_membership"

                region.metadata[
                    "include_in_main_body"
                ] = True

            if accepted:
                seeds_by_id[ranked[0][1]].extend(group)

    def enrich_document(
        self,
        pages: list[PageRecord],
    ) -> None:
        for page in pages:
            self.enrich_page(page)
