"""Invarianti del layer eventi: resolution e evidenze validate."""

import pytest

pytestmark = pytest.mark.graph


def count(session, query, **params):
    record = session.run(query, **params).single()
    return record[0] if record else 0


class TestEventLayer:

    def test_event_resolved_as_endpoints(self, session):
        """Ogni EVENT_RESOLVED_AS va da EventMention a CanonicalEvent."""
        bad = count(
            session,
            "MATCH (m)-[r:EVENT_RESOLVED_AS]->(c) "
            "WHERE NOT (m:EventMention AND c:CanonicalEvent) "
            "RETURN count(r)",
        )
        assert bad == 0, f"{bad} archi EVENT_RESOLVED_AS con end-point errati"

    def test_supported_by_endpoints(self, session):
        """Ogni SUPPORTED_BY collega un CanonicalEvent a un ValidatedFact."""
        bad = count(
            session,
            "MATCH (e)-[r:SUPPORTED_BY]->(f) "
            "WHERE NOT (e:CanonicalEvent AND f:ValidatedFact) "
            "RETURN count(r)",
        )
        assert bad == 0, f"{bad} archi SUPPORTED_BY con end-point errati"

    def test_supported_by_requires_provenance(self, session):
        """Ogni SUPPORTED_BY dichiara la propria evidence_key."""
        missing = count(
            session,
            "MATCH ()-[r:SUPPORTED_BY]->() "
            "WHERE r.evidence_key IS NULL "
            "RETURN count(r)",
        )
        assert missing == 0, f"{missing} archi SUPPORTED_BY senza evidence_key"

    def test_events_with_validated_evidence(self, session):
        """Almeno un evento canonico ha evidenza validata (SUPPORTED_BY)."""
        with_evidence = count(
            session,
            "MATCH (e:CanonicalEvent)-[:SUPPORTED_BY]->(:ValidatedFact) "
            "RETURN count(DISTINCT e)",
        )
        assert with_evidence > 0, (
            "Nessun CanonicalEvent con SUPPORTED_BY: rieseguire "
            "`pixi run pipeline --phase events --apply`."
        )

    def test_canonical_event_coverage_ratio(self, session):
        """Rapporto eventi canonici con evidenza: sanity contro eventi fantasma.

        Non esigiamo copertura totale (informazione nel 7C: ~5.285 su 7.233),
        ma un numero di eventi con evidenza plausibile e non superiore agli
        eventi canonici totali.
        """
        total = count(session, "MATCH (e:CanonicalEvent) RETURN count(e)")
        with_evidence = count(
            session,
            "MATCH (e:CanonicalEvent)-[:SUPPORTED_BY]->(:ValidatedFact) "
            "RETURN count(DISTINCT e)",
        )
        assert 0 < with_evidence <= int(total), (
            f"Eventi con evidenza ({with_evidence}) incoerenti "
            f"rispetto ai CanonicalEvent totali ({total})"
        )
