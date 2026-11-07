
import unittest

from presskg.resolution.canonical_projection_dry_run import (
    classify_graph_match,
    create_projection_plan,
    semantic_warnings,
)


def source():
    return {
        "profile_id": "p1",
        "mention_id": "vf-1:subject",
        "fact_id": "vf-1",
        "article_id": "article-1",
        "role": "subject",
        "source_kind": "Person",
        "surface": "Mario Rossi",
    }


def observed(**changes):
    result = {
        "profile_id": "p1",
        "mention_count": 1,
        "fact_count": 1,
        "fact_linked": True,
        "mention_article_id": "article-1",
        "fact_article_id": None,
        "mention_fact_id": "vf-1",
        "mention_role": "subject",
        "article_linked": False,
    }
    result.update(changes)
    return result


class ProjectionDryRunTests(unittest.TestCase):

    def test_valid_graph_match(self):
        status, reasons = classify_graph_match(
            source(), observed()
        )
        self.assertEqual(status, "READY")
        self.assertEqual(reasons, [])

    def test_missing_mention_is_invalid(self):
        status, reasons = classify_graph_match(
            source(),
            observed(mention_count=0),
        )
        self.assertEqual(status, "INVALID")
        self.assertIn("mention_missing", reasons)

    def test_duplicate_mention_is_invalid(self):
        status, reasons = classify_graph_match(
            source(),
            observed(mention_count=2),
        )
        self.assertEqual(status, "INVALID")
        self.assertIn(
            "duplicate_mention_id", reasons
        )

    def test_missing_fact_is_invalid(self):
        status, reasons = classify_graph_match(
            source(),
            observed(fact_count=0),
        )
        self.assertEqual(status, "INVALID")
        self.assertIn("fact_missing", reasons)

    def test_broken_fact_link_requires_review(self):
        status, reasons = classify_graph_match(
            source(),
            observed(fact_linked=False),
        )
        self.assertEqual(status, "REVIEW")
        self.assertIn(
            "mention_fact_link_unverified", reasons
        )

    def test_wrong_article_requires_review(self):
        status, reasons = classify_graph_match(
            source(),
            observed(
                mention_article_id="article-2"
            ),
        )
        self.assertEqual(status, "REVIEW")
        self.assertIn(
            "article_id_mismatch", reasons
        )

    def test_missing_article_evidence(self):
        status, reasons = classify_graph_match(
            source(),
            observed(
                mention_article_id=None,
                fact_article_id=None,
                article_linked=False,
            ),
        )
        self.assertEqual(status, "REVIEW")
        self.assertIn(
            "article_provenance_unverified",
            reasons,
        )

    def test_person_role_description(self):
        row = source()
        row["surface"] = "Sindaco di Scafati"
        warnings = semantic_warnings(row)

        self.assertIn(
            "role_description_as_person",
            warnings,
        )

    def test_regular_person_name(self):
        self.assertEqual(
            semantic_warnings(source()), []
        )

    def test_complete_identity_is_planned(self):
        manifest = {
            "identity-1": {
                "approved_profile_ids": ["p1"],
                "semantic_facet_counts": {"PERSON": 1},
                "surface_counts": {"Mario Rossi": 1},
            }
        }
        rows = [{
            "profile_id": "p1",
            "proposed_identity_id": "identity-1",
            "graph_status": "READY",
        }]

        plan, incomplete = create_projection_plan(
            manifest, rows
        )

        self.assertEqual(len(plan), 1)
        self.assertEqual(incomplete, [])

    def test_partial_identity_is_not_planned(self):
        manifest = {
            "identity-1": {
                "approved_profile_ids": ["p1", "p2"],
                "semantic_facet_counts": {"PERSON": 2},
                "surface_counts": {"Mario Rossi": 2},
            }
        }
        rows = [
            {
                "profile_id": "p1",
                "proposed_identity_id": "identity-1",
                "graph_status": "READY",
            },
            {
                "profile_id": "p2",
                "proposed_identity_id": "identity-1",
                "graph_status": "REVIEW",
            },
        ]

        plan, incomplete = create_projection_plan(
            manifest, rows
        )

        self.assertEqual(plan, [])
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(
            incomplete[0]["unverified_profile_ids"],
            ["p2"],
        )


if __name__ == "__main__":
    unittest.main()
