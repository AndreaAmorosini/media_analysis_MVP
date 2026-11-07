
import unittest

from presskg.resolution.canonical_fact_projection import (
    stable_key,
    validate_fact,
    build_query,
)


def pair():
    return {
        "fact_id": "fact-1",
        "article_id": "article-1",
        "subject_key": "ce-subject",
        "object_key": "ce-object",
    }


def observation(**changes):
    result = {
        "fact_count": 1,
        "subject_mention_count": 1,
        "object_mention_count": 1,
        "subject_keys": ["ce-subject"],
        "object_keys": ["ce-object"],
        "subject_fact_linked": True,
        "object_fact_linked": True,
        "subject_role": "subject",
        "object_role": "object",
        "subject_fact_id": "fact-1",
        "object_fact_id": "fact-1",
        "fact_article_id": "article-1",
        "predicate": "MANAGES",
    }
    result.update(changes)
    return result


class CanonicalFactProjectionTests(unittest.TestCase):

    def test_deterministic_fact_key(self):
        self.assertEqual(
            stable_key("batch-1", "fact-1"),
            stable_key("batch-1", "fact-1"),
        )

        self.assertNotEqual(
            stable_key("batch-1", "fact-1"),
            stable_key("batch-1", "fact-2"),
        )

    def test_valid_fact(self):
        self.assertEqual(
            validate_fact(pair(), observation()),
            [],
        )

    def test_wrong_subject_identity(self):
        errors = validate_fact(
            pair(),
            observation(
                subject_keys=["ce-other"]
            ),
        )
        self.assertIn(
            "SUBJECT_IDENTITY_MISMATCH", errors
        )

    def test_multiple_subject_identities(self):
        errors = validate_fact(
            pair(),
            observation(
                subject_keys=[
                    "ce-subject", "ce-other"
                ]
            ),
        )
        self.assertIn(
            "SUBJECT_IDENTITY_MISMATCH", errors
        )

    def test_unresolved_object(self):
        errors = validate_fact(
            pair(),
            observation(object_keys=[]),
        )
        self.assertIn(
            "OBJECT_IDENTITY_MISMATCH", errors
        )

    def test_missing_original_fact_link(self):
        errors = validate_fact(
            pair(),
            observation(
                subject_fact_linked=False
            ),
        )
        self.assertIn(
            "SUBJECT_FACT_LINK_MISSING", errors
        )

    def test_wrong_fact_role(self):
        errors = validate_fact(
            pair(),
            observation(object_role="subject"),
        )
        self.assertIn(
            "OBJECT_ROLE_MISMATCH", errors
        )

    def test_missing_predicate(self):
        errors = validate_fact(
            pair(),
            observation(predicate=None),
        )
        self.assertIn("PREDICATE_MISSING", errors)

    def test_empty_predicate(self):
        errors = validate_fact(
            pair(),
            observation(predicate=" "),
        )
        self.assertIn("PREDICATE_MISSING", errors)

    def test_different_article(self):
        errors = validate_fact(
            pair(),
            observation(
                fact_article_id="article-2"
            ),
        )
        self.assertIn(
            "ARTICLE_ID_MISMATCH", errors
        )

    def test_predicate_property_validation(self):
        with self.assertRaises(ValueError):
            build_query("predicate) DETACH DELETE f")


if __name__ == "__main__":
    unittest.main()
