
import unittest

from presskg.resolution.contextual_identity_refinement import (
    organization_base,
    organization_base_candidates,
    compatible_short_name,
    relation_signature,
    resolve_person,
)


class ContextualIdentityRefinementTests(unittest.TestCase):

    def test_legal_forms(self):
        for name in (
            "Gori",
            "Gori Spa",
            "Gori S.p.A.",
            "GORI S.p.A.",
            "Gorispa",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    organization_base(name),
                    "gori",
                )

    def test_generic_company_variants(self):
        self.assertEqual(
            organization_base("Acme S.r.l."),
            "acme",
        )
        self.assertEqual(
            organization_base("Acmesrl"),
            "acme",
        )

    def test_separate_article(self):
        self.assertEqual(
            organization_base("la Gori"),
            "gori",
        )

    def test_joined_article_requires_known_base(self):
        result = organization_base_candidates(
            "LaGori",
            {"gori", "lagori"},
        )

        self.assertEqual(
            result,
            [("gori", "OCR_JOINED_ARTICLE")],
        )

    def test_no_invented_ocr_base(self):
        result = organization_base_candidates(
            "LaGori",
            {"lagori"},
        )

        self.assertEqual(
            result,
            [(
                "lagori",
                "NORMALIZED_ORGANIZATION_NAME",
            )],
        )

    def test_compound_entity_not_equivalent(self):
        self.assertNotEqual(
            organization_base("Gori Educational"),
            organization_base("Gori"),
        )

    def test_person_surname(self):
        self.assertTrue(
            compatible_short_name(
                "Aliberti", "Pasquale Aliberti"
            )
        )
        self.assertTrue(
            compatible_short_name(
                "De Luca", "Vincenzo De Luca"
            )
        )

    def test_unrelated_person(self):
        self.assertFalse(
            compatible_short_name(
                "De Luca", "Mario Rossi"
            )
        )

    def test_relation_signature_includes_role(self):
        profile = {
            "role": "subject",
            "fact_context": {
                "predicate": "CRITICIZES",
                "counterpart_name": "Regione Campania",
            },
        }

        self.assertEqual(
            relation_signature(profile),
            (
                "subject",
                "CRITICIZES",
                "regione campania",
            ),
        )

    def test_person_with_competing_full_names_is_held(self):
        profile = {
            "profile_id": "p1",
            "surface": "Rossi",
            "role": "subject",
            "fact_id": "f1",
            "article_id": "a1",
            "fact_context": {
                "predicate": "WORKS_FOR",
                "counterpart_name": "Acme",
            },
        }

        result = resolve_person(
            profile,
            {
                "mario rossi": "id-1",
                "luigi rossi": "id-2",
            },
            {},
            {},
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
