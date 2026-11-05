
import unittest

from presskg.resolution.semantic_decomposition import (
    classify_mention,
    validate_model_output,
)


def sample_row(name, kind, quote, components):
    parts = []
    cursor = 0

    for text in components:
        start = name.index(text, cursor)

        parts.append({
            "surface": text,
            "mention_start": start,
            "mention_end": start + len(text),
        })

        cursor = start + len(text)

    evidence = []

    if quote is not None:
        evidence.append({
            "excerpt": quote,
            "excerpt_start": 0,
            "excerpt_end": len(quote),
            "source_fingerprint": "test-fingerprint",
            "field": "claim_quote",
            "evidence_type": "COMPONENTS_COLOCATED",
        })

    return {
        "mention_id": "m1",
        "fact_id": "f1",
        "article_id": "a1",
        "role": "subject",
        "name": name,
        "kind": kind,
        "components": parts,
        "evidence": evidence,
    }


class SemanticDecompositionTests(unittest.TestCase):

    def test_explicit_geographic_hierarchy(self):
        row = sample_row(
            "Pozzano, Castellammare",
            "GPE",
            (
                "Il cantiere è in località Pozzano, "
                "a Castellammare, lungo la strada."
            ),
            ["Pozzano", "Castellammare"],
        )

        result = classify_mention(row)

        self.assertEqual(
            result["classification"],
            "HIERARCHICAL_REFERENCE",
        )

        self.assertEqual(
            result["relation"]["predicate"],
            "LOCATED_IN",
        )

        self.assertEqual(
            result["primary_component"],
            0,
        )

        self.assertFalse(
            result["split_approved"]
        )

        self.assertFalse(
            result["fact_expansion_allowed"]
        )

    def test_colocation_is_not_hierarchy(self):
        row = sample_row(
            "Via Muro D'Arce, Sarno",
            "Location",
            (
                "Sarno. L'evento è avvenuto "
                "in via Muro D'Arce."
            ),
            ["Via Muro D'Arce", "Sarno"],
        )

        result = classify_mention(row)

        self.assertEqual(
            result["classification"],
            "UNRESOLVED",
        )

        self.assertIsNone(
            result["relation"]
        )

    def test_institutional_name(self):
        row = sample_row(
            (
                "Ministero delle Infrastrutture "
                "e dei Trasporti"
            ),
            "PublicAuthority",
            (
                "Il Ministero delle Infrastrutture "
                "e dei Trasporti ha annunciato."
            ),
            [
                "Ministero delle Infrastrutture",
                "dei Trasporti",
            ],
        )

        result = classify_mention(row)

        self.assertEqual(
            result["classification"],
            "COMPOSITE_NAME",
        )

        self.assertEqual(
            result["components"],
            [],
        )

    def test_missing_grounded_context(self):
        row = sample_row(
            "Pozzano, Castellammare",
            "GPE",
            None,
            ["Pozzano", "Castellammare"],
        )

        result = classify_mention(row)

        self.assertEqual(
            result["classification"],
            "UNRESOLVED",
        )

        self.assertEqual(
            result["rule_id"],
            "no_grounded_context",
        )

    def test_llm_cannot_invent_location_relation(self):
        row = sample_row(
            "Pozzano, Castellammare",
            "GPE",
            (
                "Nelle vicinanze di Pozzano "
                "e Castellammare."
            ),
            ["Pozzano", "Castellammare"],
        )

        proposal = {
            "classification":
                "HIERARCHICAL_REFERENCE",

            "components": [
                {"start": 0, "end": 7},
                {"start": 9, "end": 22},
            ],

            "primary_index": 0,

            "relation": {
                "subject_index": 0,
                "predicate": "LOCATED_IN",
                "object_index": 1,
            },

            "support_quote": (
                "Nelle vicinanze di Pozzano "
                "e Castellammare."
            ),

            "rationale": (
                "I due luoghi sono vicini "
                "ma non necessariamente inclusi."
            ),
        }

        with self.assertRaises(ValueError):
            validate_model_output(
                row,
                proposal,
            )

    def test_no_neo4j_mutations(self):
        row = sample_row(
            "Pozzano, Castellammare",
            "GPE",
            (
                "In località Pozzano, "
                "a Castellammare."
            ),
            ["Pozzano", "Castellammare"],
        )

        result = classify_mention(row)

        self.assertEqual(
            result["graph_writes"],
            0,
        )

        self.assertFalse(
            result["identity_approved"]
        )

    def test_recover_non_split_schema(self):
        import json

        from presskg.resolution.semantic_decomposition import (
            recover_non_split_schema,
        )

        row = sample_row(
            "Comitato Acqua e Ambiente",
            "Organization",
            "Il Comitato Acqua e Ambiente è intervenuto.",
            ["Comitato Acqua", "Ambiente"],
        )

        proposal = {
            "classification": "COMPOSITE_NAME",
            "components": [
                {"start": 0, "end": 14},
                {"start": 17, "end": 25},
            ],
            "primary_index": None,
            "relation": None,
            "support_quote": None,
            "rationale": (
                "La menzione indica un unico comitato "
                "con una denominazione composta."
            ),
        }

        record = {
            "status": "invalid",
            "error": (
                "ValueError: Components forbidden for this category"
            ),
            "raw_response": json.dumps(proposal),
        }

        result = recover_non_split_schema(
            row,
            record,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["classification"],
            "COMPOSITE_NAME",
        )
        self.assertEqual(
            result["components"],
            [],
        )
        self.assertFalse(
            result["split_approved"],
        )

if __name__ == "__main__":
    unittest.main()
