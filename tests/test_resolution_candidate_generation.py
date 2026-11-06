
import unittest

from presskg.resolution.candidate_generation import (
    acronym,
    eligible,
    generate_pairs,
    legal_base,
    normalize,
    pair_types,
    significant_tokens,
)


def group(name, kinds=None):
    return {
        "group_id": f"test:{name}",
        "normalized_surface": normalize(name),
        "preferred_surface": name,
        "occurrence_count": 1,
        "kind_counts": {
            kind: 1 for kind in (kinds or ["Company"])
        },
    }


def build_groups(*names):
    return {
        normalize(name): group(name)
        for name in names
    }


class CandidateGenerationTests(unittest.TestCase):

    def test_normalization(self):
        self.assertEqual(
            normalize("  GÓRI, S.p.A. "),
            "gori s p a",
        )

    def test_legal_form(self):
        self.assertEqual(
            legal_base("GORI S.p.A."),
            legal_base("Gori"),
        )

    def test_acronym(self):
        self.assertEqual(
            acronym("Ente Idrico Campano"),
            "eic",
        )

    def test_significant_tokens(self):
        self.assertEqual(
            significant_tokens(
                "Ministero delle Infrastrutture"
            ),
            ("ministero", "infrastrutture"),
        )

    def test_legal_name_candidate(self):
        groups = build_groups(
            "Gori",
            "GORI S.p.A.",
        )

        pairs, _ = generate_pairs(groups)

        self.assertEqual(len(pairs), 1)

        self.assertIn(
            "legal_form",
            pairs[0]["blocking_reasons"],
        )

        self.assertFalse(
            pairs[0]["identity_approved"],
        )

    def test_acronym_candidate(self):
        groups = build_groups(
            "Ente Idrico Campano",
            "EIC",
        )

        pairs, _ = generate_pairs(groups)

        self.assertEqual(len(pairs), 1)

        self.assertIn(
            "acronym",
            pairs[0]["blocking_reasons"],
        )

    def test_unrelated_names(self):
        groups = build_groups(
            "Gori",
            "Mario Rossi",
        )

        pairs, _ = generate_pairs(groups)

        self.assertEqual(pairs, [])

    def test_cross_type_conflict_is_not_merge(self):
        a = group("Marina", ["Person"])
        b = group("Marina S.p.A.", ["Company"])

        self.assertEqual(
            pair_types(a, b),
            "POTENTIAL_CONFLICT",
        )

        groups = {
            a["normalized_surface"]: a,
            b["normalized_surface"]: b,
        }

        pairs, _ = generate_pairs(groups)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(
            pairs[0]["type_compatibility"],
            "POTENTIAL_CONFLICT",
        )
        self.assertFalse(
            pairs[0]["identity_approved"],
        )

    def test_collective_is_excluded(self):
        profile = {
            "surface": "Gori e Regione Campania",
            "source_kind": "Organization",
            "resolution_status": "COLLECTIVE_HOLD",
        }

        self.assertFalse(eligible(profile))

    def test_held_component_excluded(self):
        profile = {
            "surface": "attività di manutenzione",
            "source_kind": "Project",
            "resolution_status": "PROFILE_ONLY_HELD",
        }

        self.assertFalse(eligible(profile))

    def test_reassessment_is_allowed(self):
        profile = {
            "surface": "Sinistra Italiana",
            "source_kind": "PoliticalParty",
            "resolution_status": "PROFILE_ONLY_REASSESS",
        }

        self.assertTrue(eligible(profile))

    def test_infrastructure_not_blocked(self):
        groups = build_groups(
            "Strada Statale 145",
            "Strada Statale 145 S.p.A.",
        )

        pairs, _ = generate_pairs(groups)

        self.assertEqual(len(pairs), 1)
        self.assertFalse(
            pairs[0]["identity_approved"],
        )

    def test_pair_generation_is_deterministic(self):
        groups = build_groups(
            "Gori",
            "Gori S.p.A.",
            "Ente Idrico Campano",
            "EIC",
        )

        first, _ = generate_pairs(groups)
        second, _ = generate_pairs(groups)

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
