
import unittest

from presskg.resolution.baseline import inspect_fact


class BaselineChecks(unittest.TestCase):

    def example(self):
        fact_id = "vf-123"

        return {
            "id": fact_id,
            "claim_id": "claim-1",
            "article_id": "article-1",
            "validation_id": "validation-1",
            "batch_id": "batch-1",

            "article_found": True,
            "claim_found": True,
            "validation_found": True,

            "article_link": True,
            "claim_link": True,
            "validation_link": True,

            "claim_article_id": "article-1",
            "validation_claim_id": "claim-1",
            "validation_batch_id": "batch-1",

            "original_quote_in_body": True,
            "supporting_span_in_body": True,

            "subject_name": "Gori",
            "subject_kind": "Company",

            "object_name": "Scafati",
            "object_kind": "GPE",

            "subjects": [
                {
                    "id": fact_id + ":subject",
                    "fact_id": fact_id,
                    "role": "subject",
                    "name": "Gori",
                    "kind": "Company",
                },
            ],

            "objects": [
                {
                    "id": fact_id + ":object",
                    "fact_id": fact_id,
                    "role": "object",
                    "name": "Scafati",
                    "kind": "GPE",
                },
            ],
        }

    def test_valid_fact(self):
        self.assertEqual(
            inspect_fact(
                self.example(),
                "batch-1",
            ),
            [],
        )

    def test_missing_subject(self):
        fact = self.example()
        fact["subjects"] = []

        self.assertIn(
            "subject_mention_count_0",
            inspect_fact(
                fact,
                "batch-1",
            ),
        )

    def test_wrong_claim_provenance(self):
        fact = self.example()

        fact["validation_claim_id"] = "other"

        self.assertIn(
            "validation_claim_mismatch",
            inspect_fact(
                fact,
                "batch-1",
            ),
        )

    def test_supporting_span_is_warning(self):
        fact = self.example()

        fact["supporting_span_in_body"] = False

        self.assertEqual(
            inspect_fact(
                fact,
                "batch-1",
            ),
            ["supporting_span_not_literal"],
        )


if __name__ == "__main__":
    unittest.main()
