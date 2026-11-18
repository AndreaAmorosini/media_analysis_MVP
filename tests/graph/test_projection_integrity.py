"""Invarianti del layer canonico: entità, fact e provenance.

Verifica la struttura dopo la fase projection: end-point corretti degli
archi canonici e provenance obbligatoria su ogni CANONICAL_FACT.
"""

import pytest

pytestmark = pytest.mark.graph


def count(session, query, **params):
    record = session.run(query, **params).single()
    return record[0] if record else 0


class TestCanonicalLayer:

    def test_canonical_entities_exist(self, session):
        n = count(session, "MATCH (c:CanonicalEntity) RETURN count(c)")
        assert n > 0, (
            "Nessun CanonicalEntity: eseguire "
            "`pixi run pipeline --phase projection --apply`."
        )

    def test_resolved_as_endpoints(self, session):
        """Ogni RESOLVED_AS va da ValidatedMention a CanonicalEntity."""
        bad = count(
            session,
            "MATCH (m)-[r:RESOLVED_AS]->(c) "
            "WHERE NOT (m:ValidatedMention AND c:CanonicalEntity) "
            "RETURN count(r)",
        )
        assert bad == 0, f"{bad} archi RESOLVED_AS con end-point errati"

    def test_canonical_fact_endpoints(self, session):
        """Ogni CANONICAL_FACT collega due CanonicalEntity."""
        bad = count(
            session,
            "MATCH (s)-[r:CANONICAL_FACT]->(o) "
            "WHERE NOT (s:CanonicalEntity AND o:CanonicalEntity) "
            "RETURN count(r)",
        )
        assert bad == 0, f"{bad} archi CANONICAL_FACT con end-point errati"

    def test_canonical_fact_provenance(self, session):
        """Ogni CANONICAL_FACT porta fact_id e article_id non nulli."""
        missing = count(
            session,
            "MATCH ()-[r:CANONICAL_FACT]->() "
            "WHERE r.fact_id IS NULL OR r.article_id IS NULL "
            "RETURN count(r)",
        )
        assert missing == 0, (
            f"{missing} archi CANONICAL_FACT privi di provenance (fact_id/article_id)"
        )

    def test_validated_fact_connectivity(self, session):
        """Ogni ValidatedFact è raggiungibile da un Claim (HAS_VALIDATED_FACT)."""
        orphan = count(
            session,
            "MATCH (f:ValidatedFact) "
            "WHERE NOT (:Claim)-[:HAS_VALIDATED_FACT]->(f) "
            "RETURN count(f)",
        )
        assert orphan == 0, (
            f"{orphan} ValidatedFact orfani: nessun Claim collegato"
        )
