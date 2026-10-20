import re
import unicodedata
from collections import defaultdict
from statistics import mean
from urllib.parse import urlsplit

from press_reputation.config import ArticleFinalizationConfig
from press_reputation.lookup import normalize_entity_key
from press_reputation.models.article import (ArticleRecord, ArticleSource, ExtractionInfo, Media, Provenance, SectionHeader)
from press_reputation.models.page import PageRecord, PageType, Region, RegionType, SourceType
from press_reputation.normalization.watermark_scrub import scrub_rights_phrase
from press_reputation.reconstruction.flow_models import ArticleDraft
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch
from press_reputation.models.article import ArticleRegionRef
from press_reputation.models.article import (
    DocumentCompletenessReport,
    ExtractionCompleteness,
)
from press_reputation.config import ExtractionCompletenessConfig
from press_reputation.reconstruction.extraction_completeness import ExtractionCompletenessAnalyzer
from press_reputation.reconstruction.flow_models import FlowLink

class ArticleFinalizer:
    MEDIA_TYPES = {
        RegionType.IMAGE, RegionType.CAPTION, RegionType.TABLE,
        RegionType.INFOGRAPHIC, RegionType.PULL_QUOTE,
    }
    BLOCKED_SCOPES = {"related", "advertisement", "navigation", "non_main", "boilerplate"}

    def __init__(self, config: ArticleFinalizationConfig | None = None, completeness_config: ExtractionCompletenessConfig | None = None) -> None:
        self.config = config or ArticleFinalizationConfig()
        self.completeness_config = completeness_config or ExtractionCompletenessConfig()
        self.completeness_analyzer = ExtractionCompletenessAnalyzer(self.completeness_config)
        self.document_completeness_reports: list[DocumentCompletenessReport] = []
    
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

    def matched_index(self, draft: ArticleDraft, members: list[tuple[PageRecord, Region]], entries: dict[str, ReviewIndexEntry], matches: list[ReviewIndexMatch]) -> list[ReviewIndexEntry]:
        title_ids = {self.region_id(region) for _, region in members if region.type == RegionType.ARTICLE_TITLE}
        match_scores = {match.entry_id: match.score for match in matches if match.status == "matched"}
        found = [
            entries[match.entry_id] for match in matches if match.status == "matched" and match.entry_id in entries and
            match.title_region_id in title_ids and entries[match.entry_id].document_id == draft.document_id
        ]
        if not found:
            return []
        # Righe d'indice quasi-duplicate possono matchare lo stesso articolo:
        # si seleziona il prior con score più alto invece di scartare tutto.
        unique: dict[str, ReviewIndexEntry] = {}
        for entry in found:
            unique.setdefault(entry.id, entry)
        return sorted(
            unique.values(),
            key=lambda entry: -(match_scores.get(entry.id, 0.0)),
        )

    @staticmethod
    def unique_value(values: list, field: str, warnings: list[str]):
        distinct = list(dict.fromkeys(value for value in values if value is not None))
        if len(distinct) > 1:
            warnings.append(f"Conflicting {field} across article pages")
            return None
        return distinct[0] if distinct else None

    def finalize(self, draft: ArticleDraft, pages: list[PageRecord], entries: dict[str, ReviewIndexEntry],
                    matches: list[ReviewIndexMatch], completeness: ExtractionCompleteness | None = None) -> ArticleRecord:
        warnings = list(draft.warnings)
        field_origins: dict[str, str] = {}
        members = self.members(draft, pages)
        safe_pages = self.unambiguous_pages(draft, pages)
        index_priors = self.matched_index(draft, members, entries, matches)
        entry = index_priors[0] if index_priors else None

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
        
        header_types = {
            RegionType.ARTICLE_KICKER, RegionType.ARTICLE_TITLE,
            RegionType.ARTICLE_SUBTITLE, RegionType.AUTHOR,
        }
        auxiliary_types = self.MEDIA_TYPES

        article_header: list[ArticleRegionRef] = []
        article_auxiliary: list[ArticleRegionRef] = []

        for page, region in ordered_members:
            region_id = self.region_id(region)
            if not region_id:
                continue

            ref = ArticleRegionRef(
                region_id=region_id,
                pdf_page=page.pdf_page,
                type=region.type,
                subrole=(
                    region.metadata.get("editorial_subrole")
                    if region.type == RegionType.ARTICLE_KICKER
                    else region.metadata.get("location_role")
                    if region.type == RegionType.LOCATION
                    else None
                ),
            )

            if region.type in header_types or (
                region.type == RegionType.LOCATION
                and region.metadata.get("location_role") == "dateline"
            ):
                article_header.append(ref)
            elif region.type in auxiliary_types:
                article_auxiliary.append(ref)

        article_content = [
            ArticleRegionRef(
                region_id=segment.region_id,
                pdf_page=segment.pdf_page,
                type=segment.type,
                order=segment.order,
                body_raw_charspan=segment.article_charspan,
            )
            for segment in sorted(
                draft.segments,
                key=lambda item: item.order if item.order is not None else 10**9,
            )
            if segment.type in {RegionType.ARTICLE_BODY, RegionType.ARTICLE_SECTION_HEADER}
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
        if len(index_priors) > 1:
            warnings.append("Multiple review index entries matched this article; best prior selected")
        if extraction_confidence is None:
            warnings.append("Calibrated extraction confidence unavailable")
            
        completeness = completeness or ExtractionCompleteness(
            status="unknown",
            reasons=["completeness_not_measured"],
        )
        warnings.extend(f"Completeness: {reason}" for reason in completeness.reasons)

        article_uses_ocr = any(
            page.document_id == draft.document_id
            and page.pdf_page in draft.pdf_pages
            and (
                (page.extraction_profile is not None and page.extraction_profile.run_ocr)
                or any(region.extraction_method == "ocr" and
                        (region.article_id or region.metadata.get("article_candidate_id")) == draft.id
                            for region in page.regions)
            )
            for page in pages
        )

        proximity_body = any(
            (region.metadata.get("article_clustering") or {}).get("method") == "proximity_fallback_v1"
            for _, region in members
        )
        index_prior_body = any(
            (region.metadata.get("article_clustering") or {}).get("method") == "index_prior_v1"
            for _, region in members
        )
        if proximity_body:
            warnings.append("Body recovered via proximity fallback; requires manual verification")
        elif index_prior_body:
            warnings.append("Body attributed via review index prior")

        retention_threshold = (
            self.completeness_config.min_ocr_text_retention_for_ready_all_ocr
            if article_uses_ocr and extraction_method == "ocr"
            else self.completeness_config.min_ocr_text_retention_for_ready
        )

        ready = (
            bool(draft.title and body_clean.strip())
            and not proximity_body
            and not any(link.status != "accepted" for link in draft.links)
            and not any("Conflicting" in warning or "not attributable" in warning for warning in warnings)
            and reconstruction_confidence is not None
            and reconstruction_confidence >= self.config.min_reconstruction_confidence_for_ready
            and (not self.config.require_source_for_ready or bool(source_name))
            and (not self.config.require_date_for_ready or publication_date is not None)
            and completeness.status == "complete"
            and completeness.body_assignment_coverage is not None
            and completeness.body_assignment_coverage >= self.completeness_config.min_body_assignment_coverage_for_ready
            and completeness.unassigned_body_chars <= self.completeness_config.max_unassigned_body_chars_for_ready
            and completeness.unmapped_text_chars <= self.completeness_config.max_unmapped_text_chars_for_ready
            and completeness.unresolved_editorial_chars <= self.completeness_config.max_unresolved_editorial_chars_for_ready
            and completeness.incomplete_cross_page_chains == 0
            and (
                not article_uses_ocr
                or not self.completeness_config.require_measurement_for_ocr_ready
                or completeness.ocr_text_retention is not None
            )
            and (
                completeness.ocr_text_retention is None
                or completeness.ocr_text_retention >= retention_threshold
            )
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
            article_header=article_header,
            article_content=article_content,
            article_auxiliary=article_auxiliary,
            body_clean=body_clean,
            extraction_completeness=completeness,
        )

    UNFINISHED_ENDINGS = {
        "in", "di", "a", "da", "con", "per", "tra", "fra", "che",
        "del", "della", "delle", "dei", "degli", "dello", "e", "o",
    }
    TERMINALS = {".", "!", "?", "…", ":"}
    # Boilerplate tipico delle testate web stampate a fine pagina: non è
    # testo editoriale e non deve entrare nei record di recupero.
    MASTHEAD_BOILERPLATE = re.compile(
        r"(testata\s+giornalistica|registrat\w+\s+al\s+tribunale|"
        r"tutti\s+i\s+diritti\s+riservati|diritti\s+riservati|"
        r"privacy\s+policy|cookie\s+policy|copyright\s+©?\s*\d{4}|"
        r"è\s+un\s+quotidiano\s+online\s+di|redazione\s+centrale|"
        r"web-?magazine|dedicat\w+\s+a\s+(politica|attualità|attualita|"
        r"cronaca|cultura|sport|eventi)|newsletter|seguici\s+su)",
        re.IGNORECASE,
    )
    MASTHEAD_MAX_CHARS = 300

    @classmethod
    def _continuation_evidence(cls, previous: PageRecord, previous_body: str,
                               current: PageRecord, current_body: str) -> str | None:
        """Evidenza di continuazione fra due pagine di body orfano.

       same_url: URL articolo identico su entrambe le pagine (stampa che
          ripete l'URL);
        url_continuation: la pagina precedente ha l'URL dell'articolo, la
          corrente no (continuazione di stampa), testo non concluso e
          nessuna nuova intestazione fonte;
        same_source_continuation: stessa fonte su entrambe, testo non
          concluso e minuscola iniziale (ritaglio cartaceo);
        url_only_continuation: stampa web multipagina senza segnali
          sintattici. La pagina precedente ha l'URL dell'articolo, la
          corrente è una pagina web priva di ogni identità (nessun URL,
          nessuna fonte): è la pagina successiva dello stesso printout.
        """
        prev_url = (previous.source.url or "").strip()
        curr_url = (current.source.url or "").strip()

        def article_like(url: str) -> bool:
            try:
                return urlsplit(url).path not in {"", "/"}
            except ValueError:
                return False

        last = previous_body.rstrip()
        words = last.split()
        last_word = words[-1].strip('",;«»…').lower() if words else ""
        unfinished = bool(last) and (
            last[-1:] not in cls.TERMINALS or last_word in cls.UNFINISHED_ENDINGS
        )
        first_alpha = next((char for char in current_body.lstrip() if char.isalpha()), None)
        starts_lower = first_alpha is not None and first_alpha.islower()

        if (prev_url and curr_url and prev_url == curr_url and article_like(prev_url)):
            return "same_url"
        if (prev_url and not curr_url and unfinished and starts_lower
                and not current.source.name):
            return "url_continuation"
        if (not prev_url and not curr_url and previous.source.name and
                previous.source.name == current.source.name and
                unfinished and starts_lower):
            return "same_source_continuation"
        # Stampa web multipagina: il frammento continua con frase completa
        # o citazione (nessun segnale sintattico), ma la pagina è web e
        # non ha identità propria. Il body web del record unito
        # sostituirà comunque i frammenti dopo il recupero.
        if (article_like(prev_url) and not curr_url and not current.source.name
                and current.page_type == PageType.WEB):
            return "url_only_continuation"
        return None

    @staticmethod
    def _group_body(group: dict) -> str:
        return group.get("body") or "\n\n".join(
            (region.text or "").strip() for region in group["regions"]
        )

    @staticmethod
    def _shingles(text: str, size: int = 8) -> set[str]:
        words = re.findall(r"\w+", unicodedata.normalize("NFKD", (text or "").casefold()))
        return {" ".join(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}

    @staticmethod
    def _first_alpha(text: str) -> str:
        return next((char for char in (text or "").lstrip() if char.isalpha()), "")

    def _tail_target(self, group: dict, candidates: list[ArticleRecord]) -> ArticleRecord | None:
        """Record di destinazione per un frammento-coda.

        Il frammento inizia in minuscola (frase spezzata) e non ha un proprio
        URL. I candidati sono articoli della stessa rassegna che terminano
        prima o sulla stessa pagina del frammento: una coda continua un
        articolo precedente, mai uno successivo. Si preferisce il body non
        concluso; in sua assenza serve stessa pagina o stessa fonte.
        A parità di distanza il caso resta ambiguo e non si attacca.
        """
        if any(page.source.url for page in group["pages"]):
            return None
        if not self._first_alpha(self._group_body(group)).islower():
            return None

        tail_pages = [page.pdf_page for page in group["pages"]]
        tail_source = next((page.source.name for page in group["pages"] if page.source.name), None)
        max_distance = self.config.tail_attachment_max_page_distance

        scored: list[tuple[int, int, int, ArticleRecord]] = []
        for record in candidates:
            if record.document_id != group["page"].document_id:
                continue
            preceding = [q for q in record.pdf_pages if q <= max(tail_pages)]
            if not preceding:
                continue
            distance = min(abs(p - q) for p in tail_pages for q in preceding)
            if distance > max_distance:
                continue
            body = (record.body_pdf_raw or record.body or "").rstrip()
            unfinished = bool(body) and body[-1] not in self.TERMINALS
            same_page = any(q in tail_pages for q in record.pdf_pages)
            same_source = bool(tail_source and record.source.source
                               and record.source.source == tail_source)
            if not (unfinished or same_page or same_source):
                continue
            scored.append((distance, 0 if unfinished else 1, id(record), record))

        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        if len(scored) > 1 and (scored[0][0], scored[0][1]) == (scored[1][0], scored[1][1]):
            return None
        return scored[0][3]

    def _attach_tail(self, record: ArticleRecord, group: dict) -> None:
        """Estende un record col frammento-coda: sempre review_required."""
        body = self._group_body(group)
        record.body_raw = (record.body_raw or "") + "\n\n" + body
        record.body = (record.body or "") + "\n\n" + body
        record.body_clean = (record.body_clean or record.body or "") + "\n\n" + body
        for owning_page, region in group["pairs"]:
            region.article_id = record.id
            region.metadata["article_clustering"] = {
                "status": "assigned",
                "method": "tail_attachment_v1",
                "score": None,
                "evidence": ["continuation_fragment", "lowercase_start", "adjacent_page"],
                "alternatives": [],
                "anchor_region_id": None,
            }
            region_id = self.region_id(region)
            if region_id:
                record.provenance.append(Provenance(
                    page=owning_page.pdf_page, bbox=region.bbox,
                    content_type=region.type.value, region_id=region_id,
                    method="tail_attachment_v1",
                    extraction_method=region.extraction_method,
                    source_refs=region.provenance,
                ))
                record.article_content.append(ArticleRegionRef(
                    region_id=region_id, pdf_page=owning_page.pdf_page,
                    type=region.type, order=len(record.article_content),
                ))
        record.pdf_pages = sorted(set(record.pdf_pages) | {page.pdf_page for page in group["pages"]})
        pages_note = ", ".join(str(page.pdf_page) for page in group["pages"])
        warning = f"Body extended by continuation fragment from page(s) {pages_note}; requires verification"
        if warning not in record.finalization_warnings:
            record.finalization_warnings.append(warning)
        record.finalization_status = "review_required"

    def _overlapping_record(self, body: str, document_id: str,
                             references: list[tuple[str, ArticleRecord, set[str]]],
                             ) -> ArticleRecord | None:
        """Record della stessa rassegna che già contiene questo testo."""
        shingles = self._shingles(body, self.config.duplicate_shingle_size)
        if len(shingles) < 5:
            return None
        best, best_overlap = None, 0.0
        for doc_id, record, other in references:
            if doc_id != document_id or not other or not shingles:
                continue
            overlap = len(shingles & other) / max(1, min(len(shingles), len(other)))
            if overlap > best_overlap:
                best, best_overlap = record, overlap
        if best_overlap >= self.config.duplicate_overlap_threshold:
            return best
        return None

    def _untitled_records(self, records: list[ArticleRecord], pages: list[PageRecord]) -> list[ArticleRecord]:
        """Record di recupero da body orfano senza identità titolo.

        Sempre review_required: lo scopo è non perdere testo editoriale
        classificato, non certificare un articolo. Le regioni ricevono un
        article_id sintetico con metodo untitled_record_v1 nei metadata.
        Gruppi su pagine adiacenti vengono fusi solo con evidenza forte
        (stesso URL articolo, continuazione di stampa, stessa fonte).
        """
        groups: list[dict] = []
        for page in sorted(pages, key=lambda item: (item.document_id, item.pdf_page)):
            orphans = [
                region for region in page.regions
                if (region.type == RegionType.ARTICLE_BODY and region.article_id is None and
                    not region.exclude_from_article_text and (region.text or "").strip() and
                    region.metadata.get("content_scope") not in self.BLOCKED_SCOPES)
            ]
            # Boilerplate di testata web (registrazione, copyright, policy):
            # regioni corte con pattern noto, lasciate orfane ma tracciate.
            usable = []
            for region in orphans:
                text = (region.text or "").strip()
                if (len(text) <= self.MASTHEAD_MAX_CHARS
                        and self.MASTHEAD_BOILERPLATE.search(text)):
                    region.metadata["untitled_record"] = {
                        "status": "excluded", "reason": "web_masthead_boilerplate",
                    }
                    continue
                usable.append(region)
            orphans = usable
            if not orphans:
                continue
            total_chars = sum(len((region.text or "").strip()) for region in orphans)
            if total_chars < self.config.untitled_record_min_body_chars:
                continue
            ordered = sorted(orphans, key=lambda region: (
                region.bbox[1] if region.bbox else 0.0,
                region.bbox[0] if region.bbox else 0.0,
            ))
            groups.append({
                "page": page, "regions": ordered,
                "pairs": [(page, region) for region in ordered],
                "chars": total_chars, "pages": [page], "evidence": [],
            })

        # Fusione multipagina solo su evidenza forte; in caso di dubbio
        # i gruppi restano separati.
        merged: list[dict] = []
        for group in groups:
            previous = merged[-1] if merged else None
            if previous and previous["page"].document_id == group["page"].document_id:
                previous_page = previous["pages"][-1]
                current_page = group["page"]
                if current_page.pdf_page == previous_page.pdf_page + 1:
                    previous_body = "\n\n".join(
                        (region.text or "").strip() for region in previous["regions"]
                    ) if len(previous["pages"]) == 1 else previous["body"]
                    current_body = "\n\n".join(
                        (region.text or "").strip() for region in group["regions"]
                    )
                    evidence = self._continuation_evidence(
                        previous_page, previous_body, current_page, current_body,
                    )
                    if evidence:
                        previous["pages"].append(current_page)
                        previous["regions"].extend(group["regions"])
                        previous["pairs"].extend(group["pairs"])
                        previous["chars"] += group["chars"]
                        previous["evidence"].append(evidence)
                        previous["body"] = previous_body + "\n\n" + current_body
                        continue
            merged.append(group)

        created: list[ArticleRecord] = []
        # Shingles dei record titolati per riconoscere il testo già coperto.
        references: list[tuple[str, ArticleRecord, set[str]]] = [
            (r.document_id, r,
             self._shingles(r.body_pdf_raw or r.body or "", self.config.duplicate_shingle_size))
            for r in records
        ]

        for group in merged:
            page = group["page"]
            pages_of_group = group["pages"]

            # Coda di un articolo esistente: il frammento non diventa record.
            target = self._tail_target(group, records + created)
            if target is not None:
                self._attach_tail(target, group)
                continue

            # Testo già dentro un altro record della stessa rassegna: il
            # record resta separato ma marcato per il conteggio downstream.
            group_body = self._group_body(group)
            overlapping = self._overlapping_record(group_body, page.document_id, references)

            article_id = f"untitled:{page.document_id}:p{pages_of_group[0].pdf_page}"
            if len(pages_of_group) > 1:
                article_id += f"-p{pages_of_group[-1].pdf_page}"
            all_regions = group["regions"]
            for region in all_regions:
                region.article_id = article_id
                region.metadata["article_clustering"] = {
                    "status": "assigned",
                    "method": "untitled_record_v1",
                    "score": None,
                    "evidence": ["orphan_body_recovery"] + list(group["evidence"]),
                    "alternatives": [],
                    "anchor_region_id": None,
                }

            body = "\n\n".join((region.text or "").strip() for region in all_regions)
            provenance = [
                Provenance(
                    page=owning_page.pdf_page, bbox=region.bbox,
                    content_type=region.type.value,
                    region_id=self.region_id(region),
                    method="untitled_record_v1",
                    extraction_method=region.extraction_method,
                    source_refs=region.provenance,
                )
                for owning_page, region in group["pairs"]
            ]
            url = next((p.source.url for p in pages_of_group if p.source.url), None)
            source_page = next((p for p in pages_of_group if p.source.name), pages_of_group[0])
            new_record = ArticleRecord(
                id=article_id,
                document_id=page.document_id,
                source=ArticleSource(source_type=source_page.source.type, source=source_page.source.name),
                publication_date=source_page.source.publication_date,
                title=None,
                original_pages=sorted({
                    p.source.original_page for p in pages_of_group
                    if p.source.original_page is not None
                }),
                pdf_pages=[p.pdf_page for p in pages_of_group],
                url=url,
                body_raw=body, body=body, body_clean=body,
                article_type="web" if page.page_type == PageType.WEB else (
                    "newspaper" if page.page_type == PageType.CLIPPING else "unknown"
                ),
                provenance=provenance,
                extraction=ExtractionInfo(method="untitled_orphan_recovery"),
                extraction_completeness=ExtractionCompleteness(
                    status="unknown", reasons=["untitled_record"],
                    unassigned_body_chars=0, assigned_body_chars=group["chars"],
                ),
                finalization_status="review_required",
                finalization_warnings=[
                    "Untitled record recovered from unattributed body; no article title found",
                ] + (
                    [f"Untitled pages merged by {', '.join(group['evidence'])}; requires verification"]
                    if group["evidence"] else []
                    ) + (
                    [f"Recovered text overlaps record {overlapping.id}; likely duplicate of the same clipping"]
                    if overlapping is not None else []
                ),
                field_origins={"body": "untitled_orphan_recovery"},
                article_content=[
                    ArticleRegionRef(
                        region_id=self.region_id(region), pdf_page=owning_page.pdf_page,
                        type=region.type, order=order,
                    )
                    for order, (owning_page, region) in enumerate(group["pairs"])
                    if self.region_id(region)
                ],
            )
            if overlapping is not None:
                new_record.extraction_completeness.reasons.append(
                    "recovered_text_overlaps_other_record"
                )
            created.append(new_record)
            references.append((page.document_id, new_record,
                              self._shingles(group_body, self.config.duplicate_shingle_size)))
        return created

    def enrich(self, drafts: list[ArticleDraft], pages: list[PageRecord], entries: list[ReviewIndexEntry],
                matches: list[ReviewIndexMatch], links: list[FlowLink] | None = None, skipped_groups: list[dict] | None = None,
                ocr_documents: dict[int, object] | None = None) -> list[ArticleRecord]:
        
        self.document_completeness_reports = []
        entries_by_id = {entry.id: entry for entry in entries}
        links = links or []
        skipped_groups = skipped_groups or []
        ocr_documents = ocr_documents or {}

        records: list[ArticleRecord] = []
        for draft in drafts:
            completeness = self.completeness_analyzer.for_draft(
                draft=draft,
                pages=pages,
                ocr_documents=ocr_documents,
                matches=matches,
            )
            records.append(
                self.finalize(
                    draft=draft,
                    pages=pages,
                    entries=entries_by_id,
                    matches=matches,
                    completeness=completeness,
                )
            )

        if self.config.allow_untitled_records:
            records.extend(self._untitled_records(records, pages))

        # Rete di sicurezza sui residui di copyright: qualunque sia la via
        # con cui il body e' stato costruito (PDF, web, frammenti assorbiti),
        # una dicitura "riproduzione riservata" non e' mai testo editoriale.
        # body_raw resta intatto perche' i charspan dei segmenti PDF si
        # riferiscono a quella stringa.
        for record in records:
            for field in ("body", "body_clean"):
                value = getattr(record, field, None)
                if not value:
                    continue
                cleaned = scrub_rights_phrase(value)
                if cleaned != value:
                    setattr(record, field, cleaned)

        document_ids = {
            *(page.document_id for page in pages),
            *(entry.document_id for entry in entries),
            *(record.document_id for record in records),
        }

        for document_id in sorted(document_ids):
            report = self.completeness_analyzer.audit_document(
                document_id=document_id,
                records=records,
                pages=pages,
                entries=entries,
                links=links,
                skipped_groups=skipped_groups,
            )
            self.document_completeness_reports.append(report)

            # Un possibile duplicato riguarda quei record specifici.
            duplicate_ids = {
                article_id
                for pair in report.possible_duplicate_record_pairs
                for article_id in (pair["left_id"], pair["right_id"])
            }
            for record in records:
                if record.document_id != document_id or record.id not in duplicate_ids:
                    continue

                record.finalization_status = "review_required"
                warning = "Possible duplicate ArticleRecord or split cross-page article"
                if warning not in record.finalization_warnings:
                    record.finalization_warnings.append(warning)

            # Un link irrisolto associato al record lo rende non ready.
            unresolved_ids = {
                item["article_candidate_id"]
                for item in report.unresolved_flow_links
            }
            for record in records:
                if record.document_id != document_id or record.id not in unresolved_ids:
                    continue

                record.finalization_status = "review_required"
                warning = "Incomplete or unresolved cross-page continuation"
                if warning not in record.finalization_warnings:
                    record.finalization_warnings.append(warning)

        return records