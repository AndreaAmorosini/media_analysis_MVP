
import unittest

from presskg.resolution.contextual_alias_recovery import (
    adjudicate,
    find_aliases,
    lexical_conflicts,
    select_articles,
    type_compatibility,
)


def group(name, kind="Organization"):
    return {
        "group_id": "group:" + name,
        "preferred_surface": name,
        "kind_counts": {kind: 10},
    }


def pair(left, right, score=0.90):
    return {
        "pair_id": "pair-1",
        "left_group_id": left["group_id"],
        "right_group_id": right["group_id"],
        "lexical_score": score,
        "blocking_reasons": ["rare_token"],
    }


def previous(verdict="INSUFFICIENT_EVIDENCE"):
    return {
        "verdict": verdict,
        "legal_base_equal": False,
        "shared_attribute_signatures": [],
    }


class ContextualAliasRecoveryTests(unittest.TestCase):

    def test_parenthetical_acronym(self):
        text = (
            "L'Ente Idrico Campano (EIC) "
            "ha approvato il progetto."
        )

        found = find_aliases(
            text, "Ente Idrico Campano", "EIC"
        )

        self.assertTrue(found)
        self.assertEqual(
            found[0]["rule"],
            "explicit_parenthetical",
        )
        self.assertEqual(
            text[found[0]["start"]:found[0]["end"]],
            found[0]["quote"],
        )

    def test_acronym_of(self):
        text = (
            "EIC, acronimo di Ente Idrico Campano, "
            "è citato nel documento."
        )

        found = find_aliases(
            text, "Ente Idrico Campano", "EIC"
        )

        self.assertTrue(
            any(
                item["rule"] == "explicit_acronym_of"
                for item in found
            )
        )

    def test_unattested_initials_are_not_alias(self):
        text = (
            "Emergenza idrica a Capri "
            "ha interessato anche EIC."
        )

        self.assertEqual(
            find_aliases(
                text,
                "Emergenza idrica a Capri",
                "EIC",
            ),
            [],
        )

    def test_plain_cooccurrence_is_not_alias(self):
        self.assertEqual(
            find_aliases(
                "EIC e Ente Idrico Campano "
                "sono menzionati.",
                "Ente Idrico Campano",
                "EIC",
            ),
            [],
        )

    def test_legal_suffix_parenthetical(self):
        text = "Gori S.p.A. (Gori) ha comunicato..."

        result = find_aliases(
            text, "Gori S.p.A.", "Gori"
        )

        self.assertTrue(result)

    def test_project_event_soft_compatibility(self):
        left = group("Bonifica Sarno", "Project")
        right = group("Bonifica del Sarno", "Event")

        result = type_compatibility(left, right)

        self.assertEqual(
            result["status"],
            "COMPATIBLE",
        )

    def test_facility_company_is_ambiguous(self):
        left = group("Marina di Stabia", "Facility")
        right = group(
            "Marina di Stabia srl", "Company"
        )

        self.assertEqual(
            type_compatibility(left, right)["status"],
            "AMBIGUOUS",
        )

    def test_minister_ministry_is_conflict(self):
        hard, soft = lexical_conflicts(
            "Ministero dell'Ambiente",
            "Ministro dell'Ambiente",
            {"status": "AMBIGUOUS"},
        )

        self.assertIn(
            "institution_vs_office_holder",
            hard,
        )

    def test_quantities_are_warning(self):
        hard, soft = lexical_conflicts(
            "4 valvole di regolazione",
            "8 valvole di regolazione",
            {"status": "COMPATIBLE"},
        )

        self.assertEqual(hard, [])
        self.assertIn(
            "different_numeric_designators",
            soft,
        )

    def test_article_selection_prefers_overlap(self):
        ids, shared = select_articles(
            "left", "right",
            {
                "left": {"a1", "a2"},
                "right": {"a2", "a3"},
            },
            limit=2,
        )

        self.assertEqual(shared, 1)
        self.assertEqual(ids[0], "a2")

    def test_explicit_alias_is_supported_without_merge(self):
        left = group("Ente Idrico Campano")
        right = group("EIC")

        result = adjudicate(
            pair(left, right),
            previous(),
            left,
            right,
            [{
                "rule": "explicit_parenthetical",
                "quote": "Ente Idrico Campano (EIC)",
            }],
        )

        self.assertEqual(
            result["verdict"],
            "MATCH_SUPPORTED",
        )
        self.assertFalse(
            result["identity_merge_approved"],
        )

    def test_legal_base_without_alias_is_possible(self):
        left = group("Gori", "Company")
        right = group("Gori S.p.A.", "Company")

        old = previous("POSSIBLE_MATCH")
        old["legal_base_equal"] = True

        result = adjudicate(
            pair(left, right),
            old,
            left, right, [],
        )

        self.assertEqual(
            result["verdict"],
            "POSSIBLE_MATCH",
        )

    def test_infrastructure_is_not_blocked_by_scope(self):
        left = group(
            "Strada Statale 145", "Infrastructure"
        )
        right = group(
            "SS 145", "Infrastructure"
        )

        result = adjudicate(
            pair(left, right),
            previous(),
            left, right, [],
        )

        self.assertNotEqual(
            result["verdict"],
            "CONFLICT",
        )
        
    def test_legal_suffix_variants(self):
        cases = [
            ("Gori S.p.A. (Gori)", "Gori S.p.A.", "Gori"),
            ("Gori S.p.A (Gori)", "Gori S.p.A.", "Gori"),
            ("Gori Spa (Gori)", "Gori S.p.A.", "Gori"),
            ("Gori S.r.l. (Gori)", "Gori S.r.l.", "Gori"),
        ]

        for text, long_name, short_name in cases:
            with self.subTest(text=text):
                found = find_aliases(
                    text, long_name, short_name
                )

                self.assertTrue(found)

                self.assertEqual(
                    found[0]["rule"],
                    "explicit_parenthetical",
                )


if __name__ == "__main__":
    unittest.main()
