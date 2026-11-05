
import unittest

from presskg.resolution.contextual_evidence import (
    occurrences,
    best_window,
    recover,
    trusted_sources,
)


class ContextualEvidenceTests(unittest.TestCase):

    def candidate(self):
        return {
            "mention_id": "vf-1:subject",
            "fact_id": "vf-1",
            "article_id": "article-1",
            "name": "Gori e Regione Campania",
            "original_kind": "Organization",
            "role": "subject",
            "status": "INSUFFICIENT_TEXTUAL_EVIDENCE",
            "structural_status": "POSSIBLE_COORDINATION",
            "components": [
                {"surface": "Gori"},
                {"surface": "Regione Campania"},
            ],
        }

    def source(self, body):
        return {
            "mention_id": "vf-1:subject",
            "fact_id": "vf-1",
            "fact_article_id": "article-1",
            "fact_claim_id": "claim-1",
            "article_id": "article-1",
            "claim_id": "claim-1",
            "claim_article_id": "article-1",
            "body": body,
            "title": "",
            "subtitle": "",
            "original_quote": None,
            "supporting_span": None,
            "claim_quote": None,
        }

    def test_exact_offsets(self):
        text = "GORI e Regione Campania"

        matches = occurrences(text, "Gori")

        self.assertEqual(len(matches), 1)
        self.assertEqual(
            text[
                matches[0]["start"]:
                matches[0]["end"]
            ],
            "GORI",
        )

    def test_normalized_whitespace(self):
        text = "Regione   Campania"

        matches = occurrences(
            text,
            "Regione Campania",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0]["surface"],
            text,
        )

    def test_component_window(self):
        body = (
            "La società Gori ha presentato "
            "un programma alla Regione Campania."
        )

        window = best_window(
            body,
            ["Gori", "Regione Campania"],
            max_span=150,
        )

        self.assertIsNotNone(window)
        self.assertEqual(
            len(window["components"]),
            2,
        )

    def test_distant_components(self):
        body = (
            "Gori "
            + ("testo " * 300)
            + "Regione Campania"
        )

        window = best_window(
            body,
            ["Gori", "Regione Campania"],
            max_span=100,
        )

        self.assertIsNone(window)

    def test_full_mention(self):
        candidate = self.candidate()

        source = self.source(
            "Gori e Regione Campania "
            "hanno presentato il progetto."
        )

        result = recover(
            candidate,
            source,
            radius=100,
            max_span=300,
        )

        self.assertEqual(
            result["status"],
            "FULL_MENTION_GROUNDED",
        )

        self.assertFalse(
            result["split_approved"]
        )

    def test_partial_component_context(self):
        source = self.source(
            "Gori ha collaborato con "
            "la Regione Campania."
        )

        result = recover(
            self.candidate(),
            source,
            radius=100,
            max_span=200,
        )

        self.assertEqual(
            result["status"],
            "COMPONENT_CONTEXT_RECOVERED",
        )

    def test_no_context(self):
        result = recover(
            self.candidate(),
            self.source("Articolo senza i due nomi."),
            radius=100,
            max_span=200,
        )

        self.assertEqual(
            result["status"],
            "NO_CONTEXT_FOUND",
        )

    def test_reject_unverified_quote(self):
        source = self.source("Corpo differente.")

        source["claim_quote"] = (
            "Gori e Regione Campania"
        )

        sources = trusted_sources(source)

        self.assertNotIn(
            "claim_quote",
            [field for field, _ in sources],
        )

    def test_reject_provenance_mismatch(self):
        source = self.source(
            "Gori e Regione Campania"
        )

        source["fact_article_id"] = "other"

        with self.assertRaises(ValueError):
            recover(
                self.candidate(),
                source,
                100,
                300,
            )


if __name__ == "__main__":
    unittest.main()
