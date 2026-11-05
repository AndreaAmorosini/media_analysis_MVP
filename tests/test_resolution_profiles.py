
import unittest

from presskg.resolution.profiles import (
    make_component_profile,
    make_mention_profile,
    profile_status,
    type_hints,
    validate_mention_row,
)


def example_row():
    return {
        "mention_id": "vf-1:subject",
        "mention_fact_id": "vf-1",
        "mention_article_id": "article-1",
        "mention_claim_id": "claim-1",
        "mention_batch_id": "batch-1",
        "fact_id": "vf-1",
        "article_id": "article-1",
        "claim_id": "claim-1",
        "fact_batch_id": "batch-1",
        "role": "subject",
        "name": (
            "Ente Idrico Campano, "
            "Regione Campania e Gori"
        ),
        "source_kind": "Organization",
        "predicate": "COLLABORATES_WITH",
        "counterpart_name": "Progetto Alfa",
        "counterpart_kind": "Project",
        "representation": "original",
        "semantic_family": "collaboration",
        "modality": "asserted",
        "attribution": None,
        "temporal_status": None,
        "publication_date": "2023-06-21",
        "original_quote": "Citazione originale.",
        "supporting_span": "Citazione originale.",
        "article_title": "Articolo di prova",
        "article_source": "Test",
    }


def example_component():
    return {
        "candidate_id": "vf-1:subject:2",
        "mention_id": "vf-1:subject",
        "fact_id": "vf-1",
        "article_id": "article-1",
        "role": "subject",
        "component_index": 2,
        "surface": "Gori",
        "identity_surface": "Gori",
        "parent_kind": "Organization",
        "mention_start": 38,
        "mention_end": 42,
        "eligibility": "CANDIDATE",
        "reason": "named_referent_candidate",
        "source_classification": "MULTI_ENTITY",
    }


class IdentityProfileTests(unittest.TestCase):

    def test_valid_source_row(self):
        validate_mention_row(
            example_row(), "batch-1"
        )

    def test_reject_wrong_batch(self):
        with self.assertRaises(ValueError):
            validate_mention_row(
                example_row(), "other-batch"
            )

    def test_original_occurrence_preserved(self):
        profile = make_mention_profile(
            example_row()
        )

        self.assertEqual(
            profile["profile_scope"],
            "mention_occurrence",
        )
        self.assertEqual(
            profile["surface"],
            example_row()["name"],
        )
        self.assertIsNone(
            profile["canonical_id"]
        )
        self.assertFalse(
            profile["merge_approved"]
        )

    def test_project_can_be_profiled(self):
        self.assertEqual(
            profile_status("Project", None),
            "PROFILE_ONLY_UNANALYZED",
        )

    def test_named_event_can_be_profiled(self):
        self.assertEqual(
            profile_status("Event", None),
            "PROFILE_ONLY_UNANALYZED",
        )

    def test_quantity_excluded_from_identity(self):
        self.assertEqual(
            profile_status("Money", None),
            "NON_IDENTITY",
        )

    def test_component_type_is_not_final(self):
        parent = make_mention_profile(
            example_row()
        )

        component = make_component_profile(
            example_component(),
            parent,
        )

        self.assertEqual(
            component["source_kind"],
            "Organization",
        )
        self.assertIsNone(
            component["resolved_type"]
        )
        self.assertEqual(
            component["surface"],
            "Gori",
        )
        self.assertEqual(
            component["profile_scope"],
            "decomposed_component",
        )

    def test_held_project_is_reassessed(self):
        parent = make_mention_profile(
            example_row()
        )

        component = {
            **example_component(),
            "candidate_id": "vf-1:subject:0",
            "parent_kind": "Project",
            "eligibility": "HELD",
        }

        profile = make_component_profile(
            component, parent
        )

        self.assertEqual(
            profile["resolution_status"],
            "PROFILE_ONLY_REASSESS",
        )

    def test_lexical_type_hint_is_not_final(self):
        hints = type_hints(
            "Regione Campania", "Organization"
        )

        self.assertTrue(
            any(
                h["kind"] == "PublicAuthority"
                and h["origin"] == "lexical_pattern"
                and h["strength"] == "weak"
                for h in hints
            )
        )


if __name__ == "__main__":
    unittest.main()
