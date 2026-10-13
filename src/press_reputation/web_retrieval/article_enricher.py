"""Prefer high-quality public HTML article text; keep OCR/PDF as fallback.

This stage runs AFTER PDF article identities are resolved to avoid attaching the
wrong website text to another article from the same clipping page. It also
supports a *unique* title-only web anchor when no PDF draft was produced.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Callable

from press_reputation.models.article import (
    ArticleRecord, ArticleSource, ExtractionCompleteness, ExtractionInfo,
    Provenance,
)
from press_reputation.models.page import PageRecord, PageType, Region, RegionType, SourceType
from press_reputation.web_retrieval.article_fetcher import FetchOutcome, PublicArticleFetcher


class WebArticleEnricher:
    MIN_CHARS = 650
    MIN_WORDS = 90
    TITLE_SIMILARITY_THRESHOLD = 0.62
    MIN_PDF_LENGTH_RATIO = 0.82

    def __init__(self, fetcher: PublicArticleFetcher | None = None):
        self.fetcher = fetcher or PublicArticleFetcher()
        self.report: list[dict] = []
        self.cache: dict[str, FetchOutcome] = {}

    @staticmethod
    def _title_tokens(text: str | None) -> list[str]:
        text = re.sub(r"\bdata\s+stampa\s*\d+(?:\s*[-–]\s*data\s+stampa\s*\d+)?", " ", text or "", flags=re.I)
        return [t for t in re.findall(r"[a-zà-ÿ0-9]+", text.casefold()) if len(t) >= 3]

    @classmethod
    def title_similarity(cls, expected: str | None, found: str | None) -> float:
        a, b = cls._title_tokens(expected), cls._title_tokens(found)
        if len(a) < 3 or len(b) < 3:
            return 0.0
        a_set, b_set = set(a), set(b)
        # Recall in the shorter title supports short/long variants of a headline.
        overlap = len(a_set & b_set) / max(1, min(len(a_set), len(b_set)))
        sequence = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
        return round(max(0.65 * overlap + 0.35 * sequence, sequence), 4)

    @staticmethod
    def _main_titles(page: PageRecord) -> list[Region]:
        return [
            r for r in page.regions
            if r.type == RegionType.ARTICLE_TITLE and r.article_id
            and r.text and not r.exclude_from_article_text
            and r.metadata.get("title_role") not in {"teaser", "rejected_non_main", "not_main"}
            and r.metadata.get("content_scope") not in {"non_main", "navigation", "related"}
        ]

    @staticmethod
    def _url_candidates(page: PageRecord) -> list[str]:
        if page.page_type != PageType.WEB and page.source.type != SourceType.WEB:
            return []
        values = [page.source.url] if page.source.url else []
        values += [
            r.metadata.get("article_url_candidate")
            for r in page.regions
            if r.metadata.get("article_url_candidate")
        ]
        return list(dict.fromkeys(
            value for value in values if isinstance(value, str) and value.strip()
        ))

    @staticmethod
    def _url_matches_expected(value: str, other: str) -> bool:
        norm = PublicArticleFetcher.normalized_url
        return norm(value) is not None and norm(value) == norm(other)

    def _candidates(self, pages: list[PageRecord], records: list[ArticleRecord]) -> list[dict]:
        record_by_key = {(r.document_id, r.id): r for r in records}
        grouped: dict[tuple[str, str], dict] = {}
        for page in pages:
            urls = self._url_candidates(page)
            if not urls:
                continue
            titles = self._main_titles(page)
            owners = {title.article_id for title in titles}
            # No title, or several titles -> URL ownership is ambiguous.
            if len(owners) != 1:
                continue
            owner = next(iter(owners))
            title = next(t for t in titles if t.article_id == owner)
            key = (page.document_id, owner)
            entry = grouped.setdefault(key, {
                "page": page, "title": title.text, "urls": [],
                "record": record_by_key.get(key),
            })
            entry["urls"].extend(urls)
        result = []
        for (doc, owner), entry in grouped.items():
            record = entry["record"]
            candidate_urls = list(dict.fromkeys(
                u for u in entry["urls"]
                if PublicArticleFetcher.normalized_url(u)
            ))
            if record and record.url:
                # Prefer the authoritative record URL only if it matches the page URL.
                matching = [u for u in candidate_urls if self._url_matches_expected(u, record.url)]
                candidate_urls = matching or []
            if len(candidate_urls) != 1:
                self.report.append({"document_id": doc, "article_id": owner, "status": "skipped", "reason": "ambiguous_or_invalid_urls", "candidate_count": len(candidate_urls)})
                continue
            if record and record.article_type != "web":
                continue
            result.append({"document_id": doc, "article_id": owner, **entry, "url": candidate_urls[0]})
        return result

    def _quality_reason(self, outcome: FetchOutcome, expected_title: str, pdf_text: str) -> tuple[str | None, float]:
        if outcome.status != "extracted":
            return outcome.reason or outcome.status, 0.0
        words = len(re.findall(r"\b\w+\b", outcome.text))
        if len(outcome.text) < self.MIN_CHARS or words < self.MIN_WORDS:
            return "insufficient_public_body", 0.0
        score = self.title_similarity(expected_title, outcome.title)
        if score < self.TITLE_SIMILARITY_THRESHOLD:
            return "title_mismatch", score
        if len(pdf_text.strip()) >= 800:
            if len(outcome.text) < self.MIN_PDF_LENGTH_RATIO * len(pdf_text.strip()):
                return "web_body_shorter_than_pdf_threshold", score
        return None, score

    @staticmethod
    def _apply(record: ArticleRecord, outcome: FetchOutcome, score: float) -> None:
        record.body_pdf_raw = record.body_raw
        record.body_pdf_clean = record.body_clean or record.body
        record.body_raw = outcome.text
        record.body = outcome.text
        record.body_clean = outcome.text
        record.url = outcome.resolved_url or outcome.url
        record.article_type = "web"
        record.source.source_type = SourceType.WEB
        record.extraction.method = "web_html"
        record.extraction.confidence = None   # do not invent a calibrated confidence
        record.extraction.warnings = ["Web extraction quality is heuristic, not calibrated"]
        record.extraction_confidence = None
        record.field_origins["body"] = "public_web_html_trafilatura"
        record.field_origins["url"] = "web_retrieval"
        if not record.publication_date and outcome.published:
            try:
                record.publication_date = date.fromisoformat(outcome.published[:10])
                record.field_origins["publication_date"] = "web_html_metadata"
            except ValueError:
                pass
        if not record.authors and outcome.authors:
            record.authors = outcome.authors
            record.field_origins["authors"] = "web_html_metadata"
        record.provenance.append(Provenance(
            page=record.pdf_pages[0],
            content_type="web_article_body",
            method="trafilatura_public_html",
            source_refs=[{"url": outcome.resolved_url or outcome.url,
                            "http_status": outcome.http_status,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "text_char_count": len(outcome.text)}],
        ))
        record.web_retrieval = {
            "status": "accepted", "url": outcome.url,
            "resolved_url": outcome.resolved_url,
            "http_status": outcome.http_status, "extractor": "trafilatura",
            "title_similarity": score, "body_chars": len(outcome.text),
            "pdf_body_chars": len(record.body_pdf_raw or ""),
            "pdf_body_preserved_in": "body_pdf_raw",
            "article_content_charspans_reference": "body_pdf_raw",
        }
        # Never reuse PDF completeness/reading confidence as proof of web content.
        record.finalization_status = "review_required"
        record.finalization_warnings = list(dict.fromkeys([
            *record.finalization_warnings,
            "Web body selected; source HTML and URL require verification",
            "PDF segment offsets refer to body_pdf_raw, not web body_raw",
        ]))

    @staticmethod
    def _new_web_record(item: dict, outcome: FetchOutcome) -> ArticleRecord:
        page: PageRecord = item["page"]
        title: Region | None = next(
            (r for r in page.regions if r.type == RegionType.ARTICLE_TITLE
                and r.article_id == item["article_id"] and not r.exclude_from_article_text), None)
        return ArticleRecord(
            id=item["article_id"], document_id=item["document_id"],
            source=ArticleSource(source_type=SourceType.WEB, source=page.source.name),
            publication_date=page.source.publication_date,
            title=item["title"], pdf_pages=[page.pdf_page],
            original_pages=[page.source.original_page] if page.source.original_page else [],
            url=item["url"],
            article_type="web", body_raw="", body="", body_clean="",
            extraction=ExtractionInfo(method="web_html"),
            provenance=[Provenance(
                page=page.pdf_page, content_type="article_title",
                region_id=title.metadata.get("region_id") if title else None,
                extraction_method=title.extraction_method if title else "unknown",
                bbox=title.bbox if title else None,
                source_refs=title.provenance if title else [],
            )],
            extraction_completeness=ExtractionCompleteness(
                status="unknown", reasons=["web_only_no_pdf_body_record"]
            ),
            finalization_status="review_required",
            finalization_warnings=["Article recovered from web-only title anchor; PDF body not assembled"],
        )

    def enrich(self, pages: list[PageRecord], records: list[ArticleRecord]) -> list[ArticleRecord]:
        self.report = []
        self.cache = {}
        candidates = self._candidates(pages, records)
        # If two distinct article identities claim the same URL, never select it.
        urls_to_owners: dict[str, set[tuple[str, str]]] = {}
        for item in candidates:
            normalized = PublicArticleFetcher.normalized_url(item["url"])
            urls_to_owners.setdefault(normalized, set()).add((item["document_id"], item["article_id"]))
        for item in candidates:
            url = item["url"]
            key = PublicArticleFetcher.normalized_url(url)
            report = {"document_id": item["document_id"], "article_id": item["article_id"], "url": url}
            if len(urls_to_owners[key]) != 1:
                self.report.append({**report, "status": "skipped", "reason": "url_claimed_by_multiple_articles"})
                continue
            if key not in self.cache:
                self.cache[key] = self.fetcher.fetch(url)
            outcome = self.cache[key]
            record = item["record"]
            title = record.title if record and record.title else item["title"]
            pdf_body = record.body_pdf_clean or record.body if record else ""
            reason, score = self._quality_reason(outcome, title, pdf_body)
            if reason:
                details = {**report, "status": "pdf_fallback" if record else "web_unavailable",
                            "retrieval_status": outcome.status, "reason": reason,
                            "http_status": outcome.http_status, "title_similarity": score,
                            "web_body_chars": len(outcome.text)}
                self.report.append(details)
                if record:
                    record.web_retrieval = details
                continue
            if record is None:
                record = self._new_web_record(item, outcome)
                records.append(record)
            self._apply(record, outcome, score)
            self.report.append({**report, "status": "web_selected", "http_status": outcome.http_status,
                                "title_similarity": score, "body_chars": len(outcome.text),
                                "pdf_body_chars": len(record.body_pdf_raw or "")})
        return records