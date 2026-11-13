"""Test di reconstruction/: body grouping, reading order, clustering, assembler."""
import pytest

from press_reputation.models.page import RegionType
from press_reputation.reconstruction.body_grouping import (
    BodyGroupingResolver,
    horizontal_overlap,
    valid_bbox,
)
from press_reputation.reconstruction.article_reading_order import (
    ArticleReadingOrderResolver,
)
from press_reputation.reconstruction.article_draft_assembler import (
    ArticleDraftAssembler,
)


class TestGeometryHelpers:
    def test_valid_bbox(self, make_region):
        from press_reputation.models.page import Region

        assert valid_bbox(make_region(bbox=[0, 0, 10, 10]))
        assert not valid_bbox(Region(bbox=None))
        assert not valid_bbox(make_region(bbox=[0, 0, 0, 10]))  # area nulla

    def test_horizontal_overlap_disjoint(self):
        assert horizontal_overlap([0.0, 0.0, 10.0, 1.0], [20.0, 0.0, 30.0, 1.0]) == 0.0

    def test_horizontal_overlap_partial(self):
        score = horizontal_overlap([0.0, 0.0, 10.0, 1.0], [5.0, 0.0, 15.0, 1.0])
        assert score == pytest.approx(0.5)


class TestBodyGrouping:
    def test_single_column_group(self, make_two_column_page):
        page = make_two_column_page()
        BodyGroupingResolver().enrich(page)
        bodies = [r for r in page.regions if r.type == RegionType.ARTICLE_BODY]
        assert all(r.metadata.get("body_group_id") for r in bodies)
        orders = sorted(r.metadata["body_reading_order"] for r in bodies)
        assert orders == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_left_column_before_right(self, make_two_column_page):
        page = make_two_column_page()
        BodyGroupingResolver().enrich(page)
        left_ids = {r.metadata["region_id"] for r in page.regions if "_L" in r.metadata["region_id"]}
        right_ids = {r.metadata["region_id"] for r in page.regions if "_R" in r.metadata["region_id"]}
        orders = {
            r.metadata["region_id"]: r.metadata["body_reading_order"]
            for r in page.regions if r.type == RegionType.ARTICLE_BODY
        }
        assert max(orders[r] for r in left_ids) < min(orders[r] for r in right_ids)

    def test_different_article_ids_in_separate_columns(self, make_page, make_region):
        left = make_region(
            RegionType.ARTICLE_BODY, text="A", bbox=[100, 220, 500, 240], article_id="a1", region_id="L1"
        )
        right = make_region(
            RegionType.ARTICLE_BODY, text="B", bbox=[520, 220, 900, 240], article_id="a2", region_id="R1"
        )
        page = make_page(regions=[left, right])
        BodyGroupingResolver().enrich(page)
        assert page.regions[0].metadata["body_group_id"] != page.regions[1].metadata["body_group_id"]

    def test_rerun_is_idempotent_on_metadata(self, make_two_column_page):
        page = make_two_column_page()
        BodyGroupingResolver().enrich(page)
        first = [r.metadata.get("body_reading_order") for r in page.regions]
        BodyGroupingResolver().enrich(page)
        second = [r.metadata.get("body_reading_order") for r in page.regions]
        assert first == second


class TestReadingOrder:
    def test_resolve_produces_orders(self, make_two_column_page):
        resolver = ArticleReadingOrderResolver()
        orders = resolver.resolve([make_two_column_page()])
        assert isinstance(orders, dict) or isinstance(orders, list)

    def test_detect_columns_two_columns(self, make_two_column_page):
        resolver = ArticleReadingOrderResolver()
        columns = resolver.detect_columns(make_two_column_page().regions)
        assert len(columns) == 2

    def test_column_boxes_shape(self, make_two_column_page):
        resolver = ArticleReadingOrderResolver()
        columns = resolver.detect_columns(make_two_column_page().regions)
        boxes = resolver.column_boxes(columns)
        assert all(len(box) == 4 for box in boxes)
        # colonne ordinate da sinistra a destra
        assert boxes[0][0] < boxes[-1][0]


class TestDraftAssembler:
    def test_assemble_single_article(self, single_article_document):
        pages = single_article_document
        reading_order = ArticleReadingOrderResolver().resolve(pages)
        assembler = ArticleDraftAssembler()
        drafts = assembler.assemble(pages=pages, links=[], reading_orders=reading_order)
        assert len(drafts) >= 1
        draft = next(d for d in drafts)
        assert draft.document_id == "test.pdf"

    def test_assembler_produces_title(self, single_article_document):
        pages = single_article_document
        drafts = ArticleDraftAssembler().assemble(
            pages=pages,
            links=[],
            reading_orders=ArticleReadingOrderResolver().resolve(pages),
        )
        titled = [d for d in drafts if d.title]
        assert titled, "atteso almeno un draft con titolo"
