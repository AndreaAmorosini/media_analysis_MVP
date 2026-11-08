
import unittest

from presskg.resolution.predicate_evidence_grounding import (
    grounding_status,
    get_article_text,
    evaluate,
)


def sample_row():
    return {
        "fact_id": "vf-1",
        "article_id": "article-1",
        "projection_key": "cf-1",
        "subject_key": "ce-1",
        "object_key": "ce-2",
        "subject_name": "Ente A",
        "object_name": "Ente B",
        "subject_facet": "ORGANIZATION",
        "object_facet": "ORGANIZATION",
        "evidence": {
            "text": (
                "L'ente A ha approvato la proposta "
                "presentata dall'ente B."
            ),
            "truncated": False,
        },
        "structural_problems": [],
    }


class EvidenceGroundingTests(unittest.TestCase):

    def test_exact_match(self):
        quote = "La società ha approvato il nuovo progetto."
        source = "Ieri. " + quote + " Oggi."

        status, _ = grounding_status(quote, source)
        self.assertEqual(status, "VERIFIED_EXACT")

    def test_whitespace_normalization(self):
        quote = "La società ha approvato il nuovo progetto."
        source = (
            "La società ha approvato\n"
            "il nuovo progetto."
        )

        status, _ = grounding_status(quote, source)
        self.assertEqual(status, "VERIFIED_NORMALIZED")

    def test_absent_quote(self):
        status, _ = grounding_status(
            "La società ha approvato il nuovo progetto.",
            "Un testo completamente differente.",
        )
        self.assertEqual(status, "NOT_FOUND")

    def test_unavailable_source(self):
        status, _ = grounding_status(
            "La società ha approvato il nuovo progetto.",
            None,
        )
        self.assertEqual(status, "SOURCE_UNAVAILABLE")

    def test_article_field_selection(self):
        field, text = get_article_text(
            {
                "title": "Titolo",
                "body": "Corpo lungo dell'articolo",
            },
            ("body", "content"),
        )

        self.assertEqual(field, "body")
        self.assertEqual(text, "Corpo lungo dell'articolo")

    def test_no_metadata_as_article_body(self):
        field, text = get_article_text(
            {"title": "La società approva un progetto"},
            ("body", "content"),
        )

        self.assertIsNone(field)
        self.assertIsNone(text)

    def test_complete_grounding(self):
        row = sample_row()

        article = {
            "article_count": 1,
            "article_properties": {
                "body": (
                    "Ieri. "
                    + row["evidence"]["text"]
                    + " La conferenza è terminata."
                )
            },
        }

        result = evaluate(
            row, article, ("body",), 35
        )

        self.assertEqual(
            result["grounding_status"],
            "VERIFIED_EXACT",
        )
        self.assertTrue(
            result["eligible_for_refinement"]
        )

    def test_duplicate_articles(self):
        result = evaluate(
            sample_row(),
            {
                "article_count": 2,
                "article_properties": None,
            },
            ("body",),
            35,
        )

        self.assertEqual(
            result["grounding_status"],
            "SOURCE_AMBIGUOUS",
        )

    def test_truncated_quote_cannot_be_approved(self):
        row = sample_row()
        row["evidence"]["truncated"] = True

        result = evaluate(
            row,
            {
                "article_count": 1,
                "article_properties": {
                    "body": row["evidence"]["text"]
                },
            },
            ("body",),
            35,
        )

        self.assertEqual(
            result["grounding_status"],
            "PARTIAL_ONLY",
        )
        self.assertFalse(
            result["eligible_for_refinement"]
        )


if __name__ == "__main__":
    unittest.main()
