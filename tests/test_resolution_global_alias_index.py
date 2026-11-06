
import unittest
from collections import defaultdict

from presskg.resolution.global_alias_index import (
    extract_from_text,
    surface_pattern,
)


def make_index(*names):
    groups = {}
    by_surface = {}
    acronyms = defaultdict(set)
    legal = defaultdict(set)

    from presskg.resolution.candidate_generation import (
        normalize, acronym, legal_base,
    )

    for i, name in enumerate(names):
        gid = f"g{i}"
        key = normalize(name)

        groups[gid] = {
            "group_id": gid,
            "normalized_surface": key,
            "preferred_surface": name,
            "kind_counts": {"Organization": 1},
        }
        by_surface[key] = gid

        if acronym(key):
            acronyms[acronym(key)].add(gid)

        legal[legal_base(key)].add(gid)

    return {
        "groups": groups,
        "by_surface": by_surface,
        "acronym_index": acronyms,
        "legal_index": legal,
    }


class GlobalAliasIndexTests(unittest.TestCase):

    def test_parenthetical_acronym(self):
        index = make_index(
            "Ente Idrico Campano", "EIC"
        )

        text = (
            "L'Ente Idrico Campano (EIC) "
            "ha approvato l'intervento."
        )

        result = extract_from_text(text, index)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["rule"],
            "PARENTHETICAL_ACRONYM",
        )
        self.assertEqual(
            text[
                result[0]["start"]:
                result[0]["end"]
            ],
            result[0]["quote"],
        )

    def test_legal_form_parenthetical(self):
        index = make_index(
            "Gori", "Gori S.p.A."
        )

        result = extract_from_text(
            "La società Gori S.p.A. (Gori) "
            "ha comunicato i lavori.",
            index,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["rule"],
            "PARENTHETICAL_LEGAL_VARIANT",
        )

    def test_acronym_of(self):
        index = make_index(
            "Ente Idrico Campano", "EIC"
        )

        result = extract_from_text(
            "EIC, acronimo di Ente Idrico Campano, "
            "ha presentato il piano.",
            index,
        )

        self.assertTrue(result)
        self.assertEqual(
            result[0]["rule"],
            "EXPLICIT_ACRONYM_OF",
        )

    def test_dash_expansion(self):
        index = make_index(
            "Ente Idrico Campano", "EIC"
        )

        result = extract_from_text(
            "EIC - Ente Idrico Campano",
            index,
        )

        self.assertTrue(result)
        self.assertEqual(
            result[0]["rule"],
            "DASH_ACRONYM_EXPANSION",
        )

    def test_ocr_missing_spaces(self):
        index = make_index(
            "Banca Europea per gli Investimenti",
            "BEI",
        )

        text = (
            "La Banca Europea per gliinvestimenti"
            "(Bei) finanzia l'opera."
        )

        result = extract_from_text(text, index)

        self.assertTrue(result)
        self.assertEqual(
            result[0]["rule"],
            "PARENTHETICAL_ACRONYM",
        )

    def test_unrelated_parentheses(self):
        index = make_index(
            "Ente Idrico Campano", "EIC"
        )

        result = extract_from_text(
            "Ente Idrico Campano (2026)",
            index,
        )

        self.assertEqual(result, [])

    def test_initials_are_not_sufficient(self):
        index = make_index(
            "Emergenza idrica a Capri", "EIC"
        )

        result = extract_from_text(
            "Emergenza idrica a Capri "
            "è stata discussa dall'EIC.",
            index,
        )

        self.assertEqual(result, [])

    def test_no_substring_match(self):
        pattern = surface_pattern("Gori")

        import re

        self.assertIsNone(
            re.search(
                pattern,
                "categorie",
                re.IGNORECASE,
            )
        )

    def test_no_duplicate_alias_without_evidence(self):
        index = make_index(
            "Ente Idrico Campano", "EIC"
        )

        result = extract_from_text(
            "L'EIC ha incontrato alcuni "
            "rappresentanti dell'Ente Idrico Campano.",
            index,
        )

        self.assertEqual(result, [])

    def test_offset_consistency(self):
        index = make_index(
            "Waidy Management System", "WMS"
        )

        text = (
            "Il nuovo sistema Waidy Management "
            "System (WMS) è operativo."
        )

        result = extract_from_text(text, index)

        self.assertEqual(len(result), 1)

        item = result[0]

        self.assertEqual(
            item["quote"],
            text[item["start"]:item["end"]],
        )


if __name__ == "__main__":
    unittest.main()
