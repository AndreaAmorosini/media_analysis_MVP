
import unittest

from presskg.resolution.materialization_safety_gate import (
    SAFE,
    REVIEW,
    evaluate_identity,
    classed_as_role_description,
    relative_institutional_reference,
    generic_project_reference,
    repeatable_event_reference,
)


def identity(surface, facet="ORGANIZATION"):
    return {
        "proposed_identity_id": "pi-test",
        "surface_counts": {surface: 3},
        "semantic_facet_counts": {facet: 3},
    }


def occurrence(kind, article="article-1", warnings=None):
    return {
        "profile_id": "profile-" + article,
        "source_kind": kind,
        "article_id": article,
        "semantic_warnings": warnings or [],
    }


class SafetyGateTests(unittest.TestCase):

    def test_institutional_role(self):
        self.assertTrue(
            classed_as_role_description(
                "Sindaco di Scafati",
                "ORGANIZATION",
            )
        )

    def test_personal_name_not_role(self):
        self.assertFalse(
            classed_as_role_description(
                "Michele di Bari",
                "PERSON",
            )
        )

    def test_relative_institution(self):
        self.assertTrue(
            relative_institutional_reference(
                "Giunta regionale precedente",
                "ORGANIZATION",
            )
        )

    def test_named_institution(self):
        self.assertFalse(
            relative_institutional_reference(
                "Regione Campania",
                "ORGANIZATION",
            )
        )

    def test_generic_project(self):
        self.assertTrue(
            generic_project_reference(
                "Lavori di completamento della rete fognaria",
                {"Project"},
            )
        )

    def test_specific_project(self):
        self.assertFalse(
            generic_project_reference(
                "Progetto di bonifica del fiume Sarno",
                {"Project"},
            )
        )

    def test_repeatable_event(self):
        self.assertTrue(
            repeatable_event_reference(
                "Inaugurazione Punto di Contatto",
                {"Event"},
            )
        )

    def test_company_is_safe(self):
        result = evaluate_identity(
            identity("Acme S.p.A."),
            [
                occurrence("Company", "article-1"),
                occurrence("Company", "article-2"),
            ],
        )

        self.assertEqual(result["status"], SAFE)

    def test_institutional_office_is_safe(self):
        result = evaluate_identity(
            identity("Prefetto di Napoli"),
            [
                occurrence(
                    "PublicAuthority", "article-1"
                ),
                occurrence(
                    "PublicAuthority", "article-2"
                ),
            ],
        )

        self.assertEqual(result["status"], SAFE)
        self.assertIn(
            "INSTITUTIONAL_OFFICE_NOT_PERSON",
            result["advisory_warnings"],
        )

    def test_office_misclassified_as_person_is_held(self):
        result = evaluate_identity(
            identity(
                "Prefetto di Napoli", "PERSON"
            ),
            [
                occurrence(
                    "Person", "article-1"
                ),
                occurrence(
                    "Person", "article-2"
                ),
            ],
        )

        self.assertEqual(result["status"], REVIEW)

    def test_office_without_independent_support_is_held(self):
        result = evaluate_identity(
            identity("Prefetto di Napoli"),
            [occurrence("PublicAuthority")],
        )

        self.assertEqual(result["status"], REVIEW)

    def test_project_warning_is_advisory(self):
        result = evaluate_identity(
            identity(
                "Progetto di bonifica del fiume Sarno",
                "ACTIVITY",
            ),
            [
                occurrence(
                    "Project", "article-1",
                    ["descriptive_project_name"],
                ),
                occurrence(
                    "Project", "article-2",
                    ["descriptive_project_name"],
                ),
            ],
        )

        self.assertEqual(result["status"], SAFE)
        self.assertIn(
            "descriptive_project_name",
            result["advisory_warnings"],
        )

    def test_generic_project_held(self):
        result = evaluate_identity(
            identity(
                "Lavori di completamento della rete fognaria",
                "ACTIVITY",
            ),
            [occurrence("Project")],
        )

        self.assertEqual(result["status"], REVIEW)
        
    def test_event_project_conflict(self):
        result = evaluate_identity(
            identity("Intervento Alfa", "ACTIVITY"),
            [
                occurrence("Event", "article-1"),
                occurrence("Project", "article-2"),
            ],
        )

        self.assertEqual(result["status"], REVIEW)
        self.assertIn(
            "MIXED_EVENT_PROJECT_IDENTITY",
            result["hold_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
