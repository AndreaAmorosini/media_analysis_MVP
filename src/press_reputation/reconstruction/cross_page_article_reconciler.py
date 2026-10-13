
from __future__ import annotations

import re
from statistics import median

from press_reputation.models.page import (
    PageRecord,
    PageType,
    Region,
    RegionType,
)
from press_reputation.reconstruction.flow_models import FlowLink


class CrossPageArticleReconciler:
    """
    Recupera continuazioni web tra pagine adiacenti.

    È un passaggio conservativo, eseguito dopo il
    clustering locale e prima del reading order.

    Non collega articoli usando soltanto la
    somiglianza del testo o la vicinanza delle pagine.
    """

    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation",
    }

    VISITORS = re.compile(
        r"visitatori\s+unici\s+giornalieri\s*:\s*([\d.,]+)",
        re.IGNORECASE,
    )

    PRINT_ID = re.compile(
        r"\bDW\s*(\d{3,})\b",
        re.IGNORECASE,
    )

    ARTICLE_LINK = re.compile(
        r"vai\s+all['’]articolo\s+https?://",
        re.IGNORECASE,
    )

    @staticmethod
    def region_id(region: Region) -> str | None:
        return region.metadata.get("region_id")

    def editorial_body(
        self,
        page: PageRecord,
    ) -> list[Region]:
        return [
            region
            for region in page.regions
            if (
                region.type == RegionType.ARTICLE_BODY
                and region.text
                and region.bbox
                and not region.exclude_from_article_text
                and region.metadata.get("content_scope")
                not in self.BLOCKED_SCOPES
            )
        ]

    @staticmethod
    def marker(
        page: PageRecord,
        pattern: re.Pattern,
    ) -> str | None:
        for region in page.regions:
            match = pattern.search(region.text or "")

            if match:
                return re.sub(r"\D", "", match.group(1))

        return None

    def has_new_url(self, page: PageRecord) -> bool:
        if page.source.url:
            return True

        return any(
            region.metadata.get("article_url_candidate")
            or self.ARTICLE_LINK.search(region.text or "")
            for region in page.regions
        )

    @staticmethod
    def titles(page: PageRecord) -> list[Region]:
        return [
            region
            for region in page.regions
            if (
                region.type == RegionType.ARTICLE_TITLE
                and region.text
                and region.bbox
                and not region.exclude_from_article_text
            )
        ]

    @staticmethod
    def weak_section_title(
        title: Region,
        target_bodies: list[Region],
        previous: PageRecord,
        previous_bodies: list[Region],
    ) -> bool:
        # Richiede un heading originariamente
        # identificato da Docling.
        if title.raw_label != "section_header":
            return False

        candidate = title.metadata.get("title_candidate") or {}

        # Un titolo collegato a una voce dell'indice
        # non deve essere declassato.
        if candidate.get("index_entry_id"):
            return False

        words = title.text.split()

        if not 2 <= len(words) <= 5:
            return False

        first_body_top = min(
            region.bbox[1]
            for region in target_bodies
        )

        if first_body_top - title.bbox[3] > 60.0:
            return False

        # Un titolo nella pagina successiva può essere
        # un heading interno se la precedente contiene
        # body che prosegue verso il margine inferiore.
        last_body_bottom = max(
            region.bbox[3]
            for region in previous_bodies
        )

        return last_body_bottom >= (
            0.78 * (previous.page_height or 842.0)
        )

    def resolve(
        self,
        pages: list[PageRecord],
        links: list[FlowLink],
    ) -> list[tuple]:

        page_index = {
            (page.document_id, page.pdf_page): page
            for page in pages
        }

        reconciled = []

        for previous in sorted(
            pages,
            key=lambda page: (
                page.document_id,
                page.pdf_page,
            ),
        ):
            current = page_index.get((
                previous.document_id,
                previous.pdf_page + 1,
            ))

            if current is None:
                continue

            if (
                previous.page_type != PageType.WEB
                or current.page_type != PageType.WEB
            ):
                continue

            # Un nuovo URL indica normalmente l'inizio
            # di un articolo diverso.
            if self.has_new_url(current):
                continue

            previous_visitors = self.marker(
                previous, self.VISITORS
            )
            current_visitors = self.marker(
                current, self.VISITORS
            )

            # Prima versione conservativa: richiede
            # una firma condivisa del wrapper web.
            if (
                not previous_visitors
                or previous_visitors != current_visitors
            ):
                continue

            previous_print_id = self.marker(
                previous, self.PRINT_ID
            )
            current_print_id = self.marker(
                current, self.PRINT_ID
            )

            if (
                previous_print_id
                and current_print_id
                and previous_print_id != current_print_id
            ):
                continue

            previous_date = previous.source.publication_date
            current_date = current.source.publication_date

            if (
                previous_date
                and current_date
                and previous_date != current_date
            ):
                continue

            if (
                previous.source.name
                and current.source.name
                and previous.source.name != current.source.name
            ):
                continue

            left = self.editorial_body(previous)
            right = self.editorial_body(current)

            owners = {
                region.article_id
                for region in left
                if region.article_id
            }

            if len(owners) != 1 or not right:
                continue

            owner = next(iter(owners))

            if any(
                region.article_id
                and region.article_id != owner
                for region in left
            ):
                continue

            # Seleziona soltanto il corpo allineato
            # con la colonna editoriale precedente.
            main_left = [
                region
                for region in left
                if region.article_id == owner
            ]

            x = median(
                region.bbox[0]
                for region in main_left
            )

            tolerance = max(
                23.0,
                0.065 * (current.page_width or 595.0),
            )

            right = [
                region
                for region in right
                if abs(region.bbox[0] - x) <= tolerance
            ]

            if not right:
                continue

            recovered_chars = sum(
                len(region.text)
                for region in right
            )

            if recovered_chars < 175:
                continue

            all_target_bodies = self.editorial_body(current)

            total_chars = sum(
                len(region.text)
                for region in all_target_bodies
            )

            # Evita di propagare un'identità a una
            # colonna marginale o a un piccolo box.
            if recovered_chars / total_chars < 0.70:
                continue

            titles = self.titles(current)

            # Se più titoli competono, non riconciliare.
            if len(titles) > 1:
                continue

            # Nessuna modifica se la continuazione è
            # già completamente assegnata.
            if (
                not titles
                and all(
                    region.article_id == owner
                    for region in right
                )
            ):
                continue

            weak_title = None

            if titles:
                if not self.weak_section_title(
                    titles[0],
                    right,
                    previous,
                    main_left,
                ):
                    continue

                weak_title = titles[0]

            right_owners = {
                region.article_id
                for region in right
                if region.article_id
            }

            # Permetti di riassegnare un ID errato
            # soltanto quando il nuovo titolo è stato
            # riconosciuto come possibile section header.
            if right_owners - {owner} and not weak_title:
                continue

            if len(right_owners - {owner}) > 1:
                continue

            if not weak_title and any(
                region.metadata.get("article_candidate_id")
                not in (None, owner)
                for region in right
            ):
                continue

            matching_links = [
                link
                for link in links
                if (
                    link.document_id == previous.document_id
                    and link.from_pdf_page == previous.pdf_page
                    and link.to_pdf_page == current.pdf_page
                    and link.article_candidate_id == owner
                )
            ]

            # Non scavalcare contraddizioni esplicite
            # già riconosciute dal flow resolver.
            if any(
                link.contradictions
                for link in matching_links
            ):
                continue

            # Conservarne l'anchor iniziale anche
            # attraverso più pagine consecutive.
            known_anchor_pages = [
                region.metadata.get("article_anchor_pdf_page")
                for region in main_left
                if isinstance(
                    region.metadata.get("article_anchor_pdf_page"),
                    int,
                )
            ]

            anchor_page = min(
                [previous.pdf_page, *known_anchor_pages]
            )

            # Le verifiche sono concluse: ora è
            # possibile applicare la riconciliazione.
            regions_to_update = list(right)

            if weak_title:
                regions_to_update.append(weak_title)

            for region in regions_to_update:
                region.metadata["cross_page_reconciliation"] = {
                    "previous_article_id": region.article_id,
                    "previous_type": region.type.value,
                    "source_pdf_page": previous.pdf_page,
                    "anchor_pdf_page": anchor_page,
                    "method": "web_wrapper_and_body_lane_v1",
                    "evidence": [
                        "adjacent_web_pages",
                        "same_print_audience",
                        "compatible_body_lane",
                        "no_new_article_url",
                    ],
                }

                if region is weak_title:
                    region.type = RegionType.ARTICLE_SECTION_HEADER
                    region.metadata["title_role"] = "section_header"

                region.article_id = owner

                # Mantieni coerente l'ID usato dai
                # resolver e dal DraftAssembler.
                region.metadata["article_candidate_id"] = owner
                region.metadata["article_anchor_pdf_page"] = anchor_page
                region.metadata["content_scope"] = "main"
                region.metadata["include_in_main_body"] = True

            # Aggiorna un link esistente oppure
            # registra il nuovo collegamento.
            if matching_links:
                link = matching_links[0]
                link.status = "accepted"
                link.method = "cross_page_reconciliation_v1"
                link.confidence = max(
                    link.confidence or 0.0,
                    0.90,
                )
                link.evidence = list(dict.fromkeys([
                    *link.evidence,
                    "cross_page_reconciliation_v1",
                    "same_print_audience",
                    "compatible_body_lane",
                ]))
            else:
                link = FlowLink(
                    document_id=previous.document_id,
                    article_candidate_id=owner,
                    from_pdf_page=previous.pdf_page,
                    to_pdf_page=current.pdf_page,
                    status="accepted",
                    method="cross_page_reconciliation_v1",
                    evidence=[
                        "adjacent_web_pages",
                        "same_print_audience",
                        "compatible_body_lane",
                        "no_new_article_url",
                    ],
                    confidence=0.90,
                )

                links.append(link)

            link.from_region_id = self.region_id(
                max(
                    main_left,
                    key=lambda region: region.bbox[3],
                )
            )

            link.to_region_id = self.region_id(
                min(
                    right,
                    key=lambda region: region.bbox[1],
                )
            )

            reconciled.append((
                previous.pdf_page,
                current.pdf_page,
                owner,
                len(right),
                recovered_chars,
            ))

        return reconciled
