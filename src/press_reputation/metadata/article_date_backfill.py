"""Conservative publication-date backfill from article-member page metadata.

This is post-finalization; does not depend on unambiguous_pages(), which may
exclude pages carrying two headline candidates.
"""
from __future__ import annotations

from press_reputation.models.article import ArticleRecord
from press_reputation.models.page import PageRecord
from press_reputation.review_index.models import ReviewIndexEntry


class ArticlePublicationDateBackfill:
    def enrich(self, records: list[ArticleRecord], pages: list[PageRecord],
                index_entries: list[ReviewIndexEntry] | None = None) -> None:
        for record in records:
            if record.publication_date:
                continue
            members = [p for p in pages if p.document_id==record.document_id and
                        p.pdf_page in record.pdf_pages]
            dates={p.source.publication_date for p in members if p.source.publication_date}
            if len(dates)==1:
                record.publication_date=next(iter(dates))
                record.field_origins['publication_date']='article_page_date_unanimous'
                record.finalization_warnings=[w for w in record.finalization_warnings
                    if w!='Publication date unavailable']
                continue
            # Do not guess if page dates conflict or are entirely absent.
            # The existing ArticleFinalizer uses strongly matched index metadata;
            # index_entries here are deliberately not used by loose title matching.