"""Test di classification/: statici euristici e resolver locali."""

from press_reputation.enrichment.subtitle_resolver import SubtitleResolver
from press_reputation.classification.technical_region_classifier import (
    TechnicalRegionClassifier,
)
from press_reputation.enrichment.title_resolver import TitleResolver
from press_reputation.classification.web.url_harvester import (
    has_path,
    normalize_candidate,
    unwrap_urls,
)
from press_reputation.core.models.page import RegionType


class TestStampCodeText:
    def test_pure_stamp_code(self):
        assert TitleResolver.is_stamp_code_text("DS1667")

    def test_duplicated_stamp_code(self):
        assert TitleResolver.is_stamp_code_text("DS1667 DS1667")

    def test_editorial_text_not_stamp(self):
        assert not TitleResolver.is_stamp_code_text("Miasmi DS1667 dal Sarno")

    def test_empty_not_stamp(self):
        assert not TitleResolver.is_stamp_code_text("")


class TestChromeText:
    def test_empty_is_chrome(self):
        assert SubtitleResolver.is_chrome_text("")

    def test_single_word_is_chrome(self):
        assert SubtitleResolver.is_chrome_text("menu")

    def test_private_use_glyphs_are_chrome(self):
        assert SubtitleResolver.is_chrome_text("\ue00b\ue00c\ue00d")

    def test_normal_subtitle_not_chrome(self):
        assert not SubtitleResolver.is_chrome_text("Riapre la fognaria nel centro")

    def test_publication_date_is_chrome(self):
        assert SubtitleResolver.is_chrome_text("Lunedì 15 gennaio 2024")

    def test_author_byline_is_chrome(self):
        assert SubtitleResolver.is_chrome_text("author: Giovanni Rossi")


class TestUrlHarvesterHelpers:
    def test_unwrap_single_url(self):
        assert unwrap_urls("vai su https://esempio.it/articolo-123 ok") == [
            "https://esempio.it/articolo-123"
        ]

    def test_unwrap_wrapped_url(self):
        # URL spezzato a fine riga sull'ultimo carattere utile
        text = "https://esempio.it/\narticolo-123"
        result = unwrap_urls(text)
        assert any("articolo-123" in url for url in result)

    def test_no_url(self):
        assert unwrap_urls("nessun link in questa riga") == []

    def test_normalize_www_prefix(self):
        assert normalize_candidate("www.esempio.it/a") == "https://www.esempio.it/a"

    def test_normalize_rejects_credentials(self):
        assert normalize_candidate("https://user:pass@esempio.it") is None

    def test_normalize_rejects_non_http(self):
        assert normalize_candidate("ftp://esempio.it/file") is None

    def test_has_path(self):
        assert has_path("https://esempio.it/articolo")
        assert not has_path("https://esempio.it/")


class TestTechnicalRegionClassifier:
    def _enrich(self, regions):
        page = make_page_simple(regions)
        result = TechnicalRegionClassifier().enrich(page, document_page_count=1)
        return result if result is not None else page

    def test_enrich_runs_and_returns_page(self, make_region, make_page):
        page = make_page(
            regions=[
                make_region(
                    RegionType.UNKNOWN,
                    text="Tiratura: 12.000",
                    bbox=[0.0, 0.0, 900.0, 60.0],
                    region_id="hd",
                ),
            ]
        )
        result = TechnicalRegionClassifier().enrich(page, document_page_count=1)
        assert result is page or result is None


class TestTitleResolverEligibility:
    def test_eligible_basic_title(self, make_region):
        resolver = TitleResolver()
        region = make_region(
            RegionType.ARTICLE_TITLE,
            text="RIAPRE LA FOGNARIA",
            bbox=[100.0, 80.0, 900.0, 130.0],
        )
        assert resolver.eligible(region) is True

    def test_blocked_by_content_scope(self, make_region):
        resolver = TitleResolver()
        region = make_region(
            RegionType.ARTICLE_TITLE,
            text="Consigliati per te",
            region_id="r2",
        )
        region.metadata["content_scope"] = "related"
        assert resolver.eligible(region) is False

    def test_excluded_from_article_text_blocked(self, make_region):
        resolver = TitleResolver()
        region = make_region(RegionType.ARTICLE_TITLE, text="Classificati 2024")
        region.exclude_from_article_text = True
        assert resolver.eligible(region) is False

    def test_invalid_bbox_blocked(self, make_region):
        resolver = TitleResolver()
        region = make_region(
            RegionType.ARTICLE_TITLE, text="Titolo", bbox=[0.0, 0.0, 0.0, 0.0]
        )
        assert resolver.eligible(region) is False
