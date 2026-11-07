
import unittest

from presskg.resolution.canonical_fact_materialization import (
    VERSION,
    ENTITY_VERSION,
    stable_key,
    validate_observation,
    batches,
)


def expected():
    return {
        "fact_id": "vf-1",
        "article_id": "article-1",
        "predicate": "MANAGES",
        "subject_key": "ce-subject",
        "object_key": "ce-object",
        "source_batch_key": "batch-1",
        "projection_key": stable_key(
            "batch-1", "vf-1"
        ),
    }


def observed(**changes):
    result = {
        "fact_count": 1,
        "subject_mention_count": 1,
        "object_mention_count": 1,
        "subject_entity_count": 1,
        "object_entity_count": 1,
        "predicate": "MANAGES",
        "fact_article_id": "article-1",
        "subject_fact_id": "vf-1",
        "object_fact_id": "vf-1",
        "subject_role": "subject",
        "object_role": "object",
        "subject_fact_linked": True,
        "object_fact_linked": True,
        "subject_resolved": True,
        "object_resolved": True,
        "subject_batch": "batch-1",
        "object_batch": "batch-1",
        "subject_version": ENTITY_VERSION,
        "object_version": ENTITY_VERSION,
        "existing": [],
    }

    result.update(changes)
    return result


def stored_relation():
    e = expected()

    return {
        "subject_key": e["subject_key"],
        "object_key": e["object_key"],
        "fact_id": e["fact_id"],
        "article_id": e["article_id"],
        "predicate": e["predicate"],
        "source_batch_key":
            e["source_batch_key"],
        "projection_version": VERSION,
    }


class CanonicalFactMaterializationTests(unittest.TestCase):

    def test_deterministic_projection_key(self):
        self.assertEqual(
            stable_key("batch-1", "vf-1"),
            stable_key("batch-1", "vf-1"),
        )

        self.assertNotEqual(
            stable_key("batch-1", "vf-1"),
            stable_key("batch-1", "vf-2"),
        )

    def test_valid_preflight(self):
        self.assertEqual(
            validate_observation(
                expected(),
                observed(),
            ),
            [],
        )

    def test_missing_fact(self):
        errors = validate_observation(
            expected(),
            observed(fact_count=0),
        )

        self.assertIn(
            "INVALID_FACT_COUNT", errors
        )

    def test_wrong_subject_identity(self):
        errors = validate_observation(
            expected(),
            observed(subject_resolved=False),
        )

        self.assertIn(
            "SUBJECT_RESOLVED_MISSING",
            errors,
        )

    def test_changed_predicate(self):
        errors = validate_observation(
            expected(),
            observed(predicate="OTHER"),
        )

        self.assertIn(
            "PREDICATE_MISMATCH",
            errors,
        )

    def test_changed_batch(self):
        errors = validate_observation(
            expected(),
            observed(
                object_batch="different-batch"
            ),
        )

        self.assertIn(
            "OBJECT_BATCH_MISMATCH",
            errors,
        )

    def test_preexisting_correct_relation(self):
        errors = validate_observation(
            expected(),
            observed(
                existing=[stored_relation()]
            ),
            require_materialized=True,
        )

        self.assertEqual(errors, [])

    def test_preexisting_wrong_endpoint(self):
        relation = stored_relation()
        relation["object_key"] = "ce-other"

        errors = validate_observation(
            expected(),
            observed(existing=[relation]),
        )

        self.assertIn(
            "EXISTING_FACT_RELATION_CONFLICT",
            errors,
        )

    def test_duplicate_projection_key(self):
        relation = stored_relation()

        errors = validate_observation(
            expected(),
            observed(
                existing=[relation, relation]
            ),
        )

        self.assertIn(
            "DUPLICATE_FACT_PROJECTION_KEY",
            errors,
        )

    def test_verify_requires_existing_relation(self):
        errors = validate_observation(
            expected(),
            observed(),
            require_materialized=True,
        )

        self.assertIn(
            "EXPECTED_FACT_RELATION_MISSING",
            errors,
        )

    def test_batches(self):
        rows = list(range(251))
        result = list(batches(rows, 100))

        self.assertEqual(
            [len(batch) for batch in result],
            [100, 100, 51],
        )


if __name__ == "__main__":
    unittest.main()
