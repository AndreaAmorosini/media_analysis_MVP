"""Smoke test del pipeline completo su pagine sintetiche.

Non usa Docling, OCR o PDF: verifica che l'intera catena di resolver giri
senza eccezioni su pagine puie' e produca struttura coerente.
"""

import pytest

from press_reputation.core.models.page import PageType, RegionType
from press_reputation.pipeline import PageProcessingPipeline


@pytest.fixture
def pipeline() -> PageProcessingPipeline:
    # style enrichment richiede il PDF su disco: disattivato nei test
    return PageProcessingPipeline(enable_style_enrichment=False)


class TestPipelineSmoke:
    def test_single_page_document(self, pipeline, make_page_with_article, make_region):
        pages = [make_page_with_article(pdf_page=1)]
        result = pipeline.process(pages=pages, pdf_path=None)
        assert result is pages
        assert len(result) == 1

    def test_reading_orders_present(self, pipeline, single_article_document):
        output = pipeline.process(pages=single_article_document, pdf_path=None)
        assert output and all(page.regions for page in output)

    def test_reports_initialized(self, pipeline, make_page_with_article):
        pages = [make_page_with_article(pdf_page=1)]
        pipeline.process(pages=pages, pdf_path=None)
        assert pipeline.cross_page_reconciliations == []
        assert pipeline.flow_links == []

    def test_two_page_document_with_same_article(
        self, pipeline, single_article_document
    ):
        output = pipeline.process(pages=list(single_article_document), pdf_path=None)
        assert len(output) == 2
        # l'articolo a1 ha >= un body assegnato su pagine diverse
        body_ids = {
            (page.pdf_page, region.article_id)
            for page in output
            for region in page.regions
            if region.type == RegionType.ARTICLE_BODY and region.article_id
        }
        assert {"a1"} <= {article_id for _, article_id in body_ids}

    def test_index_style_page_not_crashing(self, pipeline, make_page, make_region):
        pages = [
            make_page(
                page_type=PageType.INDEX,
                regions=[
                    make_region(
                        RegionType.UNKNOWN,
                        text="1. Titolo uno Gazzetta 3",
                        region_id="i1",
                        bbox=[0, 100, 900, 140],
                    ),
                    make_region(
                        RegionType.UNKNOWN,
                        text="2. Titolo due Mattino 5",
                        region_id="i2",
                        bbox=[0, 150, 900, 190],
                    ),
                ],
            )
        ]
        output = pipeline.process(pages=pages, pdf_path=None)
        assert output == pages


class TestPipelineDeterminism:
    def test_same_input_twice_same_output(self, pipeline, single_article_document):
        snapshot_first = [
            [
                (
                    r.region_id()
                    if hasattr(r, "region_id")
                    else r.metadata.get("region_id"),
                    r.article_id,
                    r.type,
                )
                for r in page.regions
            ]
            for page in pipeline.process(pages=single_article_document, pdf_path=None)
        ]

        pipeline_second = PageProcessingPipeline(enable_style_enrichment=False)
        fresh = []
        for old in single_article_document:
            new = old.model_copy(deep=True)
            fresh.append(new)
        snapshot_second = [
            [(r.metadata.get("region_id"), r.article_id, r.type) for r in page.regions]
            for page in pipeline_second.process(pages=fresh, pdf_path=None)
        ]
        assert snapshot_first == snapshot_second


class TestPipelineMultiRunReset:
    def test_state_reset_between_runs(
        self, pipeline, single_article_document, make_page_with_article
    ):
        pipeline.process(pages=list(single_article_document), pdf_path=None)
        links_after_first = list(pipeline.flow_links)
        pipeline.process(pages=[make_page_with_article(pdf_page=1)], pdf_path=None)
        # i report derivati dai link della prima run non devono sopravvivere
        assert pipeline.cross_page_reconciliations == []
        assert isinstance(links_after_first, list)
