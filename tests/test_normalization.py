"""Test di normalization/: title_cleanup, watermark_scrub, text_fragment."""

import pytest

from press_reputation.core.models.page import RegionType
from press_reputation.ingestion.normalization.text_fragment import TextFragmentRecovery
from press_reputation.ingestion.normalization.title_cleanup import (
    clean_article_title,
    unwrap_fused_codes,
)
from press_reputation.ingestion.normalization.watermark_scrub import scrub_watermark_residue

# ---------------------------------------------------------------------------
# title_cleanup
# ---------------------------------------------------------------------------


class TestTitleCleanup:
    def test_unwrap_leading_fused_code(self):
        # prefisso lettere+cifre fused a una maiuscola: "DS16Tornano"
        result = unwrap_fused_codes("DS16Tornano il mercato")
        assert "DS16" not in result
        assert "Tornano" in result

    def test_unwrap_trailing_fused_code(self):
        # codice in coda a parola minuscola: "ImprenditoreS1667"
        result = unwrap_fused_codes("l'ImprenditoreS1667 torna")
        assert "S1667" not in result

    def test_embedded_digits_within_word(self):
        result = unwrap_fused_codes("FOSSOIMPERATORE667RESIDENTI")
        # le cifre incunate fra lettere vengono rimosse
        assert "667" not in result

    def test_clean_article_title_strips_codes(self):
        titolo = "DS1667 Il bilancio approvato DS1667 DS1667"
        cleaned, actions = clean_article_title(titolo)
        assert "DS1667" not in cleaned
        assert any(word.isalpha() for word in cleaned.split())
        assert isinstance(actions, list)


# ---------------------------------------------------------------------------
# watermark_scrub
# ---------------------------------------------------------------------------


class TestWatermarkScrub:
    def test_code_token_removed_from_body(self, make_region, make_page):
        watermark = make_region(
            RegionType.WATERMARK,
            text="DS1667",
            bbox=[10.0, 10.0, 60.0, 60.0],
            region_id="wm",
        )
        body = make_region(
            RegionType.ARTICLE_BODY,
            text="Il futuroDS1667 della citta' resta aperto.",
            article_id="a1",
            region_id="b1",
        )
        pages = [make_page(regions=[watermark, body])]
        scrub_watermark_residue(pages)
        body_region = pages[0].regions[1]
        assert "DS1667" not in body_region.text
        # la pulizia e' conservativa: le azioni restano in metadata
        assert any("watermark" in (key or "") for key in body_region.metadata)

    def test_clean_body_untouched(self, make_region, make_page):
        body = make_region(
            RegionType.ARTICLE_BODY,
            text="Testo pulito senza residui.",
            article_id="a1",
            region_id="b1",
        )
        pages = [make_page(regions=[body])]
        scrub_watermark_residue(pages)
        assert pages[0].regions[0].text == "Testo pulito senza residui."

    def test_rights_phrase_scrubbed(self, make_region, make_page):
        rights = make_region(
            RegionType.RIGHTS_NOTICE, text="© RIPRODUZIONE RISERVATA", region_id="rt"
        )
        body = make_region(
            RegionType.ARTICLE_BODY,
            text="Paragrafo finale ©riproduzioneriservata",
            article_id="a1",
            region_id="b1",
        )
        pages = [make_page(regions=[rights, body])]
        scrub_watermark_residue(pages)
        assert "riproduzioneriservata" not in (pages[0].regions[1].text or "").replace(
            " ", ""
        )

    def test_title_residue_scrubbed(self, make_region, make_page):
        watermark = make_region(
            RegionType.WATERMARK,
            text="X99991",
            bbox=[0.0, 0.0, 40.0, 40.0],
            region_id="wm",
        )
        title = make_region(
            RegionType.ARTICLE_TITLE,
            text="Il bilancio X99991 approvato",
            region_id="t1",
        )
        pages = [make_page(regions=[watermark, title])]
        scrub_watermark_residue(pages)
        assert "X99991" not in (pages[0].regions[1].text or "")


# ---------------------------------------------------------------------------
# text_fragment
# ---------------------------------------------------------------------------


class TestTextFragmentRecovery:
    @pytest.fixture
    def recovery(self):
        return TextFragmentRecovery()

    def test_text_without_provenance(self, recovery):
        result = recovery.recover({"text": "qualcosa"})
        assert result.fragments == []
        assert result.unmapped_intervals == [(0, len("qualcosa"))]
        assert "text_item_without_provenance" in result.warnings

    def test_source_text_not_string(self, recovery):
        result = recovery.recover({"text": None, "prov": [{"charspan": [0, 3]}]})
        assert result.source_text is None
        assert result.fragments[0].status == "ambiguous"

    def test_exact_recover(self, recovery):
        item = {
            "text": "primo pezzo contenuto ignorato secondo pezzo",
            "prov": [
                {"charspan": [0, 11], "orig": "primo pezzo"},
                {"charspan": [31, 44], "orig": "secondo pezzo"},
            ],
        }
        result = recovery.recover(item)
        recovered = [fragment.text for fragment in result.fragments if fragment.text]
        assert any("primo pezzo" in fragment for fragment in recovered)
        assert any("secondo pezzo" in fragment for fragment in recovered)
        assert all(
            fragment.status == "exact" for fragment in result.fragments if fragment.text
        )

    def test_raw_span_invalid(self, recovery):
        assert recovery.raw_span({"charspan": "no"}) is None
        assert recovery.raw_span({"charspan": [0]}) is None
        assert recovery.raw_span({"charspan": [0, 1.5]}) is None

    def test_unmapped_intervals_gaps(self):
        intervals = TextFragmentRecovery.unmapped_intervals("abcdef", [(0, 2), (4, 6)])
        assert intervals == [(2, 4)]
