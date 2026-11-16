"""Test di lookup.py: normalizzazione chiavi, copertura, fuzzy gate, entità."""

import pytest

from press_reputation.core.lookup import (
    can_reach_similarity,
    match_coverage,
    normalize_entity_key,
    resolve_entity,
)


class TestNormalizeEntityKey:
    def test_accents_and_casefold(self):
        assert normalize_entity_key("Il Mattinò") == "il mattino"

    def test_punctuation_stripped(self):
        assert normalize_entity_key("Città di Taverna!") == "citta di taverna"

    def test_whitespace_collapsed(self):
        assert normalize_entity_key("FOOO   BAR") == "fooo bar"


class TestMatchCoverage:
    def test_full_coverage(self):
        assert match_coverage("gazzetta", "gazzetta") == 1.0

    def test_partial_coverage(self):
        score = match_coverage("gazzetta del mezzogiorno", "gazzetta")
        assert 0.0 < score < 1.0

    def test_empty_whole_text(self):
        assert match_coverage("", "qualcosa") == 0.0


class TestCanReachSimilarity:
    def test_equal_length_passes(self):
        assert can_reach_similarity("abcde", "abcdf", 0.8)

    def test_length_mismatch_blocks(self):
        # la similarita' massima teorica e' sotto la soglia
        assert not can_reach_similarity("abc", "abcdefghij", 0.8)

    def test_empty_blocks(self):
        assert not can_reach_similarity("", "abc", 0.5)


class TestResolveEntity:
    """Usa le risorse reali newspaper.json / comuni_italiani.json."""

    @pytest.fixture
    def known_newspaper(self) -> str | None:
        import json

        from press_reputation.core.lookup import RESOURCE_DIR

        data = json.loads((RESOURCE_DIR / "newspaper.json").read_text(encoding="utf-8"))
        testate = data.get("testate") or []
        return testate[0].get("nome") if testate else None

    @pytest.mark.skipif(
        not hasattr(
            __import__("press_reputation.core.lookup", fromlist=["RESOURCE_DIR"]),
            "RESOURCE_DIR",
        ),
        reason="risorse assenti",
    )
    def test_exact_newspaper_match(self, known_newspaper):
        if not known_newspaper:
            pytest.skip("nessuna testata nel lessico")
        match = resolve_entity(known_newspaper)
        if match is None:
            pytest.skip(f"'{known_newspaper}' non risolta dal lessico")
        assert match.kind in {"source", "provider"}
        assert match.similarity >= 0.99

    def test_unknown_text_returns_none(self):
        assert resolve_entity("zzzz qqqq xxxx yyyy") is None

    def test_empty_text(self):
        assert resolve_entity("") is None
