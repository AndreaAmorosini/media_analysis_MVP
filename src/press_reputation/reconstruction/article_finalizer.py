from collections import defaultdict
from statistics import mean

from press_reputation.config import ArticleFinalizationConfig
from press_reputation.lookup import normalize_entity_key
from press_reputation.models.article import (ArticleRecord, ArticleSource, ExtractionInfo, Media, Provenance, SectionHeader)
from press_reputation.models.page import PageRecord, PageType, Region, RegionType, SourceType
from press_reputation.reconstruction.flow_models import ArticleDraft
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch


class ArticleFinalizer:
    MEDIA_TYPES = {
        RegionType.IMAGE, RegionType.CAPTION, RegionType.TABLE,
        RegionType.INFOGRAPHIC, RegionType.PULL_QUOTE,
    }
    BLOCKED_SCOPES = {"related", "advertisement", "navigation", "non_main", "boilerplate"}

    def __init__(self, config: ArticleFinalizationConfig | None = None) -> None:
        self.config = config or ArticleFinalizationConfig()

    @staticmethod
    def region_id(region: Region) -> str | None:
        value = region.metadata.get("region_id")
        return value if isinstance(value, str) and value else None

    def members(self, draft: ArticleDraft, pages: list[PageRecord]) -> list[tuple[PageRecord, Region]]:
        result = []
        for page in pages:
            if page.document_id != draft.document_id or page.pdf_page not in draft.pdf_pages:
                continue

            for region in page.regions:
                legacy_id = region.metadata.get("article_candidate_id")
                article_id = region.article_id or legacy_id
                if (article_id != draft.id or (legacy_id and region.article_id and legacy_id != region.article_id) or
                    region.exclude_from_article_text or region.metadata.get("content_scope") in self.BLOCKED_SCOPES):
                    continue
                result.append((page, region))
        return result

    @staticmethod
    def source_name(page: PageRecord) -> str | None:
        for region in page.regions:
            if region.type != RegionType.SOURCE_NAME:
                continue
            match = region.metadata.get("entity_lookup") or {}
            if match.get("kind") == "source" and not match.get("ambiguous"):
                return match.get("canonical_name") or page.source.name
        return page.source.name

    def unambiguous_pages(self, draft: ArticleDraft, pages: list[PageRecord]) -> list[PageRecord]:
        selected = []
        for page in pages:
            if page.document_id != draft.document_id or page.pdf_page not in draft.pdf_pages:
                continue

            ids = {
                region.article_id or region.metadata.get("article_candidate_id")
                for region in page.regions if region.type == RegionType.ARTICLE_TITLE and not region.exclude_from_article_text 
                    and (region.article_id or region.metadata.get("article_candidate_id"))
            }
            if len(ids) <= 1 and (not ids or draft.id in ids):
                selected.append(page)
        return selected

    def matched_index(self, draft: ArticleDraft, members: list[tuple[PageRecord, Region]], entries: dict[str, ReviewIndexEntry], matches: list[ReviewIndexMatch]) -> ReviewIndexEntry | None:
        title_ids = {self.region_id(region) for _, region in members if region.type == RegionType.ARTICLE_TITLE}
        found = [
            entries[match.entry_id] for match in matches if match.status == "matched" and match.entry_id in entries and
            match.title_region_id in title_ids and entries[match.entry_id].document_id == draft.document_id
        ]
        return found[0] if len({entry.id for entry in found}) == 1 and found else None

    @staticmethod
    def unique_value(values: list, field: str, warnings: list[str]):
        distinct = list(dict.fromkeys(value for value in values if value is not None))
        if len(distinct) > 1:
            warnings.append(f"Conflicting {field} across article pages")
            return None
        return distinct[0] if distinct else None

    def finalize(self, draft: ArticleDraft, pages: list[PageRecord], entries: dict[str, ReviewIndexEntry], matches: list[ReviewIndexMatch]) -> ArticleRecord:
        warnings = list(draft.warnings)
        field_origins: dict[str, str] = {}
        members = self.members(draft, pages)
        safe_pages = self.unambiguous_pages(draft, pages)
        entry = self.matched_index(draft, members, entries, matches)

        names = [self.source_name(page) for page in safe_pages]
        normalized_names = {normalize_entity_key(name) for name in names if name}
        if len(normalized_names) > 1:
            warnings.append("Conflicting source across article pages")
            source_name = None
        else:
            source_name = next((name for name in names if name), None)

        if source_name:
            field_origins["source"] = "page_metadata"
        elif entry and entry.source:
            source_name = entry.source
            field_origins["source"] = "review_index_prior"
            warnings.append("Source taken from Review Index prior")

        publication_date = self.unique_value(
            [page.source.publication_date for page in safe_pages], "publication_date", warnings,
        )
        if publication_date:
            field_origins["publication_date"] = "page_metadata"
        elif entry and entry.publication_date:
            publication_date = entry.publication_date
            field_origins["publication_date"] = "review_index_prior"
            warnings.append("Date taken from Review Index prior")

        original_pages = sorted({
            page.source.original_page for page in safe_pages if page.source.original_page is not None
        })
        if not original_pages and entry and entry.original_page is not None:
            original_pages = [entry.original_page]
            field_origins["original_pages"] = "review_index_prior"
        elif original_pages:
            field_origins["original_pages"] = "page_metadata"

        urls = [page.source.url for page in safe_pages if page.source.url]
        url = self.unique_value(urls, "url", warnings)
        if url:
            field_origins["url"] = "page_metadata"

        source_types = [page.source.type for page in safe_pages if page.source.type != SourceType.UNKNOWN]
        source_type = self.unique_value(source_types, "source_type", warnings) or SourceType.UNKNOWN
        page_types = {page.page_type for page in safe_pages if page.page_type != PageType.UNKNOWN}
        if PageType.WEB in page_types and PageType.CLIPPING in page_types:
            warnings.append("Conflicting web and clipping page types")
            article_type = "unknown"
        elif PageType.WEB in page_types or source_type == SourceType.WEB:
            article_type = "web"
        elif PageType.CLIPPING in page_types or source_type == SourceType.NEWSPAPER:
            article_type = "newspaper"
        else:
            article_type = "unknown"

        ordered_members = sorted(
            members, key=lambda item: (
                item[0].pdf_page,
                item[1].bbox[1] if item[1].bbox else float("inf"),
                item[1].bbox[0] if item[1].bbox else float("inf"),
            ),
        )
        subtitles = list(dict.fromkeys(
            (region.text or "").strip() for _, region in ordered_members
            if region.type == RegionType.ARTICLE_SUBTITLE and region.text
        ))
        subtitle = " ".join(subtitles) if subtitles else None

        authors = list(dict.fromkeys(
            (region.metadata.get("author_resolution", {}).get("name_candidate") or region.text or "").strip()
            for _, region in ordered_members if region.type == RegionType.AUTHOR and region.text
        ))
        locations = list(dict.fromkeys(
            (region.text or "").strip()
            for _, region in ordered_members if region.type == RegionType.LOCATION and region.text
        ))

        segment_by_region = {
            (segment.pdf_page, segment.region_id): segment for segment in draft.segments
        }
        section_headers = [
            SectionHeader(
                text=segment.text, page=segment.pdf_page, region_id=segment.region_id,
                order=segment.order, body_raw_charspan=segment.article_charspan,
            )
            for segment in draft.segments if segment.type == RegionType.ARTICLE_SECTION_HEADER
        ]

        media = []
        provenance = []
        for page, region in ordered_members:
            region_id = self.region_id(region)
            segment = segment_by_region.get((page.pdf_page, region_id))
            clustering = region.metadata.get("article_clustering") or {}

            provenance.append(Provenance(
                page=page.pdf_page, bbox=region.bbox, content_type=region.type.value,
                region_id=region_id, method=clustering.get("method"),
                extraction_method=region.extraction_method,
                article_charspan=segment.article_charspan if segment else None,
                source_refs=region.provenance,
            ))

            if region.type in self.MEDIA_TYPES:
                media.append(Media(
                    type=region.type.value, page=page.pdf_page, region_id=region_id,
                    bbox=region.bbox, text=region.text,
                    linked_media_region_id=(
                        clustering.get("anchor_region_id") if region.type == RegionType.CAPTION else None
                    ),
                    extraction_method=region.extraction_method, provenance=region.provenance,
                ))

        if entry:
            provenance.append(Provenance(
                page=entry.index_pdf_page, content_type="review_index_prior",
                method="matched_review_index", source_refs=entry.provenance,
            ))

        body_raw = draft.body_raw if draft.body_raw is not None else draft.body
        body_clean = draft.body_clean if draft.body_clean is not None else body_raw
        if draft.body_clean is None:
            warnings.append("Body normalization missing; using raw body")

        raw_methods = {
            region.extraction_method for _, region in members
            if region.type == RegionType.ARTICLE_BODY and region.text
        } - {"unknown"}
        extraction_method = (
            "mixed" if len(raw_methods) > 1 else next(iter(raw_methods), "unknown")
        )

        # Non chiamare "accuracy" una proxy inventata: oggi non sono
        # disponibili confidence OCR/native calibrate per regione.
        extraction_scores = [
            region.metadata.get("extraction_confidence") for _, region in members
            if region.type == RegionType.ARTICLE_BODY
            and isinstance(region.metadata.get("extraction_confidence"), (int, float))
        ]
        extraction_confidence = mean(extraction_scores) if extraction_scores else None

        reading_scores = [
            segment.confidence for segment in draft.segments if segment.confidence is not None
        ]
        clustering_scores = [
            (region.metadata.get("article_clustering") or {}).get("score")
            for _, region in members if region.type == RegionType.ARTICLE_BODY
        ]
        clustering_scores = [
            score for score in clustering_scores if isinstance(score, (int, float))
        ]
        reconstruction_confidence = None
        if reading_scores:
            reconstruction_confidence = (
                0.7 * mean(reading_scores) + 0.3 * mean(clustering_scores)
                if clustering_scores else mean(reading_scores)
            )
            if any(link.status == "candidate" for link in draft.links):
                reconstruction_confidence -= 0.15
            reconstruction_confidence = round(max(0.0, min(reconstruction_confidence, 1.0)), 4)

        text_region_keys = {
            (page.pdf_page, self.region_id(region)) for page, region in members
            if region.type in {RegionType.ARTICLE_BODY, RegionType.ARTICLE_SECTION_HEADER}
        }
        if any((segment.pdf_page, segment.region_id) not in text_region_keys for segment in draft.segments):
            warnings.append("Draft contains segments not attributable to this article's current regions")

        if not draft.title:
            warnings.append("No unambiguous article title")
        if not source_name:
            warnings.append("Article source unavailable")
        if not publication_date:
            warnings.append("Publication date unavailable")
        if extraction_confidence is None:
            warnings.append("Calibrated extraction confidence unavailable")

        ready = (
            bool(draft.title and body_clean.strip())
            and not any(link.status != "accepted" for link in draft.links)
            and not any("Conflicting" in warning or "not attributable" in warning for warning in warnings)
            and reconstruction_confidence is not None
            and reconstruction_confidence >= self.config.min_reconstruction_confidence_for_ready
            and (not self.config.require_source_for_ready or bool(source_name))
            and (not self.config.require_date_for_ready or publication_date is not None)
        )
        return ArticleRecord(
            id=draft.id, document_id=draft.document_id,
            source=ArticleSource(source_type=source_type, source=source_name),
            publication_date=publication_date, title=draft.title, subtitle=subtitle,
            authors=authors, locations=locations, original_pages=original_pages,
            pdf_pages=sorted(set(draft.pdf_pages)), url=url,
            body_raw=body_raw, body=body_clean, section_headers=section_headers, media=media,
            article_type=article_type, provenance=provenance,
            extraction_confidence=extraction_confidence,
            reconstruction_confidence=reconstruction_confidence,
            extraction=ExtractionInfo(
                method=extraction_method, confidence=extraction_confidence,
                warnings=[warning for warning in warnings if "extraction" in warning.lower()],
            ),
            finalization_status="ready" if ready else "review_required",
            finalization_warnings=list(dict.fromkeys(warnings)),
            field_origins=field_origins,
        )

    def enrich(self, drafts: list[ArticleDraft], pages: list[PageRecord], entries: list[ReviewIndexEntry], matches: list[ReviewIndexMatch]) -> list[ArticleRecord]:
        entries_by_id = {entry.id: entry for entry in entries}
        return [
            self.finalize(draft, pages, entries_by_id, matches) for draft in drafts
        ]