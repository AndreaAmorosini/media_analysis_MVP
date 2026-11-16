"""Test di features/region_features.py: flag testuali e geometria relativa."""

import pytest

from press_reputation.features import RegionFeatureExtractor


@pytest.fixture
def extractor() -> RegionFeatureExtractor:
    return RegionFeatureExtractor()


class TestTextFlags:
    def test_url_detection(self, extractor, make_region, make_page):
        region = make_region(text="Leggi su https://esempio.it/articolo")
        features = extractor.extract(region, make_page())
        assert features.has_url is True
        assert features.word_count >= 2

    def test_no_url(self, extractor, make_region, make_page):
        features = extractor.extract(make_region(text="Testo normale"), make_page())
        assert features.has_url is False

    def test_uppercase_ratio(self, extractor, make_region, make_page):
        features = extractor.extract(make_region(text="TUTTO MAIUSCOLO"), make_page())
        assert features.uppercase_ratio == pytest.approx(1.0)

    def test_metadata_region_markers(self, extractor, make_region, make_page):
        features = extractor.extract(
            make_region(text="Tiratura 10.000 copie, superficie 5 mq"),
            make_page(),
        )
        assert features.has_tiratura is True
        assert features.has_surface is True

    def test_empty_text_is_safe(self, extractor, make_region, make_page):
        features = extractor.extract(make_region(text=None), make_page())
        assert features.text_length == 0
        assert features.word_count == 0


class TestGeometry:
    def test_bbox_relatives(self, extractor, make_region, make_page):
        region = make_region(bbox=[700.0, 700.0, 900.0, 1300.0])
        features = extractor.extract(region, make_page(width=1000.0, height=1400.0))
        assert features.relative_x0 == pytest.approx(0.7)
        assert features.relative_y0 == pytest.approx(0.5)
        assert features.is_right_area is True

    def test_top_area(self, extractor, make_region, make_page):
        features = extractor.extract(
            make_region(bbox=[0.0, 0.0, 900.0, 100.0]), make_page()
        )
        assert features.is_top_area is True

    def test_margin_top_bottom(self, extractor, make_region, make_page):
        features = extractor.extract(
            make_region(bbox=[0.0, 0.0, 900.0, 100.0]), make_page()
        )
        assert features.is_top_area ^ features.is_bottom_area or (
            features.is_top_area and not features.is_bottom_area
        )


class TestEntityFeature:
    def test_without_entity(self, extractor, make_region, make_page):
        features = extractor.extract(
            make_region(text="generico testo"), make_page(), include_entity=False
        )
        assert features.entity_kind is None
        assert features.municipalities == []
