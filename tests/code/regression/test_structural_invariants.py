"""Invarianti strutturali della pipeline di resolution.

Questi test non verificano la logica di business ma l'architettura:

- tutti i moduli di ``presskg`` devono essere importabili;
- ogni step di resolution deve esporre il contratto ``run(args) -> bool``;
- ogni fase (subpackage di ``presskg.resolution``) deve esporre
  ``run(args, state) -> state`` e una lista ``STEPS`` coerente;
- la pipeline deve preservare l'ordine delle fasi e il threading dello stato;
- nessun refactoring deve reintrodurre riferimenti ai vecchi percorsi
  flat dei moduli (``presskg.settings``, ``presskg.resolution.profiles``…);
- i task di ``pixi.toml`` con ``python -m <modulo>`` devono puntare a moduli esistenti.

Se uno di questi test fallisce, un refactoring ha rotto la struttura
concordata: sistemare il codice prima di continuare.
"""

import ast
import importlib
import inspect
import re
import types
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "presskg"

PHASE_PACKAGES = ["identity", "projection", "predicates", "events"]
EXPECTED_PHASE_ORDER = ["identity", "projection", "predicates", "events"]


# ============================================================
# Helpers
# ============================================================

def all_modules():
    """Restituisce i nomi di tutti i moduli del package presskg."""
    names = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


# ============================================================
# Importabilità
# ============================================================

class TestImportability:
    """Ogni modulo del package deve essere importabile senza errori."""

    @pytest.mark.parametrize("module", all_modules())
    def test_module_importable(self, module):
        mod = importlib.import_module(module)
        assert mod is not None


# ============================================================
# Contratti dei moduli di resolution
# ============================================================

class TestStepContract:
    """Ogni step di resolution espone run(ctx) -> StepResult (Stadio B)."""

    PHASE_STEPS = {
        phase: sorted(
            p.name for p in (SRC / "resolution" / phase).glob("*.py")
            if p.name != "__init__.py"
        )
        for phase in PHASE_PACKAGES
    }

    def test_all_phases_have_steps(self):
        assert all(len(steps) > 0 for steps in self.PHASE_STEPS.values())

    @pytest.mark.parametrize("phase", PHASE_PACKAGES)
    def test_phase_run_contract(self, phase):
        """La fase espone run(args, state) -> state e la lista STEPS."""
        mod = importlib.import_module(f"presskg.resolution.{phase}")
        run_fn = getattr(mod, "run", None)
        assert callable(run_fn), f"{phase} non espone run()"
        params = inspect.signature(run_fn).parameters
        assert list(params) == ["args", "state"], (
            f"presskg.resolution.{phase}.run deve avere firma (args, state), "
            f"trovata {tuple(params)}"
        )

        steps = getattr(mod, "STEPS", None)
        assert steps, f"{phase} deve dichiarare la lista STEPS"
        for name, step, *_ in steps:
            assert re.fullmatch(r"\d{2}_[a-z_]+", name), (
                f"{phase}: nome step non conforme '{name}'"
            )
            assert callable(getattr(step, "run", None)), (
                f"{phase}: lo step {step.__name__} non espone run()"
            )

    @pytest.mark.parametrize("phase", PHASE_PACKAGES)
    def test_steps_declared_cover_package(self, phase):
        """Ogni modulo step del package compare esattamente una volta in STEPS."""
        mod = importlib.import_module(f"presskg.resolution.{phase}")
        declared = [step.__name__.rsplit(".", 1)[-1] for _, step, *_ in mod.STEPS]
        on_disk = [p[:-3] for p in self.PHASE_STEPS[phase]]
        assert sorted(declared) == sorted(on_disk), (
            f"{phase}: STEPS e file del package divergono "
            f"(dichiarati {sorted(declared)}, sul disco {sorted(on_disk)})"
        )

    @pytest.mark.parametrize("phase", PHASE_PACKAGES)
    def test_step_signature(self, phase):
        """Ogni step ha firma run(ctx) -> StepResult (Stadio B)."""
        mod = importlib.import_module(f"presskg.resolution.{phase}")
        from presskg.resolution.context import StepResult

        for _, step, *_ in mod.STEPS:
            params = inspect.signature(step.run).parameters
            assert list(params) == ["ctx"], (
                f"{step.__name__}.run deve avere firma (ctx), trovata {tuple(params)}"
            )
            assert step.run.__annotations__.get("return") in (
                "StepResult", StepResult,
            ), f"{step.__name__}.run deve restituire StepResult"


# ============================================================
# Contratto della pipeline
# ============================================================

class TestPipelineContract:

    def test_phase_order_is_stable(self):
        from presskg.resolution import pipeline
        assert pipeline.PHASES == EXPECTED_PHASE_ORDER

    def test_defaults_are_dry_run(self):
        from presskg.resolution import pipeline
        args = pipeline.build_parser().parse_args([])
        assert args.phase == "all"
        assert args.apply is False
        assert args.out == "output/resolution"
        assert args.verbose is False
        assert args.save_state is False

    def test_accepts_single_phase(self):
        from presskg.resolution import pipeline
        args = pipeline.build_parser().parse_args(["--phase", "identity"])
        assert args.phase == "identity"

    def test_state_threading_and_phase_order(self, monkeypatch, tmp_path):
        """run() inizializza lo stato, passa lo stesso dict e rispetta l'ordine."""
        from presskg.resolution import pipeline

        calls = []
        real_import = importlib.import_module

        def fake_import(name, *a, **kw):
            if name.startswith("presskg.resolution."):
                phase = name.rsplit(".", 1)[-1]

                def fake_run(args, state):
                    calls.append(phase)
                    state.setdefault("phases", []).append(phase)
                    state[f"{phase}_ok"] = True
                    return state

                mod = types.SimpleNamespace(run=fake_run)
                monkeypatch.setitem(sys_modules(), name, mod)
                return mod
            return real_import(name, *a, **kw)

        monkeypatch.setattr(pipeline.importlib, "import_module", fake_import)
        args = Namespace(phase="all", apply=False, out=str(tmp_path), verbose=False,
                         save_state=False)
        state = pipeline.run(args)
        # dry-run: predicates ed events operano sul grafo canonico
        # MATERIALIZZATO e vengono saltate (vedi docstring di pipeline.run)
        assert calls == ["identity", "projection"]
        assert state["phases"] == ["identity", "projection"]
        assert all(state[f"{p}_ok"] for p in ("identity", "projection"))
        assert state["predicates"] == {"skipped_dry_run": True}
        assert state["events"] == {"skipped_dry_run": True}

        # con --apply girano tutte e quattro, in ordine
        calls.clear()
        state.pop("phases")
        args = Namespace(phase="all", apply=True, out=str(tmp_path),
                         verbose=False, save_state=False)
        state = pipeline.run(args)
        assert calls == EXPECTED_PHASE_ORDER
        assert state["phases"] == EXPECTED_PHASE_ORDER

    def test_fail_fast_on_phase_error(self, monkeypatch, tmp_path):
        from presskg.resolution import pipeline

        real_import = importlib.import_module

        def fake_import(name, *a, **kw):
            if name.startswith("presskg.resolution."):
                def fake_run(args, state):
                    raise RuntimeError("boom")
                return types.SimpleNamespace(run=fake_run)
            return real_import(name, *a, **kw)

        monkeypatch.setattr(pipeline.importlib, "import_module", fake_import)
        args = Namespace(phase="identity", apply=False, out=str(tmp_path),
                         verbose=False, save_state=False)
        with pytest.raises(RuntimeError, match="boom"):
            pipeline.run(args)

    def test_save_state_writes_json(self, tmp_path, monkeypatch):
        from presskg.resolution import pipeline
        import presskg.resolution.identity as identity_pkg

        def fake_run(args, state):
            state["identity"] = {"ok": True}
            return state

        monkeypatch.setattr(identity_pkg, "run", fake_run)
        args = Namespace(phase="identity", apply=False, out=str(tmp_path),
                         verbose=False, save_state=True)
        state = pipeline.run(args)
        assert (tmp_path / "identity_state.json").exists()
        assert state["identity"] == {"ok": True}


def sys_modules():
    import sys
    return sys.modules


# ============================================================
# Assenza di riferimenti ai percorsi legacy
# ============================================================

class TestNoLegacyPaths:
    """Nessun file Python può importare i percorsi flat pre-refactoring."""

    # (pattern, motivo)
    LEGACY = [
        r"\bpresskg\.core\.[a-z_]+\b", r"\bpresskg\._common\b",
        r"\bpresskg\.(articles|extraction|extraction_rules|enrich_graph|"
        r"entity_ontology|graph_store|grounding|normalize_predicates|ontology|"
        r"predicate_catalog|resilient_extraction|settings)\b",
        r"\bpresskg\.resolution\.(profiles|baseline|attributes|"
        r"global_alias_index|candidate_generation|constrained_clustering|"
        r"occurrence_assignment|contextual_identity_refinement|"
        r"contextual_evidence|contextual_alias_recovery|coreference_audit|"
        r"evidence_matching|identity_integrity_gate|projection_readiness|"
        r"projection_readiness_refinement|referent_eligibility|"
        r"relation_type_scope|semantic_decomposition|splitting|"
        r"attribute_grounding|predicate_candidate_generation|"
        r"predicate_candidate_recovery|predicate_consistency_audit|"
        r"predicate_evidence_grounding|predicate_quality_audit|"
        r"predicate_semantic_verification|canonical_predicate_overlay|"
        r"canonical_projection_dry_run|canonical_materialization|"
        r"canonical_fact_materialization|canonical_fact_projection|"
        r"materialization_safety_gate|canonical_graph_consolidation|"
        r"canonical_event_projection|event_evidence_inventory|"
        r"canonical_graph_audit|validated_event_evidence)\b",
    ]

    def python_sources(self):
        return [
            path for path in [
                *SRC.rglob("*.py"),
                *(ROOT / "scripts").glob("*.py"),
                *(ROOT / "tests").rglob("*.py"),
            ]
            # il denylist stesso contiene i pattern per nome: skip auto-riferimenti
            if path.name != Path(__file__).name
        ]

    def test_no_legacy_imports(self):
        offenders = []
        for path in self.python_sources():
            text = path.read_text(encoding="utf-8")
            for pattern in self.LEGACY:
                match = re.search(pattern, text)
                if match:
                    offenders.append(f"{path}: '{match.group(0)}'")
        assert not offenders, (
            "Trovati riferimenti ai vecchi percorsi flat dei moduli:\n"
            + "\n".join(offenders)
        )


# ============================================================
# Integrità dei riferimenti: script e task pixi
# ============================================================

class TestReferenceIntegrity:

    def _presskg_modules_from(self, text: str):
        found = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("presskg"):
                found.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("presskg"):
                        found.add(alias.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in re.findall(r"presskg(?:\.[a-z_]+)+", node.value):
                    found.add(m)
        return found

    def test_scripts_import_existing_modules(self):
        offenders = []
        for path in (ROOT / "scripts").glob("*.py"):
            for name in self._presskg_modules_from(path.read_text(encoding="utf-8")):
                try:
                    importlib.import_module(name)
                except ImportError as exc:
                    offenders.append(f"{path}: {name} ({exc})")
        assert not offenders, "Script che importano moduli inesistenti:\n" + "\n".join(offenders)

    def test_pixi_task_modules_exist(self):
        toml_path = ROOT / "pixi.toml"
        text = toml_path.read_text(encoding="utf-8")
        tasks = re.findall(r'^(\w[\w-]*)\s*=\s*"([^"]+)"', text, re.M)
        offenders = []
        for task, cmd in tasks:
            match = re.match(r"python -m ([\w.]+)", cmd)
            if not match:
                continue
            module = match.group(1).split(" ")[0]
            # presskg.cli esegue i subcomandi; il modulo deve esistere.
            try:
                importlib.import_module(module)
            except ImportError as exc:
                offenders.append(f"task '{task}': {module} ({exc})")
        assert not offenders, "Task pixi che puntano a moduli inesistenti:\n" + "\n".join(offenders)

    def test_cli_subcommands_present(self, monkeypatch, capsys):
        """I sottocomandi attesi esistono e --help esce senza errori."""
        import sys

        import presskg.cli as cli

        for command in [
            "preflight", "init-db", "ingest", "stats",
            "pipeline", "enrich", "normalize-predicates",
        ]:
            monkeypatch.setattr(sys, "argv", ["presskg.cli", command, "--help"])
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 0, f"--help di '{command}' è fallito"
        help_text = capsys.readouterr().out
        assert "pipeline" in help_text or "--phase" in help_text

class TestConfigRoot:
    """config.ROOT deve restare la radice del repo, qualunque sia la posizione del file."""

    def test_root_points_to_repo_root(self):
        from presskg.config import ROOT
        assert (ROOT / ".env.example").exists(), (
            f"presskg.config.ROOT non è la radice del repo: {ROOT}"
        )