
import unittest

from presskg.resolution.predicate_quality_audit import (
    normalize_predicate,
    extract_evidence,
    audit_record,
    group_semantics,
    repeated_pairs,
    queue_record,
)


def raw_record(
    predicate="OTHER",
    fact_predicate="OTHER",
    evidence=None,
):
    properties = {
        "id": "vf-1",
        "predicate": fact_predicate,
        "article_id": "article-1",
    }

    if evidence is not None:
        properties["evidence_quote"] = evidence

    return {
        "relationship_properties": {
            "projection_key": "cf-1",
            "fact_id": "vf-1",
            "article_id": "article-1",
            "predicate": predicate,
            "source_batch_key": "batch-1",
            "projection_version":
                "canonical-fact-projection-v1",
        },
        "fact_properties": properties,
        "fact_count": 1,
        "subject_key": "ce-subject",
        "object_key": "ce-object",
        "subject_name": "Organization A",
        "object_name": "Organization B",
        "subject_facet": "ORGANIZATION",
        "object_facet": "ORGANIZATION",
    }


class PredicateQualityAuditTests(unittest.TestCase):

    def test_normalization(self):
        self.assertEqual(
            normalize_predicate(" other "),
            "OTHER",
        )

    def test_evidence_extraction(self):
        evidence = extract_evidence(
            {"evidence_quote": " A  B  C "},
            100,
        )

        self.assertEqual(
            evidence["text"], "A B C"
        )
        self.assertFalse(
            evidence["grounding_verified"]
        )

    def test_missing_evidence(self):
        evidence = extract_evidence(
            {"title": "Example"},
            100,
        )

        self.assertIsNone(evidence["text"])

    def test_truncated_evidence(self):
        evidence = extract_evidence(
            {"evidence_text": "abcdefghij"},
            5,
        )

        self.assertEqual(
            evidence["text"], "abcde"
        )
        self.assertTrue(evidence["truncated"])

    def test_valid_other_fact(self):
        result = audit_record(
            raw_record(evidence="Source quote"),
            100,
        )

        self.assertEqual(
            result["structural_problems"], []
        )
        self.assertEqual(
            result["predicate"], "OTHER"
        )

    def test_source_predicate_conflict(self):
        result = audit_record(
            raw_record(fact_predicate="FUNDS"),
            100,
        )

        self.assertIn(
            "SOURCE_PREDICATE_MISMATCH",
            result["structural_problems"],
        )

    def test_self_loop(self):
        raw = raw_record()
        raw["object_key"] = raw["subject_key"]

        result = audit_record(raw, 100)

        self.assertIn(
            "CANONICAL_SELF_LOOP",
            result["structural_problems"],
        )

    def test_missing_source_fact(self):
        raw = raw_record()
        raw["fact_count"] = 0
        raw["fact_properties"] = None

        result = audit_record(raw, 100)

        self.assertIn(
            "FACT_CARDINALITY_NOT_ONE",
            result["structural_problems"],
        )

    def test_queue_requires_evidence(self):
        row = audit_record(raw_record(), 100)
        row["pair_occurrences"] = 1

        item = queue_record(row)

        self.assertEqual(
            item["next_action"],
            "RETRIEVE_SOURCE_EVIDENCE",
        )
        self.assertIsNone(
            item["proposed_predicate"]
        )

    def test_queue_with_evidence(self):
        row = audit_record(
            raw_record(evidence="Source quotation"),
            100,
        )
        row["pair_occurrences"] = 1

        item = queue_record(row)

        self.assertEqual(
            item["next_action"],
            "VERIFY_PREDICATE_FROM_EVIDENCE",
        )

    def test_semantic_group(self):
        row = audit_record(raw_record(), 100)
        row["pair_occurrences"] = 1

        groups = group_semantics([row], 5)

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["fact_count"], 1
        )

    def test_repeated_pairs(self):
        rows = []

        for i in range(3):
            raw = raw_record()
            raw["relationship_properties"][
                "fact_id"
            ] = f"vf-{i}"
            raw["relationship_properties"][
                "projection_key"
            ] = f"cf-{i}"
            raw["fact_properties"]["id"] = f"vf-{i}"

            row = audit_record(raw, 100)
            row["pair_occurrences"] = 3
            rows.append(row)

        groups = repeated_pairs(rows, 3, 5)

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["other_fact_count"], 3
        )
        
    def test_real_validated_fact_schema(self):
        evidence = extract_evidence(
            {
                "original_quote":
                    "Il Comune ha approvato il progetto.",
                "supporting_span":
                    "ha approvato il progetto",
            },
            1600,
        )

        self.assertEqual(
            evidence["field"], "supporting_span"
        )
        self.assertEqual(
            evidence["original_quote"],
            "Il Comune ha approvato il progetto.",
        )
        self.assertFalse(
            evidence["grounding_verified"]
        )


    def test_fallback_to_original_quote(self):
        evidence = extract_evidence(
            {
                "original_quote":
                    "La società finanzia l'intervento.",
                "supporting_span": "",
            },
            1600,
        )

        self.assertEqual(
            evidence["field"], "original_quote"
        )


if __name__ == "__main__":
    unittest.main()
