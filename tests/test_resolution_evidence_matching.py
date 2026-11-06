
import unittest

from presskg.resolution.evidence_matching import (
    acronym,
    dominant_type,
    evaluate_pair,
    explicit_alias,
    legal_base,
    lexical_conflicts,
)


def group(name, kind="Organization", ratio_count=10):
    return {
        "group_id": "test:" + name,
        "preferred_surface": name,
        "kind_counts": {kind: ratio_count},
        "occurrence_count": ratio_count,
    }


def pair(left, right, score=0.90, reasons=None):
    return {
        "pair_id": "test-pair",
        "lexical_score": score,
        "blocking_reasons": reasons or ["rare_token"],
        "type_compatibility": "OVERLAPPING",
        "left_group_id": left["group_id"],
        "right_group_id": right["group_id"],
    }


def evidence_for_both(left, right, quote):
    return {
        left["group_id"]: {
            "article-1": [{
                "quote": quote,
                "field": "original_quote",
                "fact_id": "fact-1",
                "profile_id": "p1",
            }],
        },
        right["group_id"]: {
            "article-1": [{
                "quote": quote,
                "field": "original_quote",
                "fact_id": "fact-2",
                "profile_id": "p2",
            }],
        },
    }


class EvidenceMatchingTests(unittest.TestCase):

    def test_explicit_parenthetical_acronym(self):
        self.assertEqual(
            explicit_alias(
                "L'Ente Idrico Campano (EIC) "
                "ha approvato l'intervento.",
                "Ente Idrico Campano",
                "EIC",
            ),
            "explicit_parenthetical_acronym",
        )

    def test_plain_cooccurrence_not_alias(self):
        self.assertIsNone(
            explicit_alias(
                "EIC e l'Ente Idrico Campano "
                "hanno partecipato alla riunione.",
                "Ente Idrico Campano",
                "EIC",
            )
        )

    def test_false_acronym_without_attestation(self):
        self.assertIsNone(
            explicit_alias(
                "Emergenza idrica a Capri "
                "e altre iniziative.",
                "Emergenza idrica a Capri",
                "EIC",
            )
        )

    def test_supported_alias(self):
        left = group("Ente Idrico Campano")
        right = group("EIC")

        result = evaluate_pair(
            pair(left, right),
            left,
            right,
            evidence_for_both(
                left,
                right,
                "L'Ente Idrico Campano (EIC) "
                "gestisce le competenze indicate.",
            ),
            {},
        )

        self.assertEqual(
            result["verdict"],
            "MATCH_SUPPORTED",
        )
        self.assertFalse(
            result["identity_merge_approved"],
        )

    def test_same_article_not_sufficient(self):
        left = group("Ente Idrico Campano")
        right = group("EIC")

        result = evaluate_pair(
            pair(left, right),
            left,
            right,
            evidence_for_both(
                left,
                right,
                "EIC e Ente Idrico Campano "
                "sono menzionati nell'articolo.",
            ),
            {},
        )

        self.assertNotEqual(
            result["verdict"],
            "MATCH_SUPPORTED",
        )

    def test_legal_suffix_possible_only(self):
        left = group("Gori", "Company")
        right = group("Gori S.p.A.", "Company")

        result = evaluate_pair(
            pair(left, right, 1.0, ["legal_form"]),
            left,
            right,
            {},
            {},
        )

        self.assertTrue(result["legal_base_equal"])
        self.assertEqual(
            result["verdict"],
            "POSSIBLE_MATCH",
        )

    def test_minister_ministry_conflict(self):
        conflicts = lexical_conflicts(
            "Ministero dell'Ambiente",
            "Ministro dell'Ambiente",
        )

        self.assertIn(
            "institution_vs_office_holder",
            conflicts,
        )

    def test_different_numeric_designators(self):
        conflicts = lexical_conflicts(
            "4 valvole di regolazione della pressione",
            "8 valvole di regolazione della pressione",
        )

        self.assertIn(
            "different_numeric_designators",
            conflicts,
        )

    def test_mixed_type_requires_caution(self):
        mixed = {
            "kind_counts": {
                "Company": 6,
                "Person": 4,
            }
        }

        self.assertLess(
            dominant_type(mixed)["ratio"],
            0.80,
        )

    def test_acronym_alone_is_not_supported(self):
        left = group("Emergenza idrica a Capri")
        right = group("EIC")

        result = evaluate_pair(
            pair(left, right, 0.80, ["acronym"]),
            left,
            right,
            {},
            {},
        )

        self.assertEqual(
            result["verdict"],
            "INSUFFICIENT_EVIDENCE",
        )

    def test_infrastructure_geography_not_a_conflict(self):
        left = group(
            "Strada Statale 145",
            "Infrastructure",
        )
        right = group(
            "SS 145",
            "Infrastructure",
        )

        result = evaluate_pair(
            pair(left, right),
            left,
            right,
            {},
            {},
        )

        self.assertNotEqual(
            result["verdict"],
            "CONFLICT",
        )

    def test_no_clustering_approval(self):
        left = group("Acque del Sud", "Company")
        right = group("Acque del Sud Spa", "Company")

        result = evaluate_pair(
            pair(left, right),
            left,
            right,
            {},
            {},
        )

        self.assertFalse(
            result["identity_merge_approved"],
        )
        self.assertIsNone(
            result["canonical_entity_id"],
        )
        self.assertEqual(result["graph_writes"], 0)


if __name__ == "__main__":
    unittest.main()
