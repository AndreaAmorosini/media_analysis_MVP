
"""Regression test per il package di validazione."""

import importlib
import unittest


class ValidationPackageTests(unittest.TestCase):

    def test_import_all_modules(self):
        modules = (
            "presskg.validation.primary",
            "presskg.validation.corrections",
            "presskg.validation.decisions",
            "presskg.validation.materialization",
        )

        for module in modules:
            with self.subTest(module=module):
                imported = importlib.import_module(module)
                self.assertIsNotNone(imported)

    def test_primary_schema(self):
        from presskg.validation.primary import (
            FIELDS,
            VERDICTS,
        )

        self.assertIn("claim_id", FIELDS)
        self.assertIn("claim_hash", FIELDS)
        self.assertIn("run_hash", FIELDS)

        self.assertEqual(
            VERDICTS,
            {
                "supported_as_is",
                "correction_proposed",
                "unsupported",
                "insufficient_context",
            },
        )

    def test_correction_dependencies(self):
        from presskg.validation import (
            corrections,
            decisions,
            primary,
        )

        self.assertIs(
            corrections.claim_hash,
            primary.claim_hash,
        )

        self.assertIs(
            corrections.checked_judgement,
            decisions.checked_judgement,
        )

    def test_materialization_dependencies(self):
        from presskg.validation import (
            materialization,
            primary,
        )

        self.assertIs(
            materialization.claim_hash,
            primary.claim_hash,
        )

    def test_cli_components(self):
        from presskg.validation.__main__ import COMMANDS

        self.assertEqual(
            set(COMMANDS),
            {
                "primary",
                "corrections",
                "decisions",
                "materialize",
            },
        )


if __name__ == "__main__":
    unittest.main()
