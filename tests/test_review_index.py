"""Test di review_index/: similarita' titoli, parser delle righe e matcher."""

from datetime import date

import pytest

from press_reputation.core.models.page import PageType, RegionType
from press_reputation.review_index.matcher import (
    ReviewIndexMatcher,
    normalized_title,
    squeezed_title,
    title_similarity,
)
from press_reputation.review_index.models import ReviewIndexEntry
from press_reputation.review_index.parser import ReviewIndexParser

DOC_ID = "test.pdf"


def index_entry(**overrides) -> ReviewIndexEntry:
    values = dict(
        id="e1",
        document_id=DOC_ID,
        publication_date=date(2024, 1, 17),
        source="Gazzetta",
        original_page=5,
        title="Riapre la fognaria nel centro",
        author=None,
        review_start_page=7,
        index_pdf_page=1,
        table_ref="#/tables/0",
        row_number=1,
    )
    values.update(overrides)
    return ReviewIndexEntry(**values)


def raw_index_document() -> dict:
    """Struttura Docling grezza: una tabella 'document_index' a 5 colonne."""
    return {
        "tables": [
            {
                "label": "document_index",
                "self_ref": "#/tables/0",
                "prov": [{"page_no": 1}],
                "data": {
                    "num_cols": 5,
                    "grid": [
                        [
                            {"text": "17/01/2024"},
                            {"text": "Gazzetta"},
                            {"text": "5 Riapre la fognaria nel centro"},
                            {"text": ""},
                            {"text": "7"},
                        ],
                        [
                            {"text": "17/01/2024"},
                            {"text": "Mattino"},
                            {"text": "12 Bilancio consuntivo approvato"},
                            {"text": "Rossi"},
                            {"text": "9"},
                        ],
                    ],
                },
            }
        ]
    }


class TestTitleFunctions:
    def test_normalized_title(self):
        assert normalized_title("Città di TAVerna") == "citta di taverna"

    def test_squeezed_title(self):
        assert squeezed_title("Foo-Bar Baz!") == "foobarbaz"

    def test_similarity_identical(self):
        assert title_similarity(
            "Riapre la fognaria", "Riapre la fognaria"
        ) == pytest.approx(1.0)

    def test_similarity_ocr_fused_form(self):
        score = title_similarity(
            "Riapre la fognaria in centro", "riaprelafognariaincentro"
        )
        assert score > 0.85

    def test_similarity_empty(self):
        assert title_similarity("", "qualcosa") == 0.0

    def test_similarity_unrelated(self):
        assert title_similarity("Riapre la fognaria", "Bilancio consuntivo 2024") < 0.4


class TestReviewIndexParser:
    def test_parses_rows(self):
        entries = ReviewIndexParser().parse(raw_index_document(), document_id=DOC_ID)
        assert len(entries) == 2
        titles = {entry.title for entry in entries}
        assert titles == {
            "Riapre la fognaria nel centro",
            "Bilancio consuntivo approvato",
        }

    def test_row_fields(self):
        entries = ReviewIndexParser().parse(raw_index_document(), document_id=DOC_ID)
        first = entries[0]
        assert first.source == "Gazzetta"
        assert first.original_page == 5
        assert first.review_start_page == 7
        assert first.publication_date == date(2024, 1, 17)

    def test_document_id_propagated(self):
        for entry in ReviewIndexParser().parse(
            raw_index_document(), document_id=DOC_ID
        ):
            assert entry.document_id == DOC_ID

    def test_table_without_label_skipped(self):
        document = raw_index_document()
        document["tables"][0]["label"] = "data"
        assert ReviewIndexParser().parse(document, document_id=DOC_ID) == []

    def test_wrong_column_count_skipped(self):
        document = raw_index_document()
        document["tables"][0]["data"]["num_cols"] = 4
        assert ReviewIndexParser().parse(document, document_id=DOC_ID) == []

    def test_title_too_short_skipped(self):
        document = raw_index_document()
        document["tables"][0]["data"]["grid"][0][2]["text"] = "5 Va"
        entries = ReviewIndexParser().parse(document, document_id=DOC_ID)
        assert all(entry.title != "Va" for entry in entries)


class TestReviewIndexMatcher:
    @pytest.fixture
    def entry(self) -> ReviewIndexEntry:
        return index_entry()

    def article_page(
        self, entry, make_page, make_region, title="RIAPRE LA FOGNARIA NEL CENTRO"
    ):
        return make_page(
            pdf_page=2,
            regions=[
                make_region(
                    type=RegionType.ARTICLE_TITLE,
                    text=title,
                    region_id="t1",
                    page=2,
                ),
                make_region(
                    type=RegionType.ARTICLE_BODY,
                    text="Body della notizia.",
                    article_id="a1",
                    region_id="b1",
                    page=2,
                ),
            ],
            source_name=entry.source,
            publication_date=entry.publication_date,
            original_page=entry.original_page,
        )

    def test_full_evidence_match(self, entry, make_page, make_region):
        index_page = make_page(
            pdf_page=1,
            page_type=PageType.INDEX,
            regions=[
                make_region(
                    type=RegionType.ARTICLE_TITLE,
                    text=entry.title.upper(),
                    region_id="idx1",
                    page=1,
                )
            ],
        )
        article_page = self.article_page(entry, make_page, make_region)
        matches = ReviewIndexMatcher().match([index_page, article_page], [entry])
        assert matches, (
            "atteso match con titolo, testata, data e pagina originali allineati"
        )
        best = matches[0]
        assert best.title_similarity >= 0.99
        assert best.score > 0.72
        assert {"same_publication_date", "same_source", "same_original_page"} <= set(
            best.evidence
        )

    def test_ocr_noise_still_matches_with_strong_evidence(self, make_page, make_region):
        entry = index_entry(title="Scafati, riapre la fognaria")
        article_page = make_page(
            pdf_page=2,
            regions=[
                make_region(
                    type=RegionType.ARTICLE_TITLE,
                    text="SCAFATI RIAPRELA FOGNARIA",
                    region_id="t1",
                    page=2,
                ),
            ],
            source_name=entry.source,
            publication_date=entry.publication_date,
            original_page=entry.original_page,
        )
        matches = ReviewIndexMatcher().match([article_page], [entry])
        assert matches and matches[0].title_similarity > 0.6

    def test_unrelated_title_not_matched(self, entry, make_page, make_region):
        article_page = make_page(
            pdf_page=2,
            regions=[
                make_region(
                    type=RegionType.ARTICLE_TITLE,
                    text="BILANCIO CONSUNTIVO",
                    region_id="t1",
                    page=2,
                )
            ],
            source_name=entry.source,
            publication_date=entry.publication_date,
            original_page=entry.original_page,
        )
        matches = ReviewIndexMatcher().match([article_page], [entry])
        assert all(match.status != "matched" for match in matches)
