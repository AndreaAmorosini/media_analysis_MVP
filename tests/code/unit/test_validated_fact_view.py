import copy
import unittest

from presskg.validation.primary import claim_hash
from presskg.validation.materialization import make_row


class ValidatedFactTests(unittest.TestCase):

    def setUp(self):
        quote = "Gori gestisce il servizio idrico."

        claim = {
            "id": "c1",
            "article_id": "a1",
            "chunk_id": "ch1",
            "predicate": "MANAGES",
            "predicate_raw": "MANAGES",
            "modality": "asserted",
            "attribution": None,
            "quote": quote,
            "body_start": 0,
            "body_end": len(quote),
        }

        subject = {
            "id": "s1",
            "name": "Gori",
            "kind": "Company",
        }

        obj = {
            "id": "o1",
            "name": "servizio idrico",
            "kind": "Service",
        }

        article = {
            "id": "a1",
            "content_hash": "hash-a1",
            "publication_date": "2023-01-01",
        }

        original = {
            "claim_id": "c1",
            "article_id": "a1",
            "subject_id": "s1",
            "subject": "Gori",
            "subject_type": "Company",
            "predicate": "MANAGES",
            "predicate_raw": "MANAGES",
            "object_id": "o1",
            "object": "servizio idrico",
            "object_type": "Service",
            "modality": "asserted",
            "attribution": None,
            "quote": quote,
            "body_start": 0,
            "body_end": len(quote),
            "content_hash": "hash-a1",
        }

        validation = {
            "id": "v1",
            "claim_id": "c1",
            "batch_id": "test-batch",
            "status": "accepted",
            "correction_id": None,
            "claim_hash": claim_hash(original),
            "primary_run_hash": "run1",
            "temporal_status": "ongoing",
            "semantic_family": "operational",
            "candidate_for_propagation": True,
        }

        self.source = {
            "validation": validation,
            "claim": claim,
            "subject": subject,
            "object": obj,
            "article": article,
            "linked_chunk_id": "ch1",
            "proof": quote,
            "context": quote,
            "claim_corrections": [],
            "validation_correction_ids": [],
        }

    def corrected_source(self):
        source = copy.deepcopy(self.source)
        source["validation"]["status"] = (
            "corrected_verified"
        )
        source["validation"]["correction_id"] = "x1"

        source["claim_corrections"] = [{
            "id": "x1",
            "batch_id": "test-batch",
            "claim_id": "c1",
            "claim_hash": source[
                "validation"
            ]["claim_hash"],
            "primary_run_hash": "run1",
            "source_candidate_id": "cc1",
            "candidate_hash": "candidate-hash",
            "subject_name": "Gori",
            "subject_type": "Company",
            "predicate": "PROVIDES",
            "object_name": "acqua potabile",
            "object_type": "Service",
            "modality": "asserted",
            "attribution": None,
            "temporal_status": "ongoing",
            "semantic_family": "operational",
            "supporting_span": "servizio idrico",
        }]

        source["validation_correction_ids"] = ["x1"]

        return source

    def test_accepted_uses_original(self):
        row = make_row(self.source, "test-batch")
        fact = row["fact"]

        self.assertEqual(
            fact["representation"], "original"
        )
        self.assertEqual(fact["predicate"], "MANAGES")
        self.assertEqual(fact["subject_name"], "Gori")

        self.assertFalse(
            fact["eligible_for_propagation"]
        )

    def test_corrected_uses_verified_values(self):
        row = make_row(
            self.corrected_source(), "test-batch"
        )
        fact = row["fact"]

        self.assertEqual(
            fact["representation"],
            "verified_correction",
        )
        self.assertEqual(fact["predicate"], "PROVIDES")
        self.assertEqual(
            fact["object_name"], "acqua potabile"
        )
        self.assertEqual(fact["correction_id"], "x1")

    def test_fact_id_is_stable(self):
        a = make_row(self.source, "test-batch")
        b = make_row(self.source, "test-batch")

        self.assertEqual(a["id"], b["id"])

    def test_original_hash_mismatch(self):
        source = copy.deepcopy(self.source)
        source["article"]["content_hash"] = "changed"

        with self.assertRaises(ValueError):
            make_row(source, "test-batch")

    def test_missing_correction_link(self):
        source = self.corrected_source()
        source["validation_correction_ids"] = []

        with self.assertRaises(ValueError):
            make_row(source, "test-batch")

    def test_ungrounded_corrected_span(self):
        source = self.corrected_source()
        source["claim_corrections"][0][
            "supporting_span"
        ] = "citazione inventata"

        with self.assertRaises(ValueError):
            make_row(source, "test-batch")

    def test_rejected_is_not_materialized(self):
        source = copy.deepcopy(self.source)
        source["validation"]["status"] = "rejected"

        with self.assertRaises(ValueError):
            make_row(source, "test-batch")


if __name__ == "__main__":
    unittest.main()
