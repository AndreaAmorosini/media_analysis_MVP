
import unittest

from presskg.resolution.projection_readiness_refinement import (
    APPROVED,
    CONTEXTUAL,
    particle_person_name,
    recover_person,
    recover_project,
    recover_media_outlet,
    joined_coordination_entities,
    evaluate_identity,
)


def profile(pid, surface, kind, article, predicate="FUNDS",
            counterpart="Ente Alfa"):
    return {
        "profile_id": pid,
        "surface": surface,
        "source_kind": kind,
        "profile_scope": "mention_occurrence",
        "article_id": article,
        "role": "subject",
        "fact_context": {
            "predicate": predicate,
            "counterpart_name": counterpart,
        },
    }


class ProjectionReadinessRefinementTests(unittest.TestCase):

    def test_particle_person_name(self):
        names = {"michele", "mario"}
        self.assertTrue(
            particle_person_name("Michele di Bari", names)
        )
        self.assertFalse(
            particle_person_name("Sindaco di Scafati", names)
        )

    def test_particle_person_requires_known_first_name(self):
        self.assertFalse(
            particle_person_name(
                "Michele di Bari",
                set(),
            )
        )

    def test_person_recovery(self):
        profiles = {
            "p1": profile(
                "p1", "Michele di Bari", "Person", "a1"
            ),
            "p2": profile(
                "p2", "Michele di Bari", "Person", "a2"
            ),
        }
        gate = {
            pid: {"gate_status": APPROVED}
            for pid in profiles
        }
        self.assertTrue(
            recover_person(
                list(profiles), profiles, gate,
                {"michele"},
            )
        )

    def test_title_not_recovered_as_person(self):
        profiles = {
            "p1": profile(
                "p1", "Sindaco di Scafati", "Person", "a1"
            ),
            "p2": profile(
                "p2", "Sindaco di Scafati", "Person", "a2"
            ),
        }
        gate = {
            pid: {"gate_status": APPROVED}
            for pid in profiles
        }
        self.assertFalse(
            recover_person(
                list(profiles), profiles, gate,
                {"michele"},
            )
        )

    def test_repeated_project_with_distinct_relations(self):
        profiles = {
            "p1": profile(
                "p1", "Progetto Acquedotto Alfa",
                "Project", "a1",
                "FUNDS", "Ente Alfa",
            ),
            "p2": profile(
                "p2", "Progetto Acquedotto Alfa",
                "Project", "a2",
                "IMPLEMENTED_BY", "Societa Beta",
            ),
            "p3": profile(
                "p3", "Progetto Acquedotto Alfa",
                "Project", "a3",
                "LOCATED_IN", "Comune Gamma",
            ),
        }
        gate = {
            pid: {"gate_status": APPROVED}
            for pid in profiles
        }
        self.assertTrue(
            recover_project(list(profiles), profiles, gate)
        )

    def test_project_not_recovered_from_repetition_only(self):
        profiles = {
            f"p{i}": profile(
                f"p{i}", "Intervento senza ancore",
                "Project", f"a{i}", "OTHER", "Ente Alfa",
            )
            for i in range(3)
        }
        gate = {
            pid: {"gate_status": APPROVED}
            for pid in profiles
        }
        self.assertFalse(
            recover_project(list(profiles), profiles, gate)
        )

    def test_media_outlet_recovery(self):
        profiles = {
            "p1": profile("p1", "Il Quotidiano", "MediaOutlet", "a1"),
            "p2": profile("p2", "Il Quotidiano", "MediaOutlet", "a2"),
        }
        gate = {
            pid: {
                "gate_status": CONTEXTUAL,
                "gate_reasons": [
                    "article_stripped_unverified_name"
                ],
                "upstream_status": "PROVISIONAL",
            }
            for pid in profiles
        }
        self.assertTrue(
            recover_media_outlet(
                list(profiles), profiles, gate
            )
        )

    def test_media_outlet_with_other_blockers_not_recovered(self):
        profiles = {
            "p1": profile("p1", "Il Quotidiano", "MediaOutlet", "a1"),
            "p2": profile("p2", "Il Quotidiano", "MediaOutlet", "a2"),
        }
        gate = {
            pid: {
                "gate_status": CONTEXTUAL,
                "gate_reasons": ["single_article_identity"],
                "upstream_status": "PROVISIONAL",
            }
            for pid in profiles
        }
        self.assertFalse(
            recover_media_outlet(
                list(profiles), profiles, gate
            )
        )

    def test_ocr_concatenated_entities(self):
        inventory = {
            "eic": "entity-a",
            "gori": "entity-b",
        }
        self.assertEqual(
            joined_coordination_entities(
                "Regione, EiceGori", inventory
            ),
            [("entity-a", "entity-b")],
        )

    def test_no_ocr_merge_without_independent_anchors(self):
        self.assertEqual(
            joined_coordination_entities(
                "EiceGori", {"gori": "entity-b"}
            ),
            [],
        )

    def test_ocr_composite_demoted(self):
        manifest = {
            "readiness_status": APPROVED,
            "readiness_reasons": ["repeated_multiword_organization_name"],
            "surface_counts": {
                "EiceGori": 4
            },
        }
        result = evaluate_identity(
            "entity-c",
            manifest,
            [],
            {},
            {},
            set(),
            {"eic": "entity-a", "gori": "entity-b"},
        )
        self.assertEqual(result[0], CONTEXTUAL)
        self.assertEqual(
            result[1], "OCR_MULTI_ENTITY_COMPOSITE"
        )
        
    def test_professional_titles_not_person_names(self):
        first_names = {
            "michele", "mario", "sindaco", "presidente",
        }

        self.assertTrue(
            particle_person_name(
                "Michele di Bari",
                first_names,
            )
        )

        for surface in (
            "Sindaco di Scafati",
            "Sindaco di Capri",
            "Sindaco di Napoli",
            "Presidente di Regione",
        ):
            with self.subTest(surface=surface):
                self.assertFalse(
                    particle_person_name(
                        surface,
                        first_names,
                    )
                )


if __name__ == "__main__":
    unittest.main()
