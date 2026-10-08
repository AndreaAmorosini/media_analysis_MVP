import hashlib
import re
import unicodedata
from dataclasses import dataclass
from math import isfinite
from statistics import median
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from press_reputation.models.page import PageRecord, PageType, Region, RegionType


class WebContinuationConfig(BaseModel):
    column_edge_tolerance: float = Field(default=0.04, gt=0, lt=1)

    # Fascia per cercare un'identità testuale della testata.
    header_max_y: float = Field(default=0.12, gt=0, lt=1)
    header_center_min_x: float = Field(default=0.25, ge=0, lt=1)
    header_center_max_x: float = Field(default=0.75, gt=0, le=1)

    local_neighbor_gap: float = Field(default=0.045, gt=0, lt=1)
    minimum_horizontal_overlap: float = Field(default=0.65, gt=0, le=1)
    fragment_min_words: int = Field(default=5, ge=1)

    # Limita l'accumulo di collegamenti solo euristici.
    max_candidate_pages: int = Field(default=6, ge=1)


@dataclass
class Chain:
    candidate_id: str
    anchor: PageRecord
    last: PageRecord
    count: int = 1


def normalize_name(text: str | None) -> str:
    value = unicodedata.normalize("NFKD", (text or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return "".join(c for c in value if c.isalnum())


def url_key(url: str | None) -> str | None:
    if not url:
        return None

    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


def valid_box(region: Region) -> bool:
    box = region.bbox
    return (
        box is not None
        and len(box) == 4
        and all(isfinite(value) for value in box)
        and box[2] > box[0]
        and box[3] > box[1]
    )


def box_on_page(region: Region, page: PageRecord) -> tuple[float, ...]:
    x0, y0, x1, y1 = region.bbox
    return (
        x0 / page.page_width,
        y0 / page.page_height,
        x1 / page.page_width,
        y1 / page.page_height,
    )


def horizontal_overlap(a: tuple, b: tuple) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    base = min(a[2] - a[0], b[2] - b[0])
    return overlap / base if base > 0 else 0.0


class WebArticleContinuationResolver:
    BLOCKED_SCOPES = {
        "related",
        "advertisement",
        "boilerplate",
        "non_main",
        "navigation"
    }

    OWNED_KEYS = {
        "article_candidate_id",
        "article_anchor_pdf_page",
        "continuation_status",
        "continuation_from_pdf_page",
        "continuation_evidence",
        "continuation_local_role",
    }

    def __init__(self, config: WebContinuationConfig | None = None) -> None:
        self.config = config or WebContinuationConfig()

    def enrich_document(self, pages: list[PageRecord]) -> list[PageRecord]:
        # Rimuove esclusivamente i risultati prodotti da questo componente.
        for page in pages:
            for region in page.regions:
                for key in self.OWNED_KEYS:
                    region.metadata.pop(key, None)

        chains: dict[str, Chain] = {}

        for page in sorted(
            pages,
            key=lambda item: (item.document_id, item.pdf_page),
        ):
            if not self.usable_page(page):
                chains.pop(page.document_id, None)
                continue

            bodies = self.body_candidates(page)
            titles = [
                region
                for region in page.regions
                if region.type == RegionType.ARTICLE_TITLE
                and self.eligible(region)
            ]

            # Un nuovo titolo avvia una nuova catena, non eredita quella prima.
            if titles:
                chains.pop(page.document_id, None)

                if len(titles) != 1 or not bodies:
                    continue

                if not any(
                    region.metadata.get("content_scope") == "main"
                    for region in bodies
                ):
                    continue

                chain = Chain(
                    candidate_id=self.make_id(page),
                    anchor=page,
                    last=page,
                )
                self.annotate(
                    page,
                    bodies,
                    chain,
                    previous_page=None,
                    evidence=["local_title_and_main_body"],
                )
                chains[page.document_id] = chain
                continue

            chain = chains.get(page.document_id)

            if chain is None or not bodies:
                chains.pop(page.document_id, None)
                continue

            evidence = self.connection_evidence(chain, page, bodies)

            if evidence is None:
                chains.pop(page.document_id, None)
                continue

            self.annotate(
                page,
                bodies,
                chain,
                previous_page=chain.last.pdf_page,
                evidence=evidence,
            )
            chain.last = page
            chain.count += 1

        return pages

    @staticmethod
    def usable_page(page: PageRecord) -> bool:
        return (
            page.page_type == PageType.WEB
            and page.page_width is not None
            and page.page_height is not None
            and isfinite(page.page_width)
            and isfinite(page.page_height)
            and page.page_width > 0
            and page.page_height > 0
        )

    def eligible(self, region: Region) -> bool:
        return (
            valid_box(region)
            and not region.exclude_from_article_text
            and not region.metadata.get("inside_article_position_thumbnail")
            and region.metadata.get("content_scope") not in self.BLOCKED_SCOPES
        )

    def body_candidates(self, page: PageRecord) -> list[Region]:
        return [
            region
            for region in page.regions
            if region.type == RegionType.ARTICLE_BODY
            and region.text
            and self.eligible(region)
            and (
                region.metadata.get("content_scope") == "main"
                or region.metadata.get("main_content_candidate") is True
            )
        ]

    @staticmethod
    def make_id(page: PageRecord) -> str:
        raw = f"{page.document_id}\0{page.pdf_page}".encode()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"web_{digest}"

    def header_names(self, page: PageRecord) -> set[str]:
        """
        Estrae una firma testuale della testata, non un nome canonico.

        Non usa provider, date e header tecnici come identità della fonte.
        """
        candidates: set[str] = set()

        for region in page.regions:
            if not valid_box(region) or not region.text:
                continue

            if region.type not in {
                RegionType.UNKNOWN,
                RegionType.SOURCE_NAME,
            }:
                continue

            box = box_on_page(region, page)
            center_x = (box[0] + box[2]) / 2

            if box[3] > self.config.header_max_y:
                continue

            if not (
                self.config.header_center_min_x
                <= center_x
                <= self.config.header_center_max_x
            ):
                continue

            name = normalize_name(region.text)

            if (
                4 <= len(name) <= 60
                and not any(char.isdigit() for char in name)
                and len(region.text.split()) <= 6
            ):
                candidates.add(name)

        return candidates

    @staticmethod
    def column(
        bodies: list[Region],
        page: PageRecord,
    ) -> tuple[float, float]:
        boxes = [box_on_page(region, page) for region in bodies]
        return (
            median(box[0] for box in boxes),
            median(box[2] for box in boxes),
        )

    def connection_evidence(
        self,
        chain: Chain,
        page: PageRecord,
        bodies: list[Region],
    ) -> list[str] | None:
        if page.pdf_page != chain.last.pdf_page + 1:
            return None

        if chain.count >= self.config.max_candidate_pages:
            return None

        # URL diversi sono una contraddizione.
        # URL uguali non vengono considerati una prova definitiva:
        # al momento l'estrazione può produrre URL troncati.
        anchor_url = url_key(chain.anchor.source.url)
        current_url = url_key(page.source.url)

        if anchor_url and current_url and anchor_url != current_url:
            return None

        anchor_date = chain.anchor.source.publication_date

        if (
            anchor_date is None
            or page.source.publication_date != anchor_date
        ):
            return None

        anchor_name = normalize_name(chain.anchor.source.name)
        current_name = normalize_name(page.source.name)

        if anchor_name and current_name and anchor_name != current_name:
            return None

        same_source_name = bool(
            anchor_name and current_name and anchor_name == current_name
        )
        same_header = bool(
            self.header_names(chain.anchor) & self.header_names(page)
        )

        if not same_source_name and not same_header:
            return None

        # Confronta anche con l'anchor per evitare deriva progressiva.
        current_column = self.column(bodies, page)

        for reference in (chain.anchor, chain.last):
            reference_bodies = self.body_candidates(reference)

            if not reference_bodies:
                return None

            reference_column = self.column(reference_bodies, reference)

            if any(
                abs(a - b) > self.config.column_edge_tolerance
                for a, b in zip(current_column, reference_column)
            ):
                return None

        return [
            "adjacent_pdf_pages",
            "same_source_name" if same_source_name else "same_source_header",
            "same_publication_date",
            "compatible_body_column",
            "no_new_article_title",
        ]

    def annotate(
        self,
        page: PageRecord,
        bodies: list[Region],
        chain: Chain,
        previous_page: int | None,
        evidence: list[str],
    ) -> None:
        for region in page.regions:
            if not self.eligible(region):
                continue

            local_role: str | None = None

            if any(region is body for body in bodies):
                local_role = "body"

            elif previous_page is None and (
                region.type in {
                    RegionType.ARTICLE_TITLE,
                    RegionType.ARTICLE_SUBTITLE,
                    RegionType.AUTHOR,
                }
                and region.metadata.get("content_scope") == "main"
            ):
                local_role = "article_header"

            elif region.type in {
                RegionType.UNKNOWN,
                RegionType.ARTICLE_SECTION_HEADER,
            } and self.near_body(region, bodies, page):
                if region.type == RegionType.ARTICLE_SECTION_HEADER:
                    local_role = "section_header_candidate"
                elif (
                    region.raw_label == "text"
                    and len(re.findall(r"\w+", region.text or ""))
                    >= self.config.fragment_min_words
                ):
                    local_role = "body_fragment_candidate"

            if local_role is None:
                continue

            region.metadata.update(
                {
                    "article_candidate_id": chain.candidate_id,
                    "article_anchor_pdf_page": chain.anchor.pdf_page,
                    "continuation_status": (
                        "anchor" if previous_page is None else "candidate"
                    ),
                    "continuation_from_pdf_page": previous_page,
                    "continuation_evidence": list(evidence),
                    "continuation_local_role": local_role,
                }
            )

    def near_body(
        self,
        region: Region,
        bodies: list[Region],
        page: PageRecord,
    ) -> bool:
        candidate_box = box_on_page(region, page)

        for body in bodies:
            body_box = box_on_page(body, page)

            gap = max(
                0.0,
                candidate_box[1] - body_box[3],
                body_box[1] - candidate_box[3],
            )

            if (
                gap <= self.config.local_neighbor_gap
                and horizontal_overlap(candidate_box, body_box)
                >= self.config.minimum_horizontal_overlap
            ):
                return True

        return False