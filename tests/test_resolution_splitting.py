
import unittest

from presskg.resolution.splitting import (
    analyze_mention,
    check_source_row,
    choose_context,
    classify_mention,
    split_surface,
)


class MentionDecompositionTests(unittest.TestCase):

    def example(self, name="Gori e Regione Campania"):
        quote = (
            f"{name} hanno partecipato "
            "alla presentazione del progetto."
        )

        return {
            "mention_id": "vf-1:subject",
            "mention_fact_id": "vf-1",
            "mention_article_id": "article-1",
            "mention_claim_id": "claim-1",
            "mention_batch_id": "batch-1",
            "name": name,
            "kind": "Organization",
            "role": "subject",
            "fact_id": "vf-1",
            "article_id": "article-1",
            "claim_id": "claim-1",
            "fact_batch_id": "batch-1",
            "predicate": "PARTICIPATED_IN",
            "representation": "original",
            "original_quote": quote,
            "supporting_span": quote,
            "body": quote,
            "title": "",
            "subtitle": "",
            "mention_relation": "SUBJECT_MENTION",
        }

    def test_atomic_name(self):
        result = classify_mention("Gori")

        self.assertEqual(
            result["status"],
            "NO_STRUCTURAL_SPLIT",
        )

        self.assertEqual(
            result["components"],
            [],
        )

    def test_company_name_without_coordination(self):
        result = classify_mention(
            "GORI S.p.A."
        )

        self.assertEqual(
            result["status"],
            "NO_STRUCTURAL_SPLIT",
        )

    def test_simple_coordination(self):
        name = "Gori e Regione Campania"

        result = classify_mention(name)

        self.assertEqual(
            result["status"],
            "POSSIBLE_COORDINATION",
        )

        self.assertEqual(
            [
                item["text"]
                for item in result["components"]
            ],
            [
                "Gori",
                "Regione Campania",
            ],
        )

    def test_enumeration(self):
        result = classify_mention(
            "Scafati, Angri e Sarno"
        )

        self.assertEqual(
            result["status"],
            "POSSIBLE_ENUMERATION",
        )

        self.assertEqual(
            [
                part["text"]
                for part in result["components"]
            ],
            [
                "Scafati",
                "Angri",
                "Sarno",
            ],
        )

    def test_offsets(self):
        name = "Scafati, Angri e Sarno"

        for item in split_surface(name):
            self.assertEqual(
                name[
                    item["start"]:item["end"]
                ],
                item["text"],
            )

    def test_collective_expression(self):
        result = classify_mention(
            "i sindaci dell'Agro nocerino"
        )

        self.assertEqual(
            result["status"],
            "COLLECTIVE_OR_GROUP_NAME",
        )

    def test_official_name_is_not_auto_split(self):
        result = analyze_mention(
            self.example(
                "Comitato Acqua e Ambiente"
            )
        )

        self.assertEqual(
            result["status"],
            "POSSIBLE_COORDINATION",
        )

        self.assertFalse(
            result["split_approved"]
        )

        self.assertFalse(
            result["fact_expansion_allowed"]
        )

    def test_literal_evidence(self):
        result = choose_context(
            self.example(),
            radius=100,
        )

        self.assertEqual(
            result["field"],
            "original_quote",
        )

        self.assertTrue(
            result["literal_match"]
        )

    def test_missing_evidence(self):
        row = self.example()

        row["original_quote"] = (
            "Un evento è stato presentato."
        )

        row["supporting_span"] = (
            "Un altro testo privo del nome."
        )

        row["body"] = (
            "Un testo che non menziona i soggetti."
        )

        result = analyze_mention(row)

        self.assertEqual(
            result["status"],
            "INSUFFICIENT_TEXTUAL_EVIDENCE",
        )

        self.assertFalse(
            result["split_approved"]
        )

    def test_valid_source_row(self):
        check_source_row(
            self.example(),
            "batch-1",
        )

    def test_invalid_source_batch(self):
        with self.assertRaises(ValueError):
            check_source_row(
                self.example(),
                "another-batch",
            )

    def test_invalid_mention_relation(self):
        row = self.example()

        row["mention_relation"] = (
            "OBJECT_MENTION"
        )

        with self.assertRaises(ValueError):
            check_source_row(
                row,
                "batch-1",
            )
            
    def test_decimal_value_not_split(self):
        result = classify_mention(
            "1,5 milioni di euro",
            "Money",
        )
        self.assertEqual(
            result["status"],
            "NON_REFERENTIAL_TYPE",
        )


    def test_quantity_not_split(self):
        result = classify_mention(
            "8,5 chilometri",
            "Quantity",
        )
        self.assertEqual(
            result["components"],
            [],
        )


    def test_city_enumeration_still_detected(self):
        result = classify_mention(
            "Scafati, Angri e Sarno",
            "GPE",
        )
        self.assertEqual(
            result["status"],
            "POSSIBLE_ENUMERATION",
        )


if __name__ == "__main__":
    unittest.main()
