
import unittest

from presskg.resolution.projection_readiness import (
    APPROVED,
    CONTEXTUAL,
    build_reference_inventory,
    independent_embedded_entities,
    identity_risks,
    proper_person_name,
    descriptive_activity,
)


def profile(surface, kind="Organization"):
    return {
        "surface": surface,
        "source_kind": kind,
        "profile_scope": "mention_occurrence",
        "article_id": "article-1",
        "fact_context": {},
    }


def assignment(status=APPROVED):
    return {
        "gate_status": status,
    }


class ProjectionReadinessTests(unittest.TestCase):

    def test_valid_person_name(self):
        self.assertTrue(
            proper_person_name("Mario Rossi")
        )
        self.assertTrue(
            proper_person_name("Vincenzo De Luca")
        )

    def test_descriptive_person_not_a_full_name(self):
        self.assertFalse(
            proper_person_name("Due amministratori")
        )
        self.assertFalse(
            proper_person_name("Sindaco di Scafati")
        )

    def test_composite_entities(self):
        inventory = {
            "ente idrico campano": "id-1",
            "gori": "id-2",
            "regione campania": "id-3",
        }

        result = independent_embedded_entities(
            "Ente Idrico Campano e Gori con Regione Campania",
            "id-4",
            inventory,
        )

        self.assertEqual(len(result), 3)

    def test_coordination_alone_is_not_evidence(self):
        result = independent_embedded_entities(
            "Ricerca e sviluppo",
            "id-1",
            {},
        )

        self.assertEqual(result, [])

    def test_descriptive_activity(self):
        self.assertTrue(
            descriptive_activity(
                "Scelta di Mario Rossi sul controllo delle fonti",
                "ACTIVITY",
            )
        )

    def test_short_event_name_not_downgraded(self):
        self.assertFalse(
            descriptive_activity(
                "Festival delle Acque",
                "ACTIVITY",
            )
        )

    def test_invalid_person_identity_downgraded(self):
        profiles = {
            "p1": profile(
                "Sindaco di Scafati",
                "Person",
            ),
        }
        rows = {"p1": assignment()}

        manifest = {
            "surface_counts": {
                "Sindaco di Scafati": 1,
            },
            "semantic_facet_counts": {
                "PERSON": 1,
            },
        }

        status, reasons, _ = identity_risks(
            "identity-1",
            ["p1"],
            manifest,
            profiles,
            rows,
            {},
        )

        self.assertEqual(status, CONTEXTUAL)
        self.assertIn(
            "person_without_full_name_anchor",
            reasons,
        )

    def test_generic_organization_downgraded(self):
        profiles = {
            "p1": profile("Giunta regionale"),
        }
        rows = {"p1": assignment()}

        manifest = {
            "surface_counts": {
                "Giunta regionale": 1,
            },
            "semantic_facet_counts": {
                "ORGANIZATION": 1,
            },
        }

        status, reasons, _ = identity_risks(
            "identity-1",
            ["p1"],
            manifest,
            profiles,
            rows,
            {},
        )

        self.assertEqual(status, CONTEXTUAL)

    def test_repeated_entity_inventory(self):
        manifest = {
            "id-1": {
                "gate_status": APPROVED,
                "approved_occurrences": 4,
                "surface_counts": {
                    "Acme S.p.A.": 4,
                },
            },
            "id-2": {
                "gate_status": CONTEXTUAL,
                "approved_occurrences": 0,
                "surface_counts": {
                    "Organizzazione generica": 2,
                },
            },
        }

        inventory = build_reference_inventory(
            {},
            {},
            manifest,
        )

        self.assertIn("acme s p a", inventory)
        self.assertNotIn(
            "organizzazione generica",
            inventory,
        )


if __name__ == "__main__":
    unittest.main()
