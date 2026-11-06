
import unittest

from presskg.resolution.identity_integrity_gate import (
    APPROVED,
    BLOCKED,
    CONTEXTUAL,
    has_legal_form,
    plausible_full_person_name,
    short_name_matches,
    identity_strength,
    validate_person_coreference,
)


def profile(
    pid,
    surface,
    kind,
    article="article-1",
    predicate="WORKS_FOR",
    counterpart="Acme",
):
    return {
        "profile_id": pid,
        "surface": surface,
        "source_kind": kind,
        "article_id": article,
        "role": "subject",
        "fact_context": {
            "predicate": predicate,
            "counterpart_name": counterpart,
        },
    }


def assignment(facet, warnings=None, cluster=None):
    return {
        "occurrence_semantic_facet": facet,
        "phase4d_warnings": warnings or [],
        "surface_cluster_id": cluster,
    }


class IntegrityGateTests(unittest.TestCase):

    def test_legal_variants(self):
        for surface in (
            "Acme Spa",
            "Acme S.p.A.",
            "Acme S.r.l.",
            "Acmespa",
        ):
            with self.subTest(surface=surface):
                self.assertTrue(
                    has_legal_form(surface)
                )

    def test_descriptive_person_rejected(self):
        self.assertFalse(
            plausible_full_person_name(
                "Due amministratori"
            )
        )

    def test_real_full_person_name(self):
        self.assertTrue(
            plausible_full_person_name(
                "Mario Rossi"
            )
        )
        self.assertTrue(
            plausible_full_person_name(
                "Vincenzo De Luca"
            )
        )

    def test_surname_suffix(self):
        self.assertTrue(
            short_name_matches(
                "De Luca",
                "Vincenzo De Luca",
            )
        )

    def test_descriptive_person_coreference_rejected(self):
        short = profile(
            "p-short",
            "amministratori",
            "Person",
        )
        fake_full = profile(
            "p-full",
            "Due amministratori",
            "Person",
        )

        # The fake full name has not passed the
        # personal-name anchor validator.
        anchors = {
            "article-1": {},
        }

        supported, reason = validate_person_coreference(
            short,
            "identity-1",
            {
                "p-short": short,
                "p-full": fake_full,
            },
            anchors,
        )

        self.assertFalse(supported)
        self.assertEqual(
            reason,
            "missing_or_competing_person_anchor",
        )

    def test_person_shared_relation(self):
        short = profile(
            "p-short",
            "Rossi",
            "Person",
        )
        full = profile(
            "p-full",
            "Mario Rossi",
            "Person",
        )

        supported, reason = validate_person_coreference(
            short,
            "identity-1",
            {
                "p-short": short,
                "p-full": full,
            },
            {
                "article-1": {
                    "identity-1": ["p-full"],
                }
            },
        )

        self.assertTrue(supported)
        self.assertEqual(
            reason,
            "same_article_shared_relation",
        )

    def test_person_without_shared_relation_held(self):
        short = profile(
            "p-short",
            "Rossi",
            "Person",
            predicate="CRITICIZES",
        )
        full = profile(
            "p-full",
            "Mario Rossi",
            "Person",
            predicate="WORKS_FOR",
        )

        supported, reason = validate_person_coreference(
            short,
            "identity-1",
            {
                "p-short": short,
                "p-full": full,
            },
            {
                "article-1": {
                    "identity-1": ["p-full"],
                }
            },
        )

        self.assertFalse(supported)
        self.assertEqual(
            reason,
            "coreference_without_relation_corroboration",
        )

    def test_repeated_company_with_legal_form(self):
        profiles = {
            "p1": profile(
                "p1", "Acme", "Company", "a1"
            ),
            "p2": profile(
                "p2", "Acme S.p.A.", "Company", "a2"
            ),
        }

        rows = {
            "p1": assignment("ORGANIZATION"),
            "p2": assignment("ORGANIZATION"),
        }

        status, reasons = identity_strength(
            "identity-1",
            ["p1", "p2"],
            profiles,
            rows,
        )

        self.assertEqual(status, APPROVED)
        self.assertIn(
            "observed_legal_form",
            reasons,
        )

    def test_generic_one_word_organization_held(self):
        profiles = {
            "p1": profile(
                "p1", "Giunta", "Organization", "a1"
            ),
            "p2": profile(
                "p2", "Giunta", "Organization", "a2"
            ),
        }

        rows = {
            "p1": assignment("ORGANIZATION"),
            "p2": assignment("ORGANIZATION"),
        }

        status, _ = identity_strength(
            "identity-1",
            ["p1", "p2"],
            profiles,
            rows,
        )

        self.assertEqual(status, CONTEXTUAL)

    def test_mixed_semantic_families_blocked(self):
        profiles = {
            "p1": profile(
                "p1", "Acme", "Company", "a1"
            ),
            "p2": profile(
                "p2", "Acme", "Person", "a2"
            ),
        }

        rows = {
            "p1": assignment("ORGANIZATION"),
            "p2": assignment("PERSON"),
        }

        status, _ = identity_strength(
            "identity-1",
            ["p1", "p2"],
            profiles,
            rows,
        )

        self.assertEqual(status, BLOCKED)

    def test_warning_prevents_global_projection(self):
        profiles = {
            "p1": profile(
                "p1", "Acme", "Company", "a1"
            ),
            "p2": profile(
                "p2", "Acme S.p.A.", "Company", "a2"
            ),
        }

        rows = {
            "p1": assignment(
                "ORGANIZATION",
                warnings=["ocr_segmentation_not_corroborated"],
            ),
            "p2": assignment("ORGANIZATION"),
        }

        status, _ = identity_strength(
            "identity-1",
            ["p1", "p2"],
            profiles,
            rows,
        )

        self.assertEqual(status, CONTEXTUAL)


if __name__ == "__main__":
    unittest.main()
