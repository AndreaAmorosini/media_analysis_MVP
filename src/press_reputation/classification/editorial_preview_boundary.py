
from __future__ import annotations

import re

from press_reputation.models.page import (
    PageRecord,
    Region,
    RegionType,
)
from press_reputation.review_index.matcher import title_similarity


class EditorialPreviewBoundaryResolver:
    """
    Distingue un richiamo editoriale dall'articolo
    completo quando entrambi si trovano nella pagina.

    Funziona in modo conservativo:
    - richiede due headline correlate;
    - richiede un rimando editoriale nel teaser;
    - richiede body narrativo dopo la seconda headline;
    - non modifica i casi ambigui.
    """

    PAGE_REFERENCE = re.compile(
        r"\b(?:a|alla|alle)\s+pag(?:ina)?\.?\s*\d{1,3}\b",
        re.IGNORECASE,
    )

    WATERMARK = re.compile(
        r"\bdata\s+stampa\s*\d+\b",
        re.IGNORECASE,
    )

    MIN_TITLE_SIMILARITY = 0.60
    MAX_BODY_START_GAP = 140.0
    MIN_WINNING_MARGIN = 0.10

    @classmethod
    def cleaned_headline(cls, value: str) -> str:
        return " ".join(
            cls.WATERMARK.sub(" ", value).split()
        )

    @staticmethod
    def valid(region: Region) -> bool:
        return (
            bool((region.text or "").strip())
            and region.bbox is not None
            and len(region.bbox) == 4
            and not region.exclude_from_article_text
        )

    def resolve(self, pages: list[PageRecord]) -> None:
        for page in pages:
            self.enrich_page(page)

    def enrich_page(self, page: PageRecord) -> None:
        titles = [
            r for r in page.regions
            if r.type == RegionType.ARTICLE_TITLE
            and self.valid(r)
        ]

        bodies = [
            r for r in page.regions
            if r.type == RegionType.ARTICLE_BODY
            and self.valid(r)
        ]

        proposals = []

        for teaser_title in titles:
            for main_title in page.regions:
                if main_title is teaser_title:
                    continue

                if main_title.type not in {
                    RegionType.UNKNOWN,
                    RegionType.ARTICLE_SECTION_HEADER,
                }:
                    continue

                if (
                    main_title.raw_label != "section_header"
                    or not self.valid(main_title)
                ):
                    continue

                if (
                    main_title.bbox[1]
                    <= teaser_title.bbox[3]
                ):
                    continue

                similarity = title_similarity(
                    self.cleaned_headline(
                        teaser_title.text or ""
                    ),
                    self.cleaned_headline(
                        main_title.text or ""
                    ),
                )

                if similarity < self.MIN_TITLE_SIMILARITY:
                    continue

                # Il teaser deve contenere un rimando
                # esplicito alla pagina dell'articolo.
                teaser_bodies = [
                    r for r in bodies
                    if (
                        teaser_title.bbox[3] - 5.0
                        <= r.bbox[1]
                        < main_title.bbox[1]
                        and self.PAGE_REFERENCE.search(
                            r.text or ""
                        )
                    )
                ]

                if not teaser_bodies:
                    continue

                # Deve esistere un body autonomo dopo
                # la seconda headline.
                full_article_bodies = [
                    r for r in bodies
                    if (
                        0.0
                        <= r.bbox[1] - main_title.bbox[3]
                        <= self.MAX_BODY_START_GAP
                        and (
                            r.metadata.get(
                                "body_seed_evaluation"
                            ) or {}
                        ).get("accepted") is True
                    )
                ]

                if not full_article_bodies:
                    continue

                proposals.append((
                    similarity,
                    teaser_title,
                    main_title,
                    teaser_bodies,
                ))

        if not proposals:
            return

        proposals.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        # Una scelta ambigua non deve modificare
        # la struttura editoriale.
        if (
            len(proposals) > 1
            and proposals[0][0] - proposals[1][0]
            < self.MIN_WINNING_MARGIN
        ):
            return

        similarity, teaser, main, teaser_bodies = proposals[0]

        teaser_id = teaser.metadata.get("region_id")
        main_id = main.metadata.get("region_id")

        # Il titolo superiore diventa un richiamo,
        # non un nuovo articolo principale.
        teaser.metadata["editorial_preview"] = {
            "role": "teaser_title",
            "target_region_id": main_id,
            "title_similarity": round(similarity, 4),
            "previous_type": teaser.type.value,
        }

        teaser.metadata["title_role"] = "teaser"
        teaser.metadata["content_scope"] = "non_main"
        teaser.metadata["include_in_main_body"] = False

        teaser.type = RegionType.UNKNOWN
        teaser.exclude_from_article_text = True

        # Il corpo del richiamo viene conservato
        # come testo originale, ma non inserito
        # nella narrazione principale.
        for body in teaser_bodies:
            body.metadata["editorial_preview"] = {
                "role": "teaser_body",
                "target_region_id": main_id,
            }

            body.metadata["content_scope"] = "non_main"
            body.metadata["include_in_main_body"] = False
            body.exclude_from_article_text = True

        # Eventuali sottotitoli gia' associati
        # al richiamo rimangono fuori dal body.
        for region in page.regions:
            subtitle = (
                region.metadata.get("subtitle_resolution")
                or {}
            )

            if (
                region.type == RegionType.ARTICLE_SUBTITLE
                and subtitle.get("title_region_id") == teaser_id
            ):
                region.metadata["editorial_preview"] = {
                    "role": "teaser_subtitle",
                    "target_region_id": main_id,
                }

                region.metadata["content_scope"] = "non_main"
                region.metadata["include_in_main_body"] = False
                region.exclude_from_article_text = True

        # La headline inferiore diventa il nuovo
        # titolo principale candidato.
        previous_type = main.type.value

        candidate = main.metadata.get("title_candidate") or {}

        main.metadata["type_before_title_resolution"] = (
            previous_type
        )

        main.metadata["title_candidate"] = {
            **candidate,
            "score": max(
                0.60,
                float(candidate.get("score") or 0.0),
            ),
            "method": "editorial_preview_main_v1",
        }

        main.metadata["editorial_preview"] = {
            "role": "main_title",
            "linked_teaser_region_id": teaser_id,
            "title_similarity": round(similarity, 4),
        }

        main.metadata["title_role"] = "candidate"
        main.type = RegionType.ARTICLE_TITLE
