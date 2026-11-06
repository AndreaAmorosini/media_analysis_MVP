
import unittest

from presskg.resolution.coreference_audit import (
    joined_article_base,
    legal_base,
    short_name_of,
    relation_signature,
    local_quote_contains,
    resolve_person,
    refine_one,
)


def profile(surface, kind="Company", article="article-1"):
    return {
        "profile_id": "p1",
        "surface": surface,
        "source_kind": kind,
        "article_id": article,
        "fact_id": "fact-1",
        "role": "subject",
        "profile_scope": "mention_occurrence",
        "fact_context": {
            "predicate": "WORKS_FOR",
            "counterpart_name": "Organizzazione Alfa",
        },
        "provenance": {
            "supporting_span": surface + " ha partecipato.",
            "original_quote": surface + " ha partecipato.",
        },
    }


class CoreferenceAuditTests(unittest.TestCase):

    def test_legal_form_normalization(self):
        names = [
            "Acme",
            "Acme Spa",
            "ACME S.p.A.",
            "Acmespa",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(legal_base(name), "acme")

    def test_camelcase_article(self):
        self.assertEqual(
            joined_article_base("LaGori"), "gori"
        )
        self.assertEqual(
            joined_article_base("IlConsorzio"), "consorzio"
        )

    def test_camelcase_is_only_a_candidate(self):
        self.assertEqual(
            joined_article_base("laGoriper"),
            "goriper",
        )

    def test_compound_entity_not_shortened(self):
        self.assertNotEqual(
            legal_base("Acme Educational"),
            legal_base("Acme"),
        )

    def test_person_name_suffix(self):
        self.assertTrue(
            short_name_of("Aliberti", "Pasquale Aliberti")
        )
        self.assertTrue(
            short_name_of("De Luca", "Vincenzo De Luca")
        )
        self.assertFalse(
            short_name_of("De Luca", "Mario Rossi")
        )

    def test_relation_signature(self):
        p = profile("Rossi", "Person")
        self.assertEqual(
            relation_signature(p),
            (
                "subject",
                "WORKS_FOR",
                "organizzazione alfa",
            ),
        )

    def test_local_quote_contains(self):
        p = profile("Rossi", "Person")
        self.assertTrue(
            local_quote_contains(p, "Rossi")
        )
        self.assertFalse(
            local_quote_contains(p, "Bianchi")
        )

    def test_unique_person_same_article(self):
        p = profile("Rossi", "Person")

        indexes = {
            "people_by_article": {
                "article-1": {
                    "mario rossi": {"identity-1"},
                },
            },
            "people_relations": {},
            "short_relations": {},
        }

        result = resolve_person(p, indexes)
        self.assertIsNotNone(result)
        self.assertEqual(result["identity"], "identity-1")
        self.assertEqual(
            result["rule"], "PERSON_UNIQUE_IN_ARTICLE"
        )

    def test_ambiguous_person_same_article(self):
        p = profile("Rossi", "Person")

        indexes = {
            "people_by_article": {
                "article-1": {
                    "mario rossi": {"identity-1"},
                    "luigi rossi": {"identity-2"},
                },
            },
            "people_relations": {},
            "short_relations": {},
        }

        self.assertIsNone(resolve_person(p, indexes))

    def test_unverified_ocr_is_reverted(self):
        p = profile("laGoriper")

        old = {
            "assignment_status": "PROVISIONAL",
            "proposed_identity_id": "merged-id",
            "occurrence_semantic_facet": "ORGANIZATION",
            "source_kind": "Company",
            "refinement_rule": "ORGANIZATION_OCR_JOINED_ARTICLE",
            "canonical_entity_id": None,
            "identity_assignment_approved": False,
            "fact_propagation_allowed": False,
        }

        original = {
            "proposed_identity_id": "original-id",
        }

        indexes = {
            "organizations": {},
            "legal_attested": set(),
        }

        result = refine_one(p, old, original, indexes)

        self.assertEqual(
            result["proposed_identity_id"], "original-id"
        )
        self.assertEqual(
            result["phase4d_rule"], "REVERT_UNGROUNDED_OCR"
        )
        
    def test_unverified_ocr_is_reverted(self):
        p = profile("laGoriper")

        old = {
            "assignment_status": "PROVISIONAL",
            "proposed_identity_id": "merged-id",
            "occurrence_semantic_facet": "ORGANIZATION",
            "source_kind": "Company",
            "refinement_rule": "ORGANIZATION_OCR_JOINED_ARTICLE",
            "canonical_entity_id": None,
            "identity_assignment_approved": False,
            "fact_propagation_allowed": False,
        }

        original = {
            "proposed_identity_id": "original-id",
        }

        indexes = {
            "organizations": {
                "goriper": "merged-id",
            },
            "legal_attested": set(),
        }

        result = refine_one(p, old, original, indexes)

        self.assertEqual(
            result["proposed_identity_id"],
            "original-id",
        )
        self.assertEqual(
            result["phase4d_rule"],
            "REVERT_UNGROUNDED_OCR",
        )


    def test_corroborated_camelcase_is_recovered(self):
        p = profile("LaAcme")

        old = {
            "assignment_status": "PROVISIONAL",
            "proposed_identity_id": "separate-id",
            "occurrence_semantic_facet": "ORGANIZATION",
            "source_kind": "Company",
            "refinement_rule": "UNCHANGED",
            "canonical_entity_id": None,
            "identity_assignment_approved": False,
            "fact_propagation_allowed": False,
        }

        indexes = {
            "organizations": {
                "acme": "canonical-proposal-id",
            },
            "legal_attested": {"acme"},
        }

        result = refine_one(
            p,
            old,
            {"proposed_identity_id": "separate-id"},
            indexes,
        )

        self.assertEqual(
            result["proposed_identity_id"],
            "canonical-proposal-id",
        )
        self.assertEqual(
            result["phase4d_rule"],
            "RECOVER_CAMELCASE_ARTICLE",
        )


if __name__ == "__main__":
    unittest.main()
