"""Invarianti dello Stadio A: registry dei path, wiring STEPS, driver unico.

Stadio A della migrazione di resolution:
- ``presskg.resolution.paths`` è l'unica fonte delle directory artefatto
  (niente più literal "output/resolution/..." nei moduli step);
- la pipeline apre un driver Neo4j condiviso e gli step lo riusano via
  ``_common.step_driver(args)`` (unica eccezione dichiarata: ``dry_run``,
  che espone override espliciti uri/user/password);
- l'ordine STEPS di ogni fase è topologico rispetto alle dipendenze
  dichiarate (campo ``reads``).
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
RES = ROOT / "src" / "presskg" / "resolution"

PHASES = ["identity", "projection", "predicates", "events"]


def import_phase(phase):
    return __import__(f"presskg.resolution.{phase}", fromlist=["STEPS"])


class TestPathRegistry:

    def test_step_dirs_unique(self):
        from presskg.resolution.paths import STEP_DIRS
        names = list(STEP_DIRS.values())
        assert len(names) == len(set(names)), "nomi directory duplicati nel registry"

    @pytest.mark.parametrize("phase", PHASES)
    def test_every_step_has_registry_key(self, phase):
        from presskg.resolution.paths import STEP_DIRS
        mod = import_phase(phase)
        for entry in mod.STEPS:
            step_module = entry[1].__name__
            assert step_module.startswith("presskg.resolution.")
            key = step_module.rsplit(".", 1)[-1]
            assert key in STEP_DIRS, f"{phase}/{key} mancante in paths.STEP_DIRS"

    def test_no_hardcoded_output_paths_in_steps(self):
        offenders = []
        for path in RES.rglob("*.py"):
            if path.name in ("paths.py", "benchmark.py", "pipeline.py", "_common.py"):
                continue
            if re.search(r'"output/resolution', path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            "path 'output/resolution/...' hardcodati (usare paths.step_dir):\n"
            + "\n".join(offenders)
        )

    def test_default_root_respects_args(self, tmp_path):
        from argparse import Namespace

        from presskg.resolution import paths

        args = Namespace(out=str(tmp_path))
        assert paths.root(args) == tmp_path
        assert paths.step_dir(args, "baseline") == tmp_path / "baseline-v1"


class TestStepWiring:

    @pytest.mark.parametrize("phase", PHASES)
    def test_reads_reference_earlier_artifacts(self, phase):
        """Ogni step legge solo artefatti già prodotti: da step precedenti
        della stessa fase o da qualunque step delle fasi antecedenti."""
        import presskg.resolution.pipeline as pipeline
        from presskg.resolution.paths import STEP_DIRS

        mod = import_phase(phase)
        available = set(STEP_DIRS)

        prior_keys = set()
        for earlier_phase in pipeline.PHASES[: pipeline.PHASES.index(phase)]:
            for entry in import_phase(earlier_phase).STEPS:
                prior_keys.add(entry[1].__name__.rsplit(".", 1)[-1])

        seen = set(prior_keys)
        for name, step_module, *rest in mod.STEPS:
            reads = rest[0] if rest else []
            assert isinstance(reads, list), f"{phase}/{name}: reads deve essere una lista"
            key = step_module.__name__.rsplit(".", 1)[-1]
            for dependency in reads:
                assert dependency in available, (
                    f"{phase}/{name}: dipendenza '{dependency}' non nel registry"
                )
                assert dependency in seen, (
                    f"{phase}/{name}: legge '{dependency}' prima che sia "
                    f"prodotto (ordine STEPS rotto)"
                )
            seen.add(key)

    @pytest.mark.parametrize("phase", PHASES)
    def test_steps_declared_cover_package(self, phase):
        import presskg.resolution.paths as paths
        mod = import_phase(phase)
        declared = [e[1].__name__.rsplit(".", 1)[-1] for e in mod.STEPS]
        on_disk = sorted(p.name[:-3] for p in (RES / phase).glob("*.py")
                         if p.name != "__init__.py")
        assert sorted(declared) == sorted(on_disk), (
            f"{phase}: STEPS e file del package divergono "
            f"(dichiarati {sorted(declared)}, sul disco {sorted(on_disk)})"
        )


class TestDriverReuse:

    def test_no_direct_driver_in_steps(self):
        """Nessuno step apre il driver autonomamente.

        Eccezione dichiarata: projection/dry_run espone override espliciti
        --uri/--user/--password, ma preferisce comunque il driver condiviso.
        """
        offenders = []
        for path in RES.rglob("*.py"):
            if path.name == "_common.py":
                continue
            if path.name == "dry_run.py" and path.parent.name == "projection":
                continue
            if "GraphDatabase.driver(" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            "driver aperto direttamente (usare resolution._common.step_driver):\n"
            + "\n".join(offenders)
        )

    def test_pipeline_shares_driver(self):
        import inspect

        from presskg.resolution import pipeline

        source = inspect.getsource(pipeline.run)
        assert "args.driver" in source, (
            "pipeline.run deve attaccare il driver condiviso ad args (Stadio A)"
        )
        assert "finally:" in source, "pipeline.run deve chiudere il driver in finally"
