"""Regression tests per i moduli core di presskg.

Questi test catturano il comportamento attuale dei moduli fondamentali
(articles, ontology, entity_ontology, predicate_catalog, extraction_rules,
grounding) per garantire che il refactoring non introduca regressioni.
"""

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest


# ============================================================
# articles.py
# ============================================================

class TestArticles:
    """Test per presskg.ingestion.articles: lettura, hash, iterazione."""

    def test_short_hash_length_and_determinism(self):
        from presskg.ingestion.articles import short_hash
        h1 = short_hash("test")
        h2 = short_hash("test")
        h3 = short_hash("other")
        assert len(h1) == 24
        assert h1 == h2
        assert h1 != h3

    def test_short_hash_matches_sha256_prefix(self):
        from presskg.ingestion.articles import short_hash
        expected = hashlib.sha256("hello".encode("utf-8")).hexdigest()[:24]
        assert short_hash("hello") == expected

    def test_read_article_valid(self, sample_article_file, sample_data_dir):
        from presskg.ingestion.articles import read_article
        article = read_article(sample_article_file, sample_data_dir)
        assert article is not None
        assert article.id.startswith("article-")
        assert article.rel_path == "2026/article_0001.json"
        assert article.title == "Servizio idrico ripristinato"
        assert article.publication_date == "2026-01-15"
        assert article.source_name == "Il Giornale di Scafati"
        assert len(article.content_hash) == 64  # sha256 full

    def test_read_article_empty_body_returns_none(self, tmp_path):
        from presskg.ingestion.articles import read_article
        data_dir = tmp_path / "Data"
        data_dir.mkdir()
        f = data_dir / "empty.json"
        f.write_text(json.dumps({"body": "   "}), encoding="utf-8")
        assert read_article(f, data_dir) is None

    def test_read_article_body_as_list(self, tmp_path):
        from presskg.ingestion.articles import read_article
        data_dir = tmp_path / "Data"
        data_dir.mkdir()
        f = data_dir / "list.json"
        f.write_text(json.dumps({"body": ["riga uno", "riga due"]}), encoding="utf-8")
        article = read_article(f, data_dir)
        assert article is not None
        assert article.body == "riga uno\nriga due"

    def test_read_article_date_fallback(self, tmp_path):
        from presskg.ingestion.articles import read_article
        data_dir = tmp_path / "Data"
        data_dir.mkdir()
        f = data_dir / "date.json"
        f.write_text(json.dumps({"body": "test body", "date": "2025-12-01"}), encoding="utf-8")
        article = read_article(f, data_dir)
        assert article.publication_date == "2025-12-01"

    def test_read_article_non_dict_raises(self, tmp_path):
        from presskg.ingestion.articles import read_article
        data_dir = tmp_path / "Data"
        data_dir.mkdir()
        f = data_dir / "bad.json"
        f.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        with pytest.raises(ValueError, match="non è un oggetto"):
            read_article(f, data_dir)

    def test_iter_articles_sorted(self, tmp_path):
        from presskg.ingestion.articles import iter_articles
        data_dir = tmp_path / "Data"
        (data_dir / "b").mkdir(parents=True)
        (data_dir / "a").mkdir(parents=True)
        (data_dir / "b" / "z.json").write_text(json.dumps({"body": "z"}), encoding="utf-8")
        (data_dir / "a" / "y.json").write_text(json.dumps({"body": "y"}), encoding="utf-8")
        paths = [p for p, _ in iter_articles(data_dir)]
        assert paths == sorted(paths)

    def test_iter_articles_missing_dir_raises(self, tmp_path):
        from presskg.ingestion.articles import iter_articles
        with pytest.raises(FileNotFoundError):
            list(iter_articles(tmp_path / "nonexistent"))

    def test_content_hash_changes_with_body(self, tmp_path):
        from presskg.ingestion.articles import read_article
        data_dir = tmp_path / "Data"
        data_dir.mkdir()
        f1 = data_dir / "a.json"
        f2 = data_dir / "b.json"
        f1.write_text(json.dumps({"body": "contenuto A"}), encoding="utf-8")
        f2.write_text(json.dumps({"body": "contenuto B"}), encoding="utf-8")
        a1 = read_article(f1, data_dir)
        a2 = read_article(f2, data_dir)
        assert a1.content_hash != a2.content_hash


# ============================================================
# ontology.py
# ============================================================

class TestOntology:
    """Test per presskg.taxonomy.ontology: predicati canonici e alias."""

    def test_canonical_predicate_known(self):
        from presskg.taxonomy.ontology import canonical_predicate
        assert canonical_predicate("REPORTS") == ("REPORTS", "reporting")
        assert canonical_predicate("CAUSES") == ("CAUSES", "services")
        assert canonical_predicate("LOCATED_IN") == ("LOCATED_IN", "spatial")

    def test_canonical_predicate_normalizes_input(self):
        from presskg.taxonomy.ontology import canonical_predicate
        assert canonical_predicate("reports") == ("REPORTS", "reporting")
        assert canonical_predicate("  reports  ") == ("REPORTS", "reporting")
        assert canonical_predicate("located-in") == ("LOCATED_IN", "spatial")

    def test_canonical_predicate_alias(self):
        from presskg.taxonomy.ontology import canonical_predicate
        assert canonical_predicate("FINANCES") == ("FUNDS", "services")
        assert canonical_predicate("COOPERATES_WITH") == ("COLLABORATES_WITH", "coordination")

    def test_canonical_predicate_unknown_returns_other(self):
        from presskg.taxonomy.ontology import canonical_predicate
        assert canonical_predicate("XYZ123") == ("OTHER", "unmapped")
        assert canonical_predicate("") == ("OTHER", "unmapped")
        assert canonical_predicate(None) == ("OTHER", "unmapped")

    def test_version_is_string(self):
        from presskg.taxonomy.ontology import VERSION
        assert isinstance(VERSION, str)
        assert len(VERSION) > 0

    def test_all_canonical_predicates_have_family(self):
        from presskg.taxonomy.ontology import CANONICAL, FAMILIES
        all_families = set()
        for fam_values in FAMILIES.values():
            all_families.update(fam_values.split())
        assert set(CANONICAL.keys()) == all_families


# ============================================================
# entity_ontology.py
# ============================================================

class TestEntityOntology:
    """Test per presskg.taxonomy.entity_ontology: tipi, famiglie, normalizzazione."""

    def test_types_count(self):
        from presskg.taxonomy.entity_ontology import TYPES
        assert len(TYPES) == 31

    def test_type_family_mapping(self):
        from presskg.taxonomy.entity_ontology import type_family
        assert type_family("Person") == "actor"
        assert type_family("GPE") == "place_asset"
        assert type_family("Event") == "semantic"
        assert type_family("Money") == "value_time"
        assert type_family("UnknownType") == "unmapped"

    def test_normalize_type_canonical(self):
        from presskg.taxonomy.entity_ontology import normalize_type
        assert normalize_type("Person") == "Person"
        assert normalize_type("Company") == "Company"

    def test_normalize_type_aliases(self):
        from presskg.taxonomy.entity_ontology import normalize_type
        assert normalize_type("Persona") == "Person"
        assert normalize_type("Azienda") == "Company"
        assert normalize_type("Fiume") == "NaturalResource"
        assert normalize_type("Evento") == "Event"

    def test_normalize_type_case_insensitive(self):
        from presskg.taxonomy.entity_ontology import normalize_type
        assert normalize_type("person") == "Person"
        assert normalize_type("COMPANY") == "Company"

    def test_normalize_type_unknown_preserved(self):
        from presskg.taxonomy.entity_ontology import normalize_type
        assert normalize_type("CustomType") == "CustomType"

    def test_normalize_type_empty(self):
        from presskg.taxonomy.entity_ontology import normalize_type
        assert normalize_type("") == ""
        assert normalize_type(None) == ""
        assert normalize_type(123) == ""

    def test_prompt_catalog_contains_all_types(self):
        from presskg.taxonomy.entity_ontology import prompt_catalog, TYPES
        catalog = prompt_catalog()
        for t in TYPES:
            assert t in catalog

    def test_prompt_catalog_excludes_families(self):
        from presskg.taxonomy.entity_ontology import prompt_catalog, FAMILIES
        catalog = prompt_catalog()
        for fam in FAMILIES:
            # family names should not appear as standalone type values
            assert f"- {fam}:" not in catalog


# ============================================================
# predicate_catalog.py
# ============================================================

class TestPredicateCatalog:
    """Test per presskg.taxonomy.predicate_catalog."""

    def test_canonical_news_predicate_known(self):
        from presskg.taxonomy.predicate_catalog import canonical_news_predicate
        assert canonical_news_predicate("REDUCES") == ("REDUCES", "causal_effect")
        assert canonical_news_predicate("FUNDS") == ("FUNDS", "financing")

    def test_canonical_news_predicate_alias(self):
        from presskg.taxonomy.predicate_catalog import canonical_news_predicate
        assert canonical_news_predicate("REDUCED") == ("REDUCES", "causal_effect")
        assert canonical_news_predicate("DECLARED") == ("DECLARES", "communication")
        assert canonical_news_predicate("FUND") == ("FUNDS", "financing")

    def test_canonical_news_predicate_review_only(self):
        from presskg.taxonomy.predicate_catalog import canonical_news_predicate
        assert canonical_news_predicate("HAS") == ("OTHER", "unmapped")
        assert canonical_news_predicate("IS") == ("OTHER", "unmapped")
        assert canonical_news_predicate("OPINION") == ("OTHER", "unmapped")

    def test_canonical_news_predicate_unknown(self):
        from presskg.taxonomy.predicate_catalog import canonical_news_predicate
        assert canonical_news_predicate("XYZ") == ("OTHER", "unmapped")
        assert canonical_news_predicate("") == ("OTHER", "unmapped")

    def test_needs_predicate_review(self):
        from presskg.taxonomy.predicate_catalog import needs_predicate_review
        assert needs_predicate_review("HAS") is True
        assert needs_predicate_review("IS") is True
        assert needs_predicate_review("REDUCES") is False

    def test_normalize_predicate_name(self):
        from presskg.taxonomy.predicate_catalog import normalize_predicate_name
        assert normalize_predicate_name("reduces") == "REDUCES"
        assert normalize_predicate_name("located-in") == "LOCATED_IN"
        assert normalize_predicate_name("  funds  ") == "FUNDS"
        assert normalize_predicate_name(None) == ""

    def test_inverse_predicates(self):
        from presskg.taxonomy.predicate_catalog import INVERSE_PREDICATES
        assert "FINANCED_BY" in INVERSE_PREDICATES
        assert "AFFECTED_BY" in INVERSE_PREDICATES

    def test_preferred_predicates_sorted(self):
        from presskg.taxonomy.predicate_catalog import preferred_predicates
        preds = preferred_predicates()
        assert preds == sorted(preds)
        assert len(preds) > 0


# ============================================================
# extraction_rules.py
# ============================================================

class TestExtractionRules:
    """Test per presskg.ingestion.extraction_rules."""

    def test_normalize_actor_name_basic(self):
        from presskg.ingestion.extraction_rules import normalize_actor_name
        assert normalize_actor_name("Mario Rossi") == "mario rossi"

    def test_normalize_actor_name_accents(self):
        from presskg.ingestion.extraction_rules import normalize_actor_name
        assert normalize_actor_name("GÓRI") == "gori"
        assert normalize_actor_name("Città") == "citta"

    def test_normalize_actor_name_special_chars(self):
        from presskg.ingestion.extraction_rules import normalize_actor_name
        assert normalize_actor_name("S.p.A.") == "s p a"
        assert normalize_actor_name("  test  ") == "test"

    def test_normalize_actor_name_empty(self):
        from presskg.ingestion.extraction_rules import normalize_actor_name
        assert normalize_actor_name("") == ""
        assert normalize_actor_name(None) == ""

    def test_guardrails_non_empty(self):
        from presskg.ingestion.extraction_rules import EXTRACTION_GUARDRAILS
        assert len(EXTRACTION_GUARDRAILS) > 100
        assert "RUOLI SEMANTICI" in EXTRACTION_GUARDRAILS


# ============================================================
# grounding.py
# ============================================================

class TestGrounding:
    """Test per presskg.ingestion.grounding: localizzazione evidence nel testo."""

    def test_locate_exact_match(self):
        from presskg.ingestion.grounding import locate
        snippet = "GORI ha ripristinato il servizio idrico a Scafati."
        quote = "ha ripristinato il servizio idrico"
        ev, status = locate(snippet, 0, quote)
        assert status == "exact"
        assert ev is not None
        assert snippet[ev.start:ev.end] == quote

    def test_locate_with_offset(self):
        from presskg.ingestion.grounding import locate
        body = "XXXXX GORI ha ripristinato il servizio idrico a Scafati."
        snippet = body[5:]
        quote = "GORI ha ripristinato il servizio idrico a Scafati."
        ev, status = locate(snippet, 5, quote)
        assert status == "exact"
        assert body[ev.start:ev.end] == quote

    def test_locate_short_quote_rejected(self):
        from presskg.ingestion.grounding import locate
        ev, status = locate("test body", 0, "corto")
        assert ev is None
        assert status == "short_quote"

    def test_locate_not_found(self):
        from presskg.ingestion.grounding import locate
        ev, status = locate("GORI ha ripristinato il servizio idrico a Scafati.", 0,
                            "testo che non esiste nel body abcdef")
        assert ev is None
        assert status == "quote_not_found"

    def test_locate_ambiguous(self):
        from presskg.ingestion.grounding import locate
        snippet = "il servizio idrico è ok. il servizio idrico è ok."
        ev, status = locate(snippet, 0, "il servizio idrico è ok")
        assert ev is None
        assert status == "quote_ambiguous"

    def test_locate_normalized_unicode(self):
        from presskg.ingestion.grounding import locate
        snippet = "L’acqua è tornata. La rete è funzionante."
        quote = "L’acqua è tornata. La rete è funzionante."
        ev, status = locate(snippet, 0, quote)
        # quote identica: deve trovare in modalità exact
        assert status == "exact"
        assert ev is not None

    def test_folded_preserves_length_mapping(self):
        from presskg.ingestion.grounding import folded
        text = "Ciao mondo"
        folded_text, starts, ends = folded(text)
        assert len(starts) == len(folded_text)
        assert len(ends) == len(folded_text)
        assert folded_text == "ciao mondo"
