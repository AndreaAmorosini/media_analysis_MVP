
"""Regression test per il package di validazione."""

import importlib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


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

    def test_no_frozen_batch_id_in_source(self):
        """Il batch id va derivato (presskg.validation.batch), non congelato."""
        frozen = "qwen38-gemma31-gemma12-v1"
        offenders = []
        targets = [
            * (REPO_ROOT / "src" / "presskg").rglob("*.py"),
            REPO_ROOT / "scripts" / "run_validation_full.sh",
        ]
        for target in targets:
            if target.name == "__pycache__":
                continue
            if frozen in target.read_text(encoding="utf-8"):
                offenders.append(str(target.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], (
            "batch id congelato nel sorgente; usare "
            "presskg.validation.batch.default_batch_id() o --batch-id"
        ))

    def test_cli_parsers_fill_all_defaults(self):
        """build_parser() deve dare un default a ogni opzione del modulo.

        Il wrapper __main__ gira gli argomenti al parser del modulo: se un
        attributo usato da un comando non ha default, ogni invocazione senza
        quell'opzione esplode con AttributeError.
        """
        from presskg.validation import corrections, decisions, materialization, primary

        ns = primary.build_parser().parse_args(["generate"])
        for attr in (
            "model", "limit", "context_chars", "max_tokens", "retries",
            "batch", "out", "resume", "claim_ids_file", "predicates",
            "only_flagged", "retry_invalid", "csv", "reviewer", "commit",
        ):
            self.assertTrue(hasattr(ns, attr), f"primary: manca il default di {attr}")
        self.assertEqual(ns.limit, 0)
        self.assertEqual(ns.predicates, [])

        ns = corrections.build_parser().parse_args(["prepare"])
        for attr in (
            "primary", "secondary", "candidates", "verified", "out",
            "report", "model", "context_chars", "limit", "max_tokens",
            "timeout", "retries", "max_consecutive_errors", "batch_id",
            "batch_size", "resume", "retry_failed", "overwrite", "commit",
        ):
            self.assertTrue(hasattr(ns, attr), f"corrections: manca il default di {attr}")

        ns = decisions.build_parser().parse_args(["apply"])
        for attr in (
            "primary", "secondary", "out", "skip_correction_proposals",
            "context_chars", "limit", "batch_id", "batch_size", "commit",
        ):
            self.assertTrue(hasattr(ns, attr), f"decisions: manca il default di {attr}")

        ns = materialization.build_parser().parse_args([])
        self.assertEqual(ns.batch_id, None, "il default è derivato in run() dai modelli")
        self.assertEqual(ns.padding, 600)
        self.assertEqual(ns.batch_size, 200)
        self.assertEqual(ns.limit, 0)
        self.assertIs(ns.commit, False)

    def test_batch_id_derivation(self):
        """Il batch id varia con i modelli della catena di validazione."""
        from presskg.validation.batch import (
            DEFAULT_SECONDARY_JUDGE,
            derive_batch_id,
            model_tag,
        )

        self.assertEqual(
            model_tag("unsloth/Qwen3.5-4B-MTP-GGUF:UD-Q4_K_XL"),
            "qwen35-4b-mtp",
        )
        self.assertEqual(
            model_tag("unsloth/gemma-4-E4B-it-GGUF:UD-Q8_K_XL"),
            "gemma-4-e4b",
        )
        self.assertEqual(model_tag("unsloth/Qwen3.8-27B-GGUF"), "qwen38-27b")
        self.assertEqual(model_tag(None), "unknown")
        self.assertEqual(model_tag(""), "unknown")

        # convenzione: <ingest>-<giudice>-<secondario>-vN
        self.assertEqual(
            derive_batch_id(
                extract_model="unsloth/Qwen3.8-27B-GGUF",
                judge_model="unsloth/gemma-4-31B-it-GGUF",
                secondary_model="unsloth/gemma-4-12b-it-GGUF",
            ),
            "qwen38-27b-gemma-4-31b-gemma-4-12b-v1",
        )
        # cambia un modello -> cambia l'id
        other = derive_batch_id(
            extract_model="unsloth/Qwen3.8-27B-GGUF",
            judge_model="unsloth/gemma-4-E4B-it-GGUF",
            secondary_model="unsloth/gemma-4-12b-it-GGUF",
        )
        self.assertNotEqual(
            other,
            "qwen38-27b-gemma-4-31b-gemma-4-12b-v1",
        )
        # secondario di default: dipende da UNSLOTH_EVAL_MODEL_BIS se settato
        from presskg.config import CFG

        expected_secondary = CFG.secondary_eval_model_id or DEFAULT_SECONDARY_JUDGE
        self.assertIn(
            model_tag(expected_secondary),
            derive_batch_id(
                extract_model="unsloth/Qwen3.8-27B-GGUF",
                judge_model="unsloth/gemma-4-31B-it-GGUF",
            ),
        )

    def test_batch_id_secondary_from_env_bis(self):
        """UNSLOTH_EVAL_MODEL_BIS diventa il secondario di default; vuoto = fallback."""
        from types import SimpleNamespace
        from unittest import mock

        import presskg.config as config_module
        from presskg.validation.batch import DEFAULT_SECONDARY_JUDGE, derive_batch_id, model_tag

        bis = "unsloth/Ministral-3-8B-Instruct-2512-GGUF"

        def fake_cfg(secondary):
            return SimpleNamespace(
                model_id="unsloth/Qwen3.8-27B-GGUF",
                secondary_eval_model_id=secondary,
            )

        with mock.patch.object(config_module, "CFG", fake_cfg(bis)):
            derived = derive_batch_id(
                judge_model="unsloth/gemma-4-31B-it-GGUF",
            )
        self.assertIn(model_tag(bis), derived)
        self.assertNotIn(model_tag(DEFAULT_SECONDARY_JUDGE), derived)

        with mock.patch.object(config_module, "CFG", fake_cfg("")):
            derived = derive_batch_id(
                judge_model="unsloth/gemma-4-31B-it-GGUF",
            )
        self.assertIn(model_tag(DEFAULT_SECONDARY_JUDGE), derived)

    def test_cli_dispatch_with_subcommand(self):
        """main() gira il sottocomando posizionale come args.command."""
        import sys
        from unittest import mock

        from presskg.validation import primary

        captured = {}

        def fake_run(ns):
            captured["ns"] = ns

        argv = [
            "presskg.validation",
            "primary",
            "generate",
            "--model",
            "gemma",
            "--limit",
            "10",
        ]
        with mock.patch.object(primary, "run", fake_run), \
                mock.patch.object(sys, "argv", argv):
            from presskg.validation.__main__ import main
            main()

        ns = captured["ns"]
        self.assertEqual(ns.command, "generate")
        self.assertEqual(ns.model, "gemma")
        self.assertEqual(ns.limit, 10)
        # i default arrivano dal parser del modulo anche se non richiesti
        self.assertEqual(ns.claim_ids_file, None)

    def test_cli_exit_code_on_module_failure(self):
        """Un modulo che restituisce False deve produrre exit code 1."""
        import sys
        from unittest import mock

        from presskg.validation import decisions

        argv = ["presskg.validation", "decisions", "apply"]

        with mock.patch.object(decisions, "run", lambda ns: False), \
                mock.patch.object(sys, "argv", argv):
            from presskg.validation.__main__ import main
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
