from __future__ import annotations

from collections import defaultdict
from typing import Any

from press_reputation.config import ExtractionCompletenessConfig
from press_reputation.models.article import (
    ArticleRecord,
    DocumentCompletenessReport,
    ExtractionCompleteness,
)
from press_reputation.models.page import PageRecord, PageType, Region, RegionType
from press_reputation.reconstruction.flow_models import ArticleDraft, FlowLink
from press_reputation.review_index.matcher import title_similarity
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch


TECHNICAL_TYPES = {
    RegionType.HEADER_METADATA, RegionType.SOURCE_NAME,
    RegionType.PRESS_REVIEW_PROVIDER, RegionType.PUBLICATION_DATE,
    RegionType.ORIGINAL_PAGE, RegionType.CLIPPING_SHEET,
    RegionType.WATERMARK, RegionType.RIGHTS_NOTICE,
    RegionType.FOOTER, RegionType.ADVERTISEMENT,
    RegionType.NAVIGATION, RegionType.RELATED_CONTENT,
    RegionType.ARTICLE_POSITION_THUMBNAIL,
}
BLOCKED_SCOPES = {"related", "advertisement", "navigation", "non_main", "boilerplate"}
BODY_CONTENT_TYPES = {RegionType.ARTICLE_BODY, RegionType.ARTICLE_SECTION_HEADER}


def visible_chars(text: str | None) -> int:
    return sum(not char.isspace() for char in text or "")


def editorial_region(region: Region) -> bool:
    return (
        region.type not in TECHNICAL_TYPES
        and not region.exclude_from_article_text
        and region.metadata.get("content_scope") not in BLOCKED_SCOPES
        and not region.metadata.get("inside_article_position_thumbnail")
    )


class ExtractionCompletenessAnalyzer:
    def __init__(self, config: ExtractionCompletenessConfig | None = None) -> None:
        self.config = config or ExtractionCompletenessConfig()

    @staticmethod
    def to_dict(document: Any) -> dict:
        if isinstance(document, dict):
            return document
        if hasattr(document, "export_to_dict"):
            return document.export_to_dict()
        return document.model_dump(mode="json", by_alias=True, exclude_none=True)

    @staticmethod
    def region_id(region: Region) -> str | None:
        value = region.metadata.get("region_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def article_id(region: Region) -> str | None:
        return region.article_id or region.metadata.get("article_candidate_id")

    @staticmethod
    def raw_item_is_editorial(item: dict) -> bool:
        if item.get("label") not in {"text", "section_header"}:
            return False

        text = (item.get("text") or "").casefold().strip()
        if not text or text in {"web", "stampa locale", "stampa nazionale"}:
            return False

        return not any(marker in text for marker in (
            "tiratura:", "diffusione:", "lettori:", "dir. resp.",
            "articolo non cedibile", "riproduzione riservata",
        ))

    @staticmethod
    def raw_items(document: Any) -> list[dict]:
        return ExtractionCompletenessAnalyzer.to_dict(document).get("texts", [])

    @staticmethod
    def source_regions(page: PageRecord, self_ref: str) -> list[Region]:
        return [
            region for region in page.regions
            if (region.extraction_method == "ocr" and any(prov.get("self_ref") == self_ref and
                                                            prov.get("extraction_method") == "ocr" for prov in region.provenance))
            or any(
                alternative.get("extraction_method") == "ocr"
                and any(prov.get("self_ref") == self_ref for prov in alternative.get("provenance") or [])
                for alternative in region.metadata.get("equivalent_extraction_regions") or []
            )
        ]
    @staticmethod
    def page_article_ids(page: PageRecord) -> set[str]:
        return {
            article_id
            for region in page.regions
            if region.type in {RegionType.ARTICLE_TITLE, RegionType.ARTICLE_BODY}
            if (article_id := ExtractionCompletenessAnalyzer.article_id(region))
        }

    def belongs_unambiguously(self, region: Region, page: PageRecord, draft_id: str) -> bool:
        existing = self.article_id(region)
        if existing:
            return existing == draft_id

        alternatives = (region.metadata.get("article_clustering") or {}).get("alternatives") or []
        ranked = sorted(alternatives, key=lambda item: -item.get("score", 0.0))
        if ranked:
            best = ranked[0]
            second = ranked[1].get("score", 0.0) if len(ranked) > 1 else 0.0
            return (
                best.get("article_id") == draft_id
                and best.get("score", 0.0) - second >= self.config.min_region_candidate_margin
            )

        return self.page_article_ids(page) == {draft_id}

    @staticmethod
    def raw_interval(region: Region, self_ref: str) -> tuple[int, int] | None:
        recovery = region.metadata.get("text_fragment_recovery") or {}
        if recovery.get("status") == "ambiguous":
            return None

        span = recovery.get("effective_charspan")
        if span is None:
            span = next(
                (prov.get("charspan") for prov in region.provenance if prov.get("self_ref") == self_ref),
                None,
            )

        if (
            not isinstance(span, (tuple, list)) or len(span) != 2
            or type(span[0]) is not int or type(span[1]) is not int
        ):
            return None
        return span[0], span[1]

    @staticmethod
    def retained_intervals(region: Region, self_ref: str, raw_text: str) -> tuple[list[tuple[int, int]], bool]:
        span = ExtractionCompletenessAnalyzer.raw_interval(region, self_ref)
        if span is None:
            return [], False

        start, end = span
        if not 0 <= start < end <= len(raw_text):
            return [], False

        cleanup = region.metadata.get("watermark_text_cleanup") or {}
        if cleanup.get("status") != "applied":
            return [(start, end)], True

        original_text = cleanup.get("original_text")
        kept = cleanup.get("kept_raw_spans")
        if not isinstance(original_text, str) or len(original_text) != end - start or not isinstance(kept, list):
            return [], False

        result = []
        for interval in kept:
            if (
                not isinstance(interval, (tuple, list)) or len(interval) != 2
                or type(interval[0]) is not int or type(interval[1]) is not int
                or not 0 <= interval[0] <= interval[1] <= len(original_text)
            ):
                return [], False
            if interval[0] < interval[1]:
                result.append((start + interval[0], start + interval[1]))

        return result, True

    @staticmethod
    def mark(mask: list[bool], intervals: list[tuple[int, int]]) -> None:
        for start, end in intervals:
            mask[start:end] = [True] * (end - start)

    def item_retention(self, raw_text: str, self_ref: str, regions: list[Region]) -> tuple[int, int] | None:
        retained = [False] * len(raw_text)
        intentionally_removed = [False] * len(raw_text)

        for region in regions:
            # I candidati unmapped sono conservati, ma non hanno offset
            # attribuibili in modo affidabile alla stringa raw.
            if (region.metadata.get("text_fragment_recovery") or {}).get("status") == "ambiguous":
                continue

            refs = [region] if region.extraction_method == "ocr" else []
            alternatives = (region.metadata.get("equivalent_extraction_regions") or [])
            if alternatives and editorial_region(region):
                # Il merge ha conservato testo nativo equivalente, ma la
                # regione OCR non esiste più: i suoi charspan restano qui.
                for alternative in alternatives:
                    if alternative.get("extraction_method") != "ocr":
                        continue
                    for prov in alternative.get("provenance") or []:
                        if prov.get("self_ref") != self_ref:
                            continue
                        span = prov.get("charspan")
                        if (
                            isinstance(span, (tuple, list)) and len(span) == 2
                            and all(type(value) is int for value in span)
                            and 0 <= span[0] < span[1] <= len(raw_text)
                            and " ".join(region.text.split()).casefold()
                            == " ".join(raw_text[span[0]:span[1]].split()).casefold()
                        ):
                            self.mark(retained, [(span[0], span[1])])

            for source in refs:
                if not any(prov.get("self_ref") == self_ref for prov in source.provenance):
                    continue

                intervals, valid = self.retained_intervals(source, self_ref, raw_text)
                if not valid:
                    return None

                if editorial_region(source):
                    self.mark(retained, intervals)

                cleanup = source.metadata.get("watermark_text_cleanup") or {}
                if cleanup.get("status") == "applied":
                    span = self.raw_interval(source, self_ref)
                    removed = cleanup.get("removed_raw_span")
                    if (span and isinstance(removed, (tuple, list)) and len(removed) == 2 and
                        all(type(value) is int for value in removed)):
                        lo, hi = removed
                        if 0 <= lo < hi <= span[1] - span[0]:
                            self.mark(intentionally_removed, [(span[0] + lo, span[0] + hi)])

        available = sum(not char.isspace() and not intentionally_removed[index] for index, char in enumerate(raw_text))
        if available == 0:
            return 0, 0

        preserved = sum(retained[index] and not char.isspace() and not intentionally_removed[index] for index, char in enumerate(raw_text))
        return preserved, available
    
    def ocr_retention_for_draft(self, draft: ArticleDraft, pages: list[PageRecord], ocr_documents: dict[int, Any]) -> tuple[float | None, list[str]]:
        page_by_number = {
            page.pdf_page: page for page in pages
            if page.document_id == draft.document_id and page.pdf_page in draft.pdf_pages
        }
        available, preserved = 0, 0
        reasons: list[str] = []

        for page_number, page in page_by_number.items():
            document = ocr_documents.get(page_number)
            expects_ocr = (
                page.extraction_profile is not None
                and page.extraction_profile.run_ocr
            )
            if document is None:
                if expects_ocr:
                    reasons.append(f"ocr_raw_missing_page_{page_number}")
                continue

            for item in self.raw_items(document):
                if not self.raw_item_is_editorial(item):
                    continue

                self_ref, raw_text = item.get("self_ref"), item.get("text")
                if not isinstance(self_ref, str) or not isinstance(raw_text, str):
                    reasons.append(f"ocr_item_without_reference_page_{page_number}")
                    continue

                provenances = item.get("prov") or []
                if not provenances or any(prov.get("page_no") != 1 for prov in provenances):
                    reasons.append(f"ocr_item_page_mapping_uncertain_{page_number}:{self_ref}")
                    continue

                regions = self.source_regions(page, self_ref)
                owners = {self.article_id(region) for region in regions if self.article_id(region)}

                # Non attribuire il raw di una pagina multi-articolo
                # al draft più vicino per sola geometria.
                if owners and owners != {draft.id}:
                    if draft.id in owners:
                        reasons.append(f"ocr_item_multiarticle_{page_number}:{self_ref}")
                    continue
                if not owners and self.page_article_ids(page) != {draft.id}:
                    continue

                counts = self.item_retention(raw_text, self_ref, regions)
                if counts is None:
                    reasons.append(f"ocr_item_retention_unverifiable_{page_number}:{self_ref}")
                    continue

                item_preserved, item_available = counts
                preserved += item_preserved
                available += item_available

        if reasons:
            return None, list(dict.fromkeys(reasons))
        return (preserved / available if available else None), []

    @staticmethod
    def matched_index_entry_id(
        draft: ArticleDraft, pages: list[PageRecord], matches: list[ReviewIndexMatch],
    ) -> str | None:
        title_ids = {
            region.metadata.get("region_id")
            for page in pages if page.document_id == draft.document_id
            for region in page.regions
            if region.type == RegionType.ARTICLE_TITLE
            and (region.article_id or region.metadata.get("article_candidate_id")) == draft.id
        }
        entry_ids = {
            match.entry_id for match in matches
            if match.status == "matched" and match.title_region_id in title_ids
        }
        return next(iter(entry_ids)) if len(entry_ids) == 1 else None

    @staticmethod
    def incomplete_chains(draft: ArticleDraft, pages: list[PageRecord]) -> tuple[int, list[str]]:
        reasons = [
            f"unresolved_flow_{link.from_pdf_page}_{link.to_pdf_page}"
            for link in draft.links if link.status != "accepted"
        ]
        reasons.extend(
            f"accepted_flow_missing_page_{link.from_pdf_page}_{link.to_pdf_page}"
            for link in draft.links
            if link.status == "accepted"
            and not {link.from_pdf_page, link.to_pdf_page}.issubset(set(draft.pdf_pages))
        )

        sheets = {
            page.clipping.sheet_current for page in pages
            if page.document_id == draft.document_id and page.pdf_page in draft.pdf_pages
            and page.clipping and page.clipping.sheet_current is not None
            and any(
                (region.article_id or region.metadata.get("article_candidate_id")) == draft.id
                for region in page.regions
            )
        }
        totals = {
            page.clipping.sheet_total for page in pages
            if page.document_id == draft.document_id and page.pdf_page in draft.pdf_pages
            and page.clipping and page.clipping.sheet_total is not None
        }
        if len(totals) == 1 and len(sheets) < next(iter(totals)):
            reasons.append("incomplete_clipping_sheets")

        return len(reasons), reasons

    def for_draft(self, draft: ArticleDraft, pages: list[PageRecord], ocr_documents: dict[int, Any], matches: list[ReviewIndexMatch]) -> ExtractionCompleteness:
        included = {(segment.pdf_page, segment.region_id) for segment in draft.segments if segment.type == RegionType.ARTICLE_BODY}
        selected_pages = [page for page in pages if page.document_id == draft.document_id and page.pdf_page in draft.pdf_pages]

        assigned = sum(visible_chars(segment.text) for segment in draft.segments if segment.type == RegionType.ARTICLE_BODY)
        unassigned = unresolved_regions = unresolved_chars = unmapped = 0
        ambiguous_multiarticle = False
        counted: set[tuple[int, str]] = set()

        for page in selected_pages:
            for region in page.regions:
                if not region.text or not editorial_region(region):
                    continue

                region_id = self.region_id(region)
                key = (page.pdf_page, region_id if region_id is not None else id(region))
                if region_id is None:
                    ambiguous_multiarticle = True
                if key in counted:
                    continue
                counted.add(key)

                if not self.belongs_unambiguously(region, page, draft.id):
                    alternatives = (region.metadata.get("article_clustering") or {}).get("alternatives") or []
                    if any(option.get("article_id") == draft.id for option in alternatives):
                        ambiguous_multiarticle = True
                    continue

                recovery = region.metadata.get("text_fragment_recovery") or {}
                if recovery.get("status") == "ambiguous" and recovery.get("source_interval"):
                    unmapped += visible_chars(region.text)
                elif region.type == RegionType.ARTICLE_BODY and key not in included:
                    unassigned += visible_chars(region.text)
                elif (
                    region.type == RegionType.UNKNOWN
                    and region.raw_label in {"text", "section_header"}
                    and key not in included
                ):
                    unresolved_regions += 1
                    unresolved_chars += visible_chars(region.text)

        denominator = assigned + unassigned + unresolved_chars + unmapped
        coverage = assigned / denominator if denominator else None
        retention, retention_reasons = self.ocr_retention_for_draft(draft, pages, ocr_documents)
        incomplete_links, link_reasons = self.incomplete_chains(draft, pages)
        reasons = retention_reasons + link_reasons

        # Soglie relative: un residuo e' tollerabile fino al 10% dei
        # caratteri assegnati, oltre al limite assoluto di configurazione.
        residual_limit = max(self.config.max_residual_ratio_for_ready * assigned, 1.0)

        if coverage is None:
            reasons.append("body_assignment_not_measurable")
        elif coverage < self.config.min_body_assignment_coverage_for_ready:
            reasons.append("body_assignment_coverage_below_threshold")
        if unassigned > max(self.config.max_unassigned_body_chars_for_ready, residual_limit):
            reasons.append("unassigned_body_text")
        if unmapped > max(self.config.max_unmapped_text_chars_for_ready, residual_limit):
            reasons.append("unmapped_ocr_text")
        if unresolved_chars > max(self.config.max_unresolved_editorial_chars_for_ready, residual_limit):
            reasons.append("unresolved_editorial_text")
        if ambiguous_multiarticle:
            reasons.append("unresolved_multiarticle_page_editorial_text")

        uses_ocr = any(
            (page.extraction_profile is not None and page.extraction_profile.run_ocr)
            or any(region.extraction_method == "ocr" and editorial_region(region) and self.article_id(region) == draft.id
                    for region in page.regions)
            for page in selected_pages
        )
        
        if uses_ocr and retention is None and self.config.require_measurement_for_ocr_ready:
            reasons.append("ocr_retention_not_measurable")
        elif retention is not None and retention < self.config.min_ocr_text_retention_for_ready:
            reasons.append("ocr_text_retention_below_threshold")

        reasons = list(dict.fromkeys(reasons))
        status = (
            "unknown" if coverage is None or (uses_ocr and retention is None)
            else "incomplete" if reasons else "complete"
        )
        return ExtractionCompleteness(
            ocr_text_retention=retention,
            body_assignment_coverage=coverage,
            assigned_body_chars=assigned,
            unassigned_body_chars=unassigned,
            unresolved_editorial_regions=unresolved_regions,
            unresolved_editorial_chars=unresolved_chars,
            unmapped_text_chars=unmapped,
            index_entry_id=self.matched_index_entry_id(draft, pages, matches),
            incomplete_cross_page_chains=incomplete_links,
            status=status,
            reasons=reasons,
        )

    @staticmethod
    def suspicious_title(record: ArticleRecord, pages: list[PageRecord]) -> bool:
        ids = {ref.region_id for ref in record.article_header if ref.type == RegionType.ARTICLE_TITLE}
        return any(
            region.metadata.get("region_id") in ids
            and (
                region.metadata.get("type_before_title_resolution") == RegionType.ARTICLE_BODY.value
                or (region.metadata.get("title_candidate") or {}).get("score", 1.0) < 0.50
            )
            for page in pages if page.document_id == record.document_id
            for region in page.regions
        )

    @staticmethod
    def layout_areas(record: ArticleRecord, pages: list[PageRecord]) -> set[str]:
        return {
            area for page in pages
            if page.document_id == record.document_id and page.pdf_page in record.pdf_pages
            for region in page.regions
            if (region.article_id or region.metadata.get("article_candidate_id")) == record.id
            if (area := region.metadata.get("layout_area_id"))
        }

    def audit_document(
        self, document_id: str, records: list[ArticleRecord], pages: list[PageRecord],
        entries: list[ReviewIndexEntry], links: list[FlowLink], skipped_groups: list[dict],
    ) -> DocumentCompletenessReport:
        selected_records = [record for record in records if record.document_id == document_id]
        selected_pages = [page for page in pages if page.document_id == document_id]
        entry_ids = {entry.id for entry in entries if entry.document_id == document_id}
        represented = {
            record.extraction_completeness.index_entry_id for record in selected_records
            if record.extraction_completeness.index_entry_id
        }

        report = DocumentCompletenessReport(
            document_id=document_id,
            index_entries_total=len(entry_ids),
            index_entries_with_record=len(entry_ids & represented),
            index_article_coverage=len(entry_ids & represented) / len(entry_ids) if entry_ids else None,
            unmatched_index_entry_ids=sorted(entry_ids - represented),
            skipped_article_groups=[group for group in skipped_groups if group.get("document_id") == document_id],
        )

        index_pages = {entry.index_pdf_page for entry in entries if entry.document_id == document_id}
        report.orphan_editorial_region_ids = sorted({
            region_id for page in selected_pages if page.pdf_page not in index_pages
            for region in page.regions if editorial_region(region)
            if region.type in {RegionType.ARTICLE_BODY, RegionType.UNKNOWN}
            if region.text and not self.article_id(region)
            if (region_id := self.region_id(region))
        })

        for link in links:
            if link.document_id != document_id:
                continue
            owners = [
                record.id for record in selected_records
                if {link.from_pdf_page, link.to_pdf_page}.issubset(set(record.pdf_pages))
            ]
            if link.status != "accepted" or link.article_candidate_id not in owners:
                report.unresolved_flow_links.append({
                    "article_candidate_id": link.article_candidate_id,
                    "from_pdf_page": link.from_pdf_page,
                    "to_pdf_page": link.to_pdf_page,
                    "status": link.status,
                    "contradictions": link.contradictions,
                })

        for index, left in enumerate(selected_records):
            for right in selected_records[index + 1:]:
                # L'URL a livello pagina è ereditato da ogni articolo presente
                # sulla pagina: non prova un duplicato se i due record
                # condividono pagine. Vale solo per pagine disgiunte.
                shares_pages = bool(set(left.pdf_pages) & set(right.pdf_pages))
                same_url = bool(
                    left.url and right.url and left.url == right.url
                    and not shares_pages
                )
                same_index = bool(
                    left.extraction_completeness.index_entry_id
                    and left.extraction_completeness.index_entry_id
                    == right.extraction_completeness.index_entry_id
                )
                similar_title = bool(
                    left.title and right.title
                    and title_similarity(left.title, right.title)
                    >= self.config.min_duplicate_title_similarity
                )
                adjacent_web = (
                    left.article_type == right.article_type == "web"
                    and any(abs(a - b) == 1 for a in left.pdf_pages for b in right.pdf_pages)
                )
                same_layout = bool(self.layout_areas(left, pages) & self.layout_areas(right, pages))
                suspicious = self.suspicious_title(left, pages) or self.suspicious_title(right, pages)
                source_conflict = bool(
                    left.source.source and right.source.source
                    and left.source.source.casefold() != right.source.source.casefold()
                )
                date_conflict = bool(
                    left.publication_date and right.publication_date
                    and left.publication_date != right.publication_date
                )

                possible_split = adjacent_web and not source_conflict and not date_conflict and (same_layout or suspicious)
                if same_url or same_index or (similar_title and adjacent_web) or possible_split:
                    report.possible_duplicate_record_pairs.append({
                        "left_id": left.id,
                        "right_id": right.id,
                        "evidence": {
                            "same_url": same_url,
                            "same_index_entry": same_index,
                            "similar_title": similar_title,
                            "adjacent_web_pages": adjacent_web,
                            "same_layout_area": same_layout,
                            "suspicious_title": suspicious,
                            "possible_cross_page_split": possible_split,
                        },
                    })

        if report.unmatched_index_entry_ids:
            report.warnings.append("Review Index entries without corresponding ArticleRecord")
        if report.skipped_article_groups:
            report.warnings.append("Article groups skipped during draft assembly")
        if report.orphan_editorial_region_ids:
            report.warnings.append("Editorial regions without article assignment")
        if report.possible_duplicate_record_pairs:
            report.warnings.append("Possible duplicate or split ArticleRecords")

        return report