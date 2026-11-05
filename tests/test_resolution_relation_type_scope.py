
import unittest

from presskg.resolution.relation_type_scope import (
    assess,
    direction_assessment,
    scope_assessment,
    type_assessment,
)


def example(
    subject="Pierfrancesco Latini",
    subject_kind="Person",
    predicate="WORKS_FOR",
    object_="Acea",
    object_kind="Company",
    grounding="BOTH_GROUNDED",
    cue="LEXICAL_CUE_FOUND",
):
    return {
        "evidence_id": "f1:WORKS_FOR",
        "fact_id": "f1",
        "claim_id": "c1",
        "article_id": "a1",
        "subject_profile_id": "f1:subject",
        "subject_surface": subject,
        "subject_kind": subject_kind,
        "object_surface": object_,
        "object_kind": object_kind,
        "predicate": predicate,
        "attribute_kind": "employer",
        "status": "GROUNDED_CUE_CANDIDATE",
        "original_disposition": "PROFILE_EVIDENCE_CANDIDATE",
        "argument_grounding": {
            "status": grounding,
            "evidence": {
                "excerpt": (
                    f"{subject} lavora per {object_}."
                ),
            },
        },
        "predicate_grounding": {
            "status": cue,
            "cue": "lavora per" if cue == "LEXICAL_CUE_FOUND" else None,
        },
        "scope_assessment": {
            "status": "SCOPE_UNVERIFIED",
            "scope": "PROFESSIONAL_TEMPORAL",
            "reasons": [],
            "scope_verified": False,
        },
        "publication_date": "2024-01-01",
        "source_fact_validated": True,
        "attribute_semantically_verified": False,
        "match_constraint_approved": False,
    }


class RelationTypeScopeTests(unittest.TestCase):

    def test_professional_relation_is_compatible(self):
        result = assess(example())

        self.assertEqual(
            result["type_assessment"]["status"],
            "COMPATIBLE",
        )

        self.assertEqual(
            result["matching_disposition"],
            "SUPPORTING_CANDIDATE",
        )

        self.assertFalse(
            result["hard_identity_constraint"],
        )

        self.assertFalse(
            result["attribute_semantically_verified"],
        )

    def test_professional_relation_is_temporal(self):
        result = scope_assessment(example())

        self.assertEqual(
            result["status"],
            "SCOPE_UNVERIFIED",
        )

        self.assertEqual(
            result["scope"],
            "TEMPORAL_ROLE",
        )

    def test_municipality_to_street_is_blocked(self):
        record = example(
            subject="Comune di San Giuseppe Vesuviano",
            subject_kind="PublicAuthority",
            predicate="LOCATED_IN",
            object_="Via Lavinaio",
            object_kind="Location",
        )

        result = assess(record)

        self.assertEqual(
            result["direction_assessment"]["status"],
            "SUSPECTED_INVERSION",
        )

        self.assertEqual(
            result["matching_disposition"],
            "BLOCKED",
        )

    def test_industry_is_not_geography(self):
        record = example(
            subject="Lubox Imballaggi s.r.l.",
            subject_kind="Company",
            predicate="OPERATES_IN",
            object_="lavorazione di carta e cartone",
            object_kind="Topic",
        )

        result = assess(record)

        self.assertEqual(
            result["type_assessment"]["status"],
            "INCOMPATIBLE",
        )

        self.assertIn(
            "object_describes_industry_not_geography",
            result["type_assessment"]["reasons"],
        )

    def test_infrastructure_extent_is_held(self):
        record = example(
            subject="Strada Statale 145",
            subject_kind="Infrastructure",
            predicate="LOCATED_IN",
            object_="Castellammare di Stabia",
            object_kind="GPE",
        )

        result = assess(record)

        self.assertEqual(
            result["scope_assessment"]["status"],
            "ENTITY_EXTENT_RISK",
        )

        self.assertEqual(
            result["matching_disposition"],
            "BLOCKED",
        )

    def test_facility_localization_is_plausible(self):
        record = example(
            subject="Feudo Grande",
            subject_kind="Facility",
            predicate="LOCATED_IN",
            object_="Marigliano",
            object_kind="GPE",
        )

        result = assess(record)

        self.assertEqual(
            result["type_assessment"]["status"],
            "COMPATIBLE",
        )

        self.assertEqual(
            result["direction_assessment"]["status"],
            "PLAUSIBLE",
        )

        self.assertFalse(
            result["scope_verified"],
        )

    def test_organization_operating_in_gpe(self):
        record = example(
            subject="Gori",
            subject_kind="Company",
            predicate="OPERATES_IN",
            object_="Scafati",
            object_kind="GPE",
        )

        self.assertEqual(
            type_assessment(record)["status"],
            "COMPATIBLE",
        )

        self.assertEqual(
            scope_assessment(record)["scope"],
            "OPERATIONAL_GEOGRAPHY",
        )

    def test_unknown_type_is_not_a_contradiction(self):
        record = example(
            subject="Gori",
            subject_kind="UnknownType",
            predicate="OPERATES_IN",
            object_="Scafati",
            object_kind="GPE",
        )

        self.assertEqual(
            type_assessment(record)["status"],
            "UNKNOWN",
        )

        self.assertEqual(
            assess(record)["matching_disposition"],
            "REVIEW_REQUIRED",
        )

    def test_missing_cue_does_not_confirm_relation(self):
        record = example(
            cue="NO_EXPLICIT_CUE",
        )

        self.assertEqual(
            assess(record)["matching_disposition"],
            "REVIEW_REQUIRED",
        )

    def test_partial_grounding_is_reviewed(self):
        record = example(
            grounding="PARTIAL_GROUNDING",
            cue="NOT_EVALUATED",
        )

        self.assertEqual(
            assess(record)["matching_disposition"],
            "REVIEW_REQUIRED",
        )

    def test_reverse_professional_direction(self):
        record = example(
            subject="Acea",
            subject_kind="Company",
            predicate="WORKS_FOR",
            object_="Pierfrancesco Latini",
            object_kind="Person",
        )

        self.assertEqual(
            direction_assessment(record)["status"],
            "SUSPECTED_INVERSION",
        )

        self.assertEqual(
            assess(record)["matching_disposition"],
            "BLOCKED",
        )

    def test_original_scope_hold_is_preserved(self):
        record = example()
        record["original_disposition"] = "HELD_COMPOSITE_SCOPE"

        result = assess(record)

        self.assertEqual(
            result["matching_disposition"],
            "BLOCKED",
        )

        self.assertIn(
            "original_scope_hold",
            result["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
