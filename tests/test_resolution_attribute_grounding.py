
import unittest

from presskg.resolution.attribute_grounding import (
    choose_evidence,
    evaluate,
    occurrences,
    predicate_cue,
)


def make_record(
    subject="Pierfrancesco Latini",
    predicate="WORKS_FOR",
    object_="Acea",
    subject_kind="Person",
    quote=None,
    disposition="PROFILE_EVIDENCE_CANDIDATE",
):
    if quote is None:
        quote = (
            f"{subject}, responsabile "
            f"delle iniziative di {object_}"
        )

    return {
        "evidence_id": "fact-1:WORKS_FOR",
        "fact_id": "fact-1",
        "claim_id": "claim-1",
        "article_id": "article-1",
        "subject_profile_id": "fact-1:subject",
        "subject_surface": subject,
        "object_surface": object_,
        "subject_kind": subject_kind,
        "object_kind": "Company",
        "predicate": predicate,
        "attribute_kind": "employer",
        "disposition": disposition,
        "evidence": {
            "quote": quote,
            "publication_date": "2024-01-01",
        },
    }


def article(body):
    return {
        "article_id": "article-1",
        "found_id": "article-1",
        "body": body,
        "title": "",
        "subtitle": "",
    }


class AttributeGroundingTests(unittest.TestCase):

    def test_literal_offset(self):
        text = "Pierfrancesco Latini lavora per Acea."

        matches = occurrences(
            text, "Acea"
        )

        self.assertEqual(len(matches), 1)

        match = matches[0]

        self.assertEqual(
            text[match["start"]:match["end"]],
            "Acea",
        )

    def test_case_insensitive_matching(self):
        text = "GORI opera in Campania."

        matches = occurrences(
            text, "Gori"
        )

        self.assertEqual(
            matches[0]["surface"],
            "GORI",
        )

        self.assertEqual(
            matches[0]["match_type"],
            "NORMALIZED",
        )

    def test_normalized_whitespace_offsets(self):
        text = "Regione   Campania"

        matches = occurrences(
            text,
            "Regione Campania",
        )

        self.assertEqual(
            matches[0]["surface"],
            text,
        )

    def test_no_substring_false_positive(self):
        self.assertEqual(
            occurrences(
                "Le categorie sono numerose.",
                "Gori",
            ),
            [],
        )

    def test_glued_name_is_not_grounded(self):
        record = make_record(
            subject="Gori",
            object_="Roccapiemonte",
            predicate="OPERATES_IN",
            subject_kind="Company",
            quote=(
                "LaGoriha preso casa "
                "anche a Roccapiemonte."
            ),
        )

        result = evaluate(
            record,
            article(record["evidence"]["quote"]),
        )

        self.assertEqual(
            result["status"],
            "PARTIAL_GROUNDING",
        )

        self.assertFalse(
            result["attribute_semantically_verified"],
        )

    def test_validated_quote_preferred(self):
        quote = (
            "Pierfrancesco Latini, "
            "responsabile iniziative di Acea"
        )

        record = make_record(quote=quote)

        result = choose_evidence(
            record,
            article(
                "Introduzione. " + quote
                + ". Altri dettagli."
            ),
        )

        self.assertEqual(
            result["status"],
            "BOTH_GROUNDED",
        )

        self.assertTrue(
            result["evidence"][
                "within_validated_quote"
            ],
        )

    def test_both_names_are_not_predicate_proof(self):
        text = (
            "Pierfrancesco Latini ha incontrato "
            "i rappresentanti di Acea."
        )

        record = make_record(
            quote=text
        )

        result = evaluate(
            record,
            article(text),
        )

        self.assertEqual(
            result["status"],
            "GROUNDED_RELATION_UNVERIFIED",
        )

        self.assertEqual(
            result["predicate_grounding"]["status"],
            "NO_EXPLICIT_CUE",
        )

    def test_explicit_professional_cue(self):
        text = (
            "Pierfrancesco Latini lavora per Acea."
        )

        record = make_record(
            quote=text
        )

        result = evaluate(
            record,
            article(text),
        )

        self.assertEqual(
            result["status"],
            "GROUNDED_CUE_CANDIDATE",
        )

        self.assertFalse(
            result["attribute_semantically_verified"],
        )

        self.assertFalse(
            result["match_constraint_approved"],
        )

    def test_infrastructure_scope_held(self):
        text = (
            "La Strada Statale 145 è situata "
            "nel comune di Castellammare."
        )

        record = make_record(
            subject="Strada Statale 145",
            object_="Castellammare",
            subject_kind="Infrastructure",
            predicate="LOCATED_IN",
            quote=text,
        )

        result = evaluate(
            record,
            article(text),
        )

        self.assertEqual(
            result["status"],
            "SCOPE_HELD",
        )

        self.assertIn(
            "infrastructure_extent_not_verified",
            result["scope_assessment"]["reasons"],
        )

    def test_missing_article(self):
        result = evaluate(
            make_record(),
            None,
        )

        self.assertEqual(
            result["status"],
            "NO_GROUNDING",
        )

    def test_names_far_apart_not_grounded(self):
        text = (
            "Pierfrancesco Latini "
            + ("testo " * 200)
            + "Acea"
        )

        record = make_record(
            quote=text,
        )

        result = choose_evidence(
            record,
            article(text),
            max_span=100,
        )

        self.assertNotEqual(
            result["status"],
            "BOTH_GROUNDED",
        )


if __name__ == "__main__":
    unittest.main()
