"""Test dei modelli Pydantic: PageRecord, Region, FlowLink, ArticleDraft."""
import pytest
from pydantic import ValidationError

from press_reputation.models.page import PageRecord, Region, RegionType, PageType


class TestRegion:
    def test_default_metadata_and_style_empty(self):
        region = Region()
        assert region.metadata == {}
        assert region.style == {}
        assert region.provenance == []
        assert region.boilerplate is False
        assert region.article_id is None

    def test_region_id_defaults_into_metadata(self, make_region):
        region = make_region(region_id="p1_r2")
        assert region.metadata["region_id"] == "p1_r2"

    def test_bbox_required_geometry(self, make_region):
        region = make_region(bbox=[1, 2, 3, 4])
        assert region.bbox == [1, 2, 3, 4]

    def test_text_can_be_none(self, make_region):
        assert make_region(text=None).text is None

    def test_type_must_be_valid_region_type(self, make_region):
        with pytest.raises(ValidationError):
            make_region(type="non_esiste")


class TestPageRecord:
    def test_minimal_page(self, make_page):
        page = make_page()
        assert page.page_type == PageType.CLIPPING
        assert page.regions == []
        assert page.page_width == 1000.0

    def test_document_id_required(self):
        with pytest.raises(ValidationError):
            PageRecord(pdf_page=1)

    def test_regions_round_trip(self, make_region, make_page):
        page = make_page(regions=[make_region(article_id="a7")])
        data = page.model_dump()
        assert data["regions"][0]["article_id"] == "a7"
        rebuilt = PageRecord.model_validate(data)
        assert rebuilt.regions[0].metadata["region_id"]


class TestEnumValuesStr:
    def test_region_type_serializes_as_value(self, make_region):
        assert make_region(type=RegionType.ARTICLE_TITLE).model_dump(mode="json")["type"] == "article_title"

    def test_page_type_membership(self):
        assert {p.value for p in PageType} >= {"clipping", "web", "index"}
