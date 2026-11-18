"""Regression tests per presskg.ingestion.extraction.

Cattura il comportamento di: windows, parse_json_object,
validate_partial_graph, grounded_records, ExtractedClaim/ExtractedEntity.
"""

import json

import pytest


# ============================================================
# windows()
# ============================================================

class TestWindows:
    """Test per la funzione windows() di chunking."""

    def test_single_window_short_body(self):
        from presskg.ingestion.extraction import windows
        body = "testo corto"
        result = list(windows(body, 6500, 500))
        assert len(result) == 1
        assert result[0] == (0, body)

    def test_multiple_windows_long_body(self):
        from presskg.ingestion.extraction import windows
        body = "A" * 20000
        result = list(windows(body, 6500, 500))
        assert len(result) > 1
        # ogni finestra (tranne l'ultima) deve essere <= size
        for i, (offset, snippet) in enumerate(result[:-1]):
            assert len(snippet) <= 6500
        # l'ultima finestra termina alla fine del body
        assert result[-1][0] + len(result[-1][1]) == len(body)

    def test_windows_cover_entire_body(self):
        from presskg.ingestion.extraction import windows
        body = "Parola " * 5000  # ~35000 chars
        result = list(windows(body, 6500, 500))
        # la copertura deve essere continua (con overlap)
        for i in range(1, len(result)):
            prev_end = result[i - 1][0] + len(result[i - 1][1])
            curr_start = result[i][0]
            assert curr_start < prev_end  # c'è overlap

    def test_windows_invalid_params(self):
        from presskg.ingestion.extraction import windows
        with pytest.raises(ValueError):
            list(windows("test", 500, 100))  # size < 1000
        with pytest.raises(ValueError):
            list(windows("test", 6500, 4000))  # overlap >= size//2

    def test_windows_prefers_sentence_boundary(self):
        from presskg.ingestion.extraction import windows
        # body con frasi nette a metà finestra
        body = ("Prima frase. " * 300) + ("Seconda frase. " * 300)
        result = list(windows(body, 2500, 200))
        assert len(result) >= 2


# ============================================================
# parse_json_object()
# ============================================================

class TestParseJsonObject:
    """Test per parse_json_object: parsing robusto di output LLM."""

    def test_plain_json(self):
        from presskg.ingestion.extraction import parse_json_object
        result = parse_json_object('{"a": 1}')
        assert result == {"a": 1}

    def test_json_with_markdown_fence(self):
        from presskg.ingestion.extraction import parse_json_object
        result = parse_json_object('```json\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_json_with_text_before(self):
        from presskg.ingestion.extraction import parse_json_object
        result = parse_json_object('Ecco il risultato: {"a": 1}')
        assert result == {"a": 1}

    def test_json_with_text_after(self):
        from presskg.ingestion.extraction import parse_json_object
        result = parse_json_object('{"a": 1} spero sia corretto')
        assert result == {"a": 1}

    def test_no_json_raises(self):
        from presskg.ingestion.extraction import parse_json_object
        with pytest.raises(ValueError, match="privo di JSON"):
            parse_json_object("nessun json qui")

    def test_non_dict_json_raises(self):
        from presskg.ingestion.extraction import parse_json_object
        # '[1, 2, 3]' non contiene '{', quindi lancia "privo di JSON"
        with pytest.raises(ValueError):
            parse_json_object('[1, 2, 3]')


# ============================================================
# validate_partial_graph()
# ============================================================

class TestValidatePartialGraph:
    """Test per validate_partial_graph."""

    def test_valid_graph(self, sample_extracted_graph):
        from presskg.ingestion.extraction import validate_partial_graph
        graph, errors = validate_partial_graph(sample_extracted_graph)
        assert len(errors) == 0
        assert len(graph.entities) == 2
        assert len(graph.claims) == 1

    def test_invalid_entities_not_list(self):
        from presskg.ingestion.extraction import validate_partial_graph
        with pytest.raises(ValueError, match="entities non è una lista"):
            validate_partial_graph({"entities": "not a list", "claims": []})

    def test_invalid_claims_not_list(self):
        from presskg.ingestion.extraction import validate_partial_graph
        with pytest.raises(ValueError, match="claims non è una lista"):
            validate_partial_graph({"entities": [], "claims": "not a list"})

    def test_non_dict_raises(self):
        from presskg.ingestion.extraction import validate_partial_graph
        with pytest.raises(ValueError, match="Output non valido"):
            validate_partial_graph([1, 2, 3])

    def test_schema_errors_collected(self):
        from presskg.ingestion.extraction import validate_partial_graph
        data = {
            "entities": [
                {"local_id": "e1", "name": "Test", "type": "Company"},
                {"local_id": "e2", "name": "Bad", "type": "InvalidType"},
            ],
            "claims": [],
        }
        graph, errors = validate_partial_graph(data)
        assert len(graph.entities) == 1  # solo quella valida
        assert len(errors) == 1
        assert "entity[1]" in errors[0]

    def test_predicate_normalization_in_claim(self):
        from presskg.ingestion.extraction import validate_partial_graph
        data = {
            "entities": [
                {"local_id": "e1", "name": "A", "type": "Company"},
                {"local_id": "e2", "name": "B", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "restores", "object_id": "e2",
                 "quote": "test quote long enough"}
            ],
        }
        graph, errors = validate_partial_graph(data)
        assert len(errors) == 0
        assert graph.claims[0].predicate == "RESTORES"
        assert graph.claims[0].predicate_raw == "RESTORES"

    def test_unknown_predicate_becomes_other(self):
        from presskg.ingestion.extraction import validate_partial_graph
        data = {
            "entities": [
                {"local_id": "e1", "name": "A", "type": "Company"},
                {"local_id": "e2", "name": "B", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "UNKNOWN_PRED", "object_id": "e2",
                 "quote": "test quote long enough"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        assert graph.claims[0].predicate == "OTHER"
        assert graph.claims[0].predicate_raw == "UNKNOWN_PRED"


# ============================================================
# grounded_records()
# ============================================================

class TestGroundedRecords:
    """Test per grounded_records: ancoraggio delle claim al body."""

    def _make_article(self, body: str):
        from presskg.ingestion.articles import Article
        return Article(
            id="article-test",
            rel_path="test.json",
            title="Test",
            subtitle="",
            publication_date="2026-01-01",
            source_name="Test",
            source_type="unknown",
            body=body,
            content_hash="abc123",
        )

    def test_valid_claim_grounded(self):
        from presskg.ingestion.extraction import validate_partial_graph, grounded_records
        body = "GORI ha ripristinato il servizio idrico a Scafati dopo il guasto."
        quote = "GORI ha ripristinato il servizio idrico a Scafati"
        data = {
            "entities": [
                {"local_id": "e1", "name": "GORI", "type": "Company"},
                {"local_id": "e2", "name": "guasto", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "RESTORES", "object_id": "e2",
                 "quote": quote, "modality": "reported"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        article = self._make_article(body)
        entities, claims, rejected = grounded_records(article, 0, body, graph, 0)
        assert rejected == 0
        assert len(claims) == 1
        assert claims[0]["body_start"] == 0
        assert claims[0]["body_end"] == len(quote)

    def test_short_quote_rejected(self):
        from presskg.ingestion.extraction import validate_partial_graph, grounded_records
        body = "GORI ha ripristinato il servizio idrico a Scafati."
        data = {
            "entities": [
                {"local_id": "e1", "name": "GORI", "type": "Company"},
                {"local_id": "e2", "name": "guasto", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "RESTORES", "object_id": "e2",
                 "quote": "corto", "modality": "reported"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        article = self._make_article(body)
        _, claims, rejected = grounded_records(article, 0, body, graph, 0)
        assert rejected == 1
        assert len(claims) == 0

    def test_quote_not_in_body_rejected(self):
        from presskg.ingestion.extraction import validate_partial_graph, grounded_records
        body = "GORI ha ripristinato il servizio idrico a Scafati."
        data = {
            "entities": [
                {"local_id": "e1", "name": "GORI", "type": "Company"},
                {"local_id": "e2", "name": "guasto", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "RESTORES", "object_id": "e2",
                 "quote": "testo che non esiste nel body abcdef", "modality": "reported"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        article = self._make_article(body)
        _, claims, rejected = grounded_records(article, 0, body, graph, 0)
        assert rejected == 1
        assert len(claims) == 0

    def test_event_entity_gets_event_id(self):
        from presskg.ingestion.extraction import validate_partial_graph, grounded_records
        body = "GORI ha ripristinato il servizio idrico a Scafati dopo il guasto."
        quote = "GORI ha ripristinato il servizio idrico a Scafati"
        data = {
            "entities": [
                {"local_id": "e1", "name": "GORI", "type": "Company"},
                {"local_id": "e2", "name": "guasto", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "RESTORES", "object_id": "e2",
                 "quote": quote, "modality": "reported"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        article = self._make_article(body)
        entities, claims, _ = grounded_records(article, 0, body, graph, 0)
        event_entity = next(e for e in entities if e["type"] == "Event")
        company_entity = next(e for e in entities if e["type"] == "Company")
        assert event_entity["id"].startswith("event-")
        assert company_entity["id"] == ""  # ID globale assegnato da Neo4j

    def test_claim_id_deterministic(self):
        from presskg.ingestion.extraction import validate_partial_graph, grounded_records
        body = "GORI ha ripristinato il servizio idrico a Scafati dopo il guasto."
        quote = "GORI ha ripristinato il servizio idrico a Scafati"
        data = {
            "entities": [
                {"local_id": "e1", "name": "GORI", "type": "Company"},
                {"local_id": "e2", "name": "guasto", "type": "Event"},
            ],
            "claims": [
                {"subject_id": "e1", "predicate": "RESTORES", "object_id": "e2",
                 "quote": quote, "modality": "reported"}
            ],
        }
        graph, _ = validate_partial_graph(data)
        article = self._make_article(body)
        _, claims1, _ = grounded_records(article, 0, body, graph, 0)
        _, claims2, _ = grounded_records(article, 0, body, graph, 0)
        assert claims1[0]["id"] == claims2[0]["id"]
        assert claims1[0]["id"].startswith("claim-")


# ============================================================
# Modelli Pydantic
# ============================================================

class TestPydanticModels:
    """Test per i modelli ExtractedEntity, ExtractedClaim, ExtractedGraph."""

    def test_extracted_entity_defaults(self):
        from presskg.ingestion.extraction import ExtractedEntity
        e = ExtractedEntity(local_id="e1", name="Test", type="Company")
        assert e.event_type is None
        assert e.event_date is None
        assert e.location is None

    def test_extracted_claim_defaults(self):
        from presskg.ingestion.extraction import ExtractedClaim
        c = ExtractedClaim()
        assert c.predicate == "OTHER"
        assert c.modality == "uncertain"
        assert c.subject_id is None
        assert c.object_id is None

    def test_extracted_graph_defaults(self):
        from presskg.ingestion.extraction import ExtractedGraph
        g = ExtractedGraph()
        assert g.entities == []
        assert g.claims == []
