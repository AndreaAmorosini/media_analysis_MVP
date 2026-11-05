
import unittest

from presskg.resolution.attributes import (
    contains_surface,
    lexical_signals,
    make_relation_record,
    process_profile,
    quote_coverage,
)


def make_profile(
    *,
    name="Gori",
    kind="Company",
    predicate="COLLABORATES_WITH",
    target="Ente Idrico Campano",
    target_kind="PublicAuthority",
    quote=None,
    role="subject",
    scope="mention_occurrence",
    status="PROFILE_ONLY_UNANALYZED",
):
    if quote is None:
        quote = (
            f"{name} collabora con "
            f"{target}."
        )

    return {
        "profile_id": "vf-1:subject",
        "profile_scope": scope,
        "mention_id": "vf-1:subject",
        "fact_id": "vf-1",
        "claim_id": "claim-1",
        "article_id": "article-1",
        "role": role,
        "surface": name,
        "source_kind": kind,
        "resolution_status": status,
        "canonical_id": None,
        "merge_approved": False,

        "fact_context": {
            "predicate": predicate,
            "counterpart_name": target,
            "counterpart_kind": target_kind,
            "representation": "original",
            "modality": "asserted",
            "temporal_status": "ongoing",
        },

        "provenance": {
            "original_quote": quote,
            "supporting_span": quote,
            "publication_date": "2024-02-07",
            "article_title": "Articolo di prova",
            "article_source": None,
        },
    }


class IdentityAttributeTests(unittest.TestCase):

    def test_full_quote_coverage(self):
        profile = make_profile()

        coverage = quote_coverage(profile)

        self.assertEqual(
            coverage["status"],
            "BOTH_ARGUMENTS_LEXICAL",
        )

    def test_partial_quote_coverage(self):
        profile = make_profile(
            quote="Gori ha annunciato alcuni lavori."
        )

        coverage = quote_coverage(profile)

        self.assertEqual(
            coverage["status"],
            "PARTIAL_ARGUMENTS_LEXICAL",
        )

    def test_no_false_substring_match(self):
        self.assertFalse(
            contains_surface(
                "categorie editoriali",
                "Gori",
            )
        )

    def test_case_insensitive_match(self):
        self.assertTrue(
            contains_surface(
                "GORI collabora con altri enti",
                "Gori",
            )
        )

    def test_workplace_attribute(self):
        profile = make_profile(
            name="Mario Rossi",
            kind="Person",
            predicate="WORKS_FOR",
            target="Gori",
            target_kind="Company",
        )

        relation = make_relation_record(profile)

        self.assertEqual(
            relation["category"],
            "identity_attribute_candidate",
        )

        self.assertEqual(
            relation["attribute_kind"],
            "employer",
        )

        self.assertEqual(
            relation["disposition"],
            "PROFILE_EVIDENCE_CANDIDATE",
        )

        self.assertFalse(
            relation["match_constraint_approved"],
        )

    def test_located_in_is_not_global_identity(self):
        profile = make_profile(
            name="Pozzano",
            kind="Location",
            predicate="LOCATED_IN",
            target="Castellammare di Stabia",
            target_kind="GPE",
        )

        relation = make_relation_record(profile)

        self.assertEqual(
            relation["attribute_kind"],
            "geographic_context",
        )

        self.assertFalse(
            relation["attribute_verified"],
        )

    def test_collaboration_is_contextual(self):
        relation = make_relation_record(
            make_profile()
        )

        self.assertEqual(
            relation["category"],
            "contextual_relation",
        )

        self.assertIsNone(
            relation["attribute_kind"]
        )

    def test_other_predicate_not_inferred(self):
        profile = make_profile(
            predicate="OTHER"
        )

        self.assertIsNone(
            make_relation_record(profile)
        )

    def test_object_side_does_not_duplicate_fact(self):
        profile = make_profile(
            role="object"
        )

        self.assertIsNone(
            make_relation_record(profile)
        )

    def test_no_fact_distribution_to_components(self):
        profile = make_profile(
            name="Gori",
            scope="decomposed_component",
        )

        self.assertIsNone(
            make_relation_record(profile)
        )

    def test_composite_mention_held(self):
        profile = make_profile(
            name="Gori e Regione Campania",
            target="Progetto Alfa",
            status="COMPOSITE_MENTION_HOLD",
            predicate="WORKS_ON",
        )

        relation = make_relation_record(profile)

        self.assertEqual(
            relation["disposition"],
            "HELD_COMPOSITE_SCOPE",
        )

    def test_legal_form_extraction(self):
        profile = make_profile(
            name="Acme S.p.A.",
        )

        signals = lexical_signals(profile)

        self.assertEqual(
            len(signals),
            1,
        )

        self.assertEqual(
            signals[0]["value"],
            "S.p.A.",
        )

        self.assertFalse(
            signals[0]["identity_verified"],
        )

    def test_no_automatic_merge(self):
        profile = make_profile(
            name="Mario Rossi",
            kind="Person",
            predicate="WORKS_FOR",
            target="Gori",
        )

        signals, relation = process_profile(
            profile
        )

        self.assertEqual(signals, [])

        self.assertFalse(
            relation["match_constraint_approved"]
        )

        self.assertFalse(
            relation["fact_expansion_allowed"]
        )


if __name__ == "__main__":
    unittest.main()
