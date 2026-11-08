
import json
import unittest

from presskg.resolution.predicate_candidate_generation import (
    extract_context,
    stratified_sample,
    parse_candidate,
)


def fact(fid, subject="PERSON", obj="ORGANIZATION"):
    return {
        "fact_id": fid,
        "subject_facet": subject,
        "object_facet": obj,
    }


def grounded_context():
    quote = (
        "La società Alfa ha finanziato la costruzione "
        "del nuovo impianto comunale."
    )

    return {
        "quote": quote,
        "subject_name": "Società Alfa",
        "object_name": "Nuovo impianto comunale",
        "subject_facet": "ORGANIZATION",
        "object_facet": "FACILITY",
        "context": (
            "[EVIDENCE_START]"
            + quote
            + "[EVIDENCE_END]"
        ),
    }


class PredicateCandidateTests(unittest.TestCase):

    def test_context_exact_match(self):
        quote = (
            "La società Alfa ha finanziato "
            "il nuovo progetto."
        )
        body = "Introduzione. " + quote + " Conclusione."

        result = extract_context(body, quote, 50, 50)

        self.assertEqual(result["quote"], quote)
        self.assertIn("[EVIDENCE_START]", result["context"])
        self.assertEqual(result["matches_in_body"], 1)

    def test_context_normalized_match(self):
        quote = (
            "La società Alfa ha finanziato "
            "il nuovo progetto."
        )
        body = (
            "La società Alfa ha finanziato\n"
            "il nuovo progetto."
        )

        result = extract_context(body, quote)

        self.assertEqual(result["quote"], quote)

    def test_missing_quote_fails(self):
        with self.assertRaises(ValueError):
            extract_context(
                "Articolo senza relazione.",
                "Citazione non presente nell'articolo.",
            )

    def test_deterministic_sampling(self):
        rows = [
            fact(f"vf-{i}", "PERSON", "ORGANIZATION")
            for i in range(30)
        ] + [
            fact(f"vf-x{i}", "ORGANIZATION", "GEOGRAPHY")
            for i in range(10)
        ]

        a = stratified_sample(rows, 12, 42)
        b = stratified_sample(rows, 12, 42)

        self.assertEqual(
            [x["fact_id"] for x in a],
            [x["fact_id"] for x in b],
        )

        self.assertEqual(len(a), 12)
        self.assertEqual(
            len({x["fact_id"] for x in a}), 12
        )

    def test_valid_reclassification(self):
        row = grounded_context()

        raw = json.dumps({
            "decision": "RECLASSIFY",
            "proposed_predicate": "FUNDS",
            "suggested_new_predicate": None,
            "evidence_quote":
                "ha finanziato la costruzione",
            "rationale":
                "Il finanziamento è esplicito.",
        })

        result = parse_candidate(
            raw, row, ["FUNDS", "MANAGES"]
        )

        self.assertEqual(
            result["proposed_predicate"],
            "FUNDS",
        )
        self.assertFalse(result["approved"])

    def test_predicate_outside_taxonomy_rejected(self):
        row = grounded_context()

        raw = json.dumps({
            "decision": "RECLASSIFY",
            "proposed_predicate": "UNKNOWN_RELATION",
            "suggested_new_predicate": None,
            "evidence_quote":
                "ha finanziato la costruzione",
            "rationale": "Relazione esplicita.",
        })

        with self.assertRaises(ValueError):
            parse_candidate(raw, row, ["FUNDS"])

    def test_hallucinated_quote_rejected(self):
        row = grounded_context()

        raw = json.dumps({
            "decision": "RECLASSIFY",
            "proposed_predicate": "FUNDS",
            "suggested_new_predicate": None,
            "evidence_quote":
                "ha donato un milione di euro",
            "rationale": "Finanziamento.",
        })

        with self.assertRaises(ValueError):
            parse_candidate(raw, row, ["FUNDS"])

    def test_valid_abstention(self):
        row = grounded_context()

        raw = json.dumps({
            "decision": "INSUFFICIENT_EVIDENCE",
            "proposed_predicate": None,
            "suggested_new_predicate": None,
            "evidence_quote": None,
            "rationale":
                "Non è dimostrata la relazione richiesta.",
        })

        result = parse_candidate(raw, row, ["FUNDS"])

        self.assertEqual(
            result["decision"],
            "INSUFFICIENT_EVIDENCE",
        )

    def test_invalid_taxonomy_extension(self):
        row = grounded_context()

        raw = json.dumps({
            "decision": "TAXONOMY_GAP",
            "proposed_predicate": None,
            "suggested_new_predicate": "new relation",
            "evidence_quote":
                "ha finanziato la costruzione",
            "rationale": "Manca il predicato.",
        })

        with self.assertRaises(ValueError):
            parse_candidate(raw, row, ["FUNDS"])


if __name__ == "__main__":
    unittest.main()
