"""Invarianti di schema sul grafo reale: etichette e relazioni attese.

Ogni test fallisce se una fase della pipeline non è mai stata applicata al
database: il messaggio indica quale fase eseguire.
"""

import pytest

pytestmark = pytest.mark.graph

EXPECTED_NODE_LABELS = [
    "Article",
    "Chunk",
    "Claim",
    "GlobalEntity",
    "EventMention",
    "ValidatedMention",
    "ValidatedFact",
    "ValidatedFactBatch",
    "CanonicalEntity",
    "CanonicalEvent",
]

# Etichette legittime non obbligatorie: presenti dopo le fasi di
# validazione/corrections, backfill fonti o overlay predicati.
# EntityMention e' scritto da graph_store.store_article insieme alle
# CANDIDATE_GLOBAL verso i GlobalEntity (grafo entita' pre-resolution).
OPTIONAL_NODE_LABELS = [
    "ClaimValidation",
    "VerifiedClaimCorrection",
    "Source",
    "PredicateProposal",
    "GraphNode",
    "Topic",
    "EntityMention",
]

EXPECTED_REL_TYPES = [
    "FACT",
    "HAS_VALIDATED_FACT",
    "HAS_FACT",
    "RESOLVED_AS",
    "CANONICAL_FACT",
    "EVENT_RESOLVED_AS",
    "SUPPORTED_BY",
]

# Relazioni legittime non obbligatorie (in ingestion, validation, backfill
# o arricchimenti). HAS_TOPIC compare solo dopo `enrich`/`topics`.
OPTIONAL_REL_TYPES = [
    "SUBJECT",
    "OBJECT",
    "HAS_CLAIM",
    "HAS_VALIDATION",
    "VALIDATED_AS",
    "SUBJECT_MENTION",
    "OBJECT_MENTION",
    "HAS_CHUNK",
    "HAS_EVENT",
    "HAS_VERIFIED_CORRECTION",
    "VERIFIED_CORRECTION",
    "MATERIALIZED_AS",
    "PUBLISHED_BY",
    "DOCUMENTED_IN",
    "CANONICAL_RELATION",
    "HAS_TOPIC",
    "CANDIDATE_GLOBAL",
]


def scalar(session, query, **params):
    record = session.run(query, **params).single()
    return record[0] if record else 0


class TestSchemaLabels:

    @pytest.mark.parametrize("label", EXPECTED_NODE_LABELS)
    def test_label_exists(self, session, label):
        count = scalar(session, f"MATCH (n:{label}) RETURN count(n)")
        assert count > 0, (
            f"Nessun nodo con etichetta :{label}. "
            "Il grafo non contiene il layer atteso: verificare ingest/validation"
            " o la fase di resolution corrispondente."
        )

    def test_unknown_labels_reported(self, session):
        """Le etichette presenti devono essere tutte note al progetto.

        Rassegnare eventuali nuove etichette qui quando introdotte
        deliberatamente.
        """
        allowed = set(EXPECTED_NODE_LABELS) | set(OPTIONAL_NODE_LABELS)
        rows = session.run(
            "MATCH (n) UNWIND labels(n) AS label "
            "RETURN label, count(*) AS total ORDER BY total DESC"
        ).data()
        unknown = [r for r in rows if r["label"] not in allowed]
        assert not unknown, (
            "Etichette non previste nel grafo:\n"
            + "\n".join(f"  {r['label']}: {r['total']}" for r in unknown)
        )


class TestSchemaRelations:

    @pytest.mark.parametrize("rel", EXPECTED_REL_TYPES)
    def test_rel_type_exists(self, session, rel):
        count = scalar(
            session, f"MATCH ()-[r:{rel}]->() RETURN count(r)"
        )
        assert count > 0, (
            f"Nessuna relazione :{rel}. Fase mancante: "
            + {
                "FACT": "ingest",
                "HAS_VALIDATED_FACT": "validation materialize",
                "HAS_FACT": "validation materialize",
                "RESOLVED_AS": "resolution pipeline --phase projection --apply",
                "CANONICAL_FACT": "resolution pipeline --phase projection --apply",
                "EVENT_RESOLVED_AS": "resolution pipeline --phase events --apply",
                "SUPPORTED_BY": "resolution pipeline --phase events --apply",
                "HAS_TOPIC": "enrich (topics lessicali o semantici)",
            }.get(rel, "vedere docs/ARCHITECTURE.md")
        )

    def test_unknown_rel_types_reported(self, session):
        allowed = set(EXPECTED_REL_TYPES) | set(OPTIONAL_REL_TYPES)
        rows = session.run(
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS total ORDER BY total DESC"
        ).data()
        unknown = [r for r in rows if r["rel"] not in allowed]
        assert not unknown, (
            "Tipi di relazione non previsti nel grafo:\n"
            + "\n".join(f"  {r['rel']}: {r['total']}" for r in unknown)
        )
