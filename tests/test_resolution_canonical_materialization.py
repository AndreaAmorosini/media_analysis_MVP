
import unittest

from presskg.resolution.canonical_materialization import (
    VERSION,
    projection_key,
    representative_name,
    validate_observation,
    plan_batches,
)


class CanonicalMaterializationTests(unittest.TestCase):

    def test_deterministic_projection_key(self):
        a = projection_key("batch-1", "identity-1")
        b = projection_key("batch-1", "identity-1")
        c = projection_key("batch-1", "identity-2")

        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("ce-"))

    def test_separate_source_batches(self):
        self.assertNotEqual(
            projection_key("batch-1", "identity-1"),
            projection_key("batch-2", "identity-1"),
        )

    def test_representative_name(self):
        self.assertEqual(
            representative_name({
                "Acme": 15,
                "Acme S.p.A.": 3,
            }),
            "Acme",
        )

    def test_missing_fact_link(self):
        source = {
            "fact_id": "fact-1",
            "article_id": "article-1",
            "role": "subject",
            "projection_key": "ce-1",
        }

        observation = {
            "mention_count": 1,
            "fact_count": 1,
            "fact_linked": False,
            "mention_fact_id": "fact-1",
            "mention_role": "subject",
            "mention_article_id": "article-1",
            "fact_article_id": "article-1",
            "article_linked": True,
            "existing_resolutions": [],
        }

        self.assertIn(
            "FACT_LINK_MISSING",
            validate_observation(
                source, observation
            ),
        )

    def test_conflicting_canonical_identity(self):
        source = {
            "fact_id": "fact-1",
            "article_id": "article-1",
            "role": "subject",
            "projection_key": "ce-expected",
        }

        observation = {
            "mention_count": 1,
            "fact_count": 1,
            "fact_linked": True,
            "mention_fact_id": "fact-1",
            "mention_role": "subject",
            "mention_article_id": "article-1",
            "fact_article_id": "article-1",
            "article_linked": True,
            "existing_resolutions": [{
                "key": "ce-other",
                "version": VERSION,
            }],
        }

        self.assertIn(
            "EXISTING_RESOLUTION_CONFLICT",
            validate_observation(
                source, observation
            ),
        )

    def test_batch_preserves_large_identity(self):
        plan = [
            {
                "links": [
                    {"profile_id": str(i)}
                    for i in range(500)
                ],
            },
            {
                "links": [
                    {"profile_id": "last"}
                ],
            },
        ]

        batches = list(plan_batches(plan, 250))

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 1)
        self.assertEqual(len(batches[1]), 1)


if __name__ == "__main__":
    unittest.main()
